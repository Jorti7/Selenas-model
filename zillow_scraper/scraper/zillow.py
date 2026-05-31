"""
Zillow scraper — internal async API approach (pyzill-inspired).

Key improvements over HTML scraping:
- PUT to /async-create-search-page-state (Zillow's internal JSON API)
- curl_cffi with Chrome TLS fingerprint impersonation — no Playwright needed
- mapResults returns ALL listings in bounds at once (up to 500)
- Automatic quadrant splitting when hitting the 500-result cap
- Merge listResults (rich fields) + mapResults (full coverage)
- Proper Chrome Sec-CH-UA headers + randomized request IDs and delays
"""

import json
import logging
import random
import time
from typing import Any

logger = logging.getLogger(__name__)

ZILLOW_API_URL = "https://www.zillow.com/async-create-search-page-state"
VALID_HOME_TYPES = {"SINGLE_FAMILY"}

# Impersonate Chrome 124 — matches pyzill's approach
_CHROME_IMPERSONATE = "chrome124"

# API call headers (XHR/fetch style, not page navigation)
_API_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Content-Type": "application/json",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "origin": "https://www.zillow.com",
    "referer": "https://www.zillow.com/",
    "Sec-Ch-Ua": '"Not_A Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


# ---------------------------------------------------------------------------
# Payload builder
# ---------------------------------------------------------------------------

def _build_payload(bounds: dict, config: dict, page: int = 1) -> dict:
    f = config["filters"]
    return {
        "searchQueryState": {
            "isMapVisible": True,
            "isListVisible": True,
            "mapBounds": {
                "north": bounds["north"],
                "east": bounds["east"],
                "south": bounds["south"],
                "west": bounds["west"],
            },
            "filterState": {
                "sortSelection": {"value": "globalrelevanceex"},
                "isAllHomes": {"value": True},
                "price": {"min": f["price_min"], "max": f["price_max"]},
                "beds": {"min": f["min_beds"]},
                "baths": {"min": f["min_baths"]},
                "sqft": {"min": 800},
                "lotSize": {"min": f["min_lot_sqft"]},
                "isHouseType": {"value": True},
                "isCondoType": {"value": False},
                "isApartmentType": {"value": False},
                "isManufacturedType": {"value": False},
                "isLotType": {"value": False},
                "isTownhouseType": {"value": False},
                "isMultiFamilyType": {"value": False},
                "doz": {"value": "90"},
            },
            "mapZoom": config.get("search", {}).get("zoom_level", 9),
            "pagination": {"currentPage": page},
        },
        "wants": {
            "cat1": ["listResults", "mapResults"],
            "cat2": ["total"],
        },
        "requestId": random.randint(3, 20),
        "isDebugRequest": False,
    }


# ---------------------------------------------------------------------------
# Single API call
# ---------------------------------------------------------------------------

def _call_api(bounds: dict, config: dict, page: int = 1) -> tuple[list[dict], int, bool]:
    """
    Single PUT to Zillow's internal API.
    Returns (merged_listings, total_count, hit_map_cap).
    hit_map_cap=True means mapResults returned 500 — there are more listings.
    """
    from curl_cffi import requests as cffi_requests

    proxy = config.get("proxy") or None
    proxies = {"http": proxy, "https": proxy} if proxy else None

    payload = _build_payload(bounds, config, page)

    resp = cffi_requests.put(
        ZILLOW_API_URL,
        json=payload,
        headers=_API_HEADERS,
        impersonate=_CHROME_IMPERSONATE,
        timeout=30,
        verify=False,   # cloud sandbox TLS proxy
        proxies=proxies,
    )
    resp.raise_for_status()

    data = resp.json()
    cat1 = data.get("cat1", {})
    results = cat1.get("searchResults", {})
    total = data.get("cat2", {}).get("total", 0) or 0

    map_results = results.get("mapResults", [])
    list_results = results.get("listResults", [])

    # Merge: listResults has richer fields; mapResults has more coverage.
    # Index by zpid, preferring listResults data where available.
    merged: dict[str, dict] = {}
    for item in map_results:
        zpid = str(item.get("zpid", ""))
        if zpid:
            merged[zpid] = item
    for item in list_results:
        zpid = str(item.get("zpid", ""))
        if zpid:
            # Overlay listResults fields onto the map entry (richer)
            if zpid in merged:
                merged[zpid] = {**merged[zpid], **item}
            else:
                merged[zpid] = item

    hit_cap = len(map_results) >= 500
    return list(merged.values()), total, hit_cap


# ---------------------------------------------------------------------------
# Quadrant splitting (recursive, for when mapResults hits 500)
# ---------------------------------------------------------------------------

def _split_bounds(bounds: dict) -> list[dict]:
    lat_mid = (bounds["north"] + bounds["south"]) / 2
    lon_mid = (bounds["east"] + bounds["west"]) / 2
    return [
        {"north": bounds["north"], "south": lat_mid, "east": lon_mid,        "west": bounds["west"]},
        {"north": bounds["north"], "south": lat_mid, "east": bounds["east"], "west": lon_mid},
        {"north": lat_mid,         "south": bounds["south"], "east": lon_mid,        "west": bounds["west"]},
        {"north": lat_mid,         "south": bounds["south"], "east": bounds["east"], "west": lon_mid},
    ]


def _collect_all(bounds: dict, config: dict, depth: int = 0,
                 seen: set | None = None) -> list[dict]:
    """
    Recursively collect listings by splitting bounds when cap is hit.
    Max depth 4 (256 sub-regions). Min box ~0.01° before giving up.
    """
    if seen is None:
        seen = set()

    box_size = (bounds["north"] - bounds["south"]) * (bounds["east"] - bounds["west"])
    if box_size < 0.0001 or depth > 4:
        logger.warning("Bounds too small or max depth reached at depth %d — skipping", depth)
        return []

    try:
        listings, total, hit_cap = _call_api(bounds, config)
    except Exception as exc:
        logger.error("API call failed at depth %d: %s", depth, exc)
        return []

    # Filter already-seen zpids
    new_listings = [l for l in listings if str(l.get("zpid", "")) not in seen]
    for l in new_listings:
        seen.add(str(l.get("zpid", "")))

    logger.info(
        "Depth %d | bounds N%.3f S%.3f E%.3f W%.3f → %d listings%s",
        depth,
        bounds["north"], bounds["south"], bounds["east"], bounds["west"],
        len(new_listings),
        " [CAP — splitting]" if hit_cap else "",
    )

    if not hit_cap:
        return new_listings

    # Hit the 500-result cap — recurse into quadrants
    time.sleep(random.uniform(1.0, 2.5))
    all_results = list(new_listings)
    for quad in _split_bounds(bounds):
        all_results.extend(_collect_all(quad, config, depth + 1, seen))
        time.sleep(random.uniform(0.8, 2.0))

    return all_results


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def scrape_zillow(config: dict) -> list[dict]:
    """
    Scrape Zillow via the internal async API.
    Returns raw listing dicts (before flood enrichment / filtering).
    Falls back to Scrapling DynamicFetcher if the API is blocked.
    """
    bounds = config["search"]["bounds"]

    # --- Primary: internal API via curl_cffi ---
    try:
        from curl_cffi import requests as cffi_requests  # noqa: F401 (import check)
        raw = _collect_all(bounds, config)
        if raw:
            logger.info("API scrape complete: %d raw listings", len(raw))
            return [_parse_listing(item) for item in raw if item.get("zpid")]

        logger.warning("API returned 0 listings — falling back to Scrapling")
    except ImportError:
        logger.warning("curl_cffi not available — falling back to Scrapling")
    except Exception as exc:
        logger.warning("API approach failed (%s) — falling back to Scrapling", exc)

    # --- Fallback: Scrapling DynamicFetcher (browser-based) ---
    return _scrape_with_scrapling(config)


# ---------------------------------------------------------------------------
# Listing parser
# ---------------------------------------------------------------------------

def _parse_listing(item: dict) -> dict:
    """Normalise a raw Zillow API/HTML result into our canonical dict."""
    hd = item.get("hdpData", {}).get("homeInfo", {})

    lat_lon = item.get("latLong", {})
    lot_raw = hd.get("lotAreaValue") or item.get("lotAreaValue")
    lot_unit = hd.get("lotAreaUnit") or item.get("lotAreaUnit") or "sqft"

    detail_url = item.get("detailUrl", "")
    if detail_url and not detail_url.startswith("http"):
        detail_url = "https://www.zillow.com" + detail_url

    price_raw = item.get("unformattedPrice") or item.get("price", "")
    price = _safe_int(str(price_raw).replace("$", "").replace(",", "").replace("+", ""))

    # Zestimate
    zestimate = _safe_int(
        item.get("zestimate") or hd.get("zestimate") or item.get("hdpData", {}).get("zestimate")
    )

    # Price reduction (how much it dropped from original list)
    price_reduction = _safe_int(
        hd.get("priceReduction") or item.get("priceReduction")
    )

    # Listing sub-type (foreclosure, new construction, etc.)
    listing_sub_type = _extract_listing_sub_type(item, hd)

    return {
        "zillow_id": str(item.get("zpid", "")),
        "address": item.get("addressStreet") or item.get("address", ""),
        "city": item.get("addressCity") or hd.get("city", ""),
        "zip_code": str(item.get("addressZipcode") or hd.get("zipcode", "")),
        "lat": lat_lon.get("latitude") or hd.get("latitude"),
        "lon": lat_lon.get("longitude") or hd.get("longitude"),
        "price": price,
        "beds": _safe_int(item.get("beds") or hd.get("bedrooms")),
        "baths": _safe_float(item.get("baths") or hd.get("bathrooms")),
        "living_sqft": _safe_int(item.get("area") or hd.get("livingArea")),
        "lot_sqft": _to_sqft(lot_raw, lot_unit),
        "year_built": _safe_int(hd.get("yearBuilt")),
        "hoa_monthly": _safe_int(hd.get("hoaFee") or hd.get("monthlyHoaFee")),
        "dom": _safe_int(hd.get("daysOnZillow") or item.get("daysOnZillow")),
        "list_date": hd.get("datePostedString", ""),
        "property_type": hd.get("homeType", "SINGLE_FAMILY"),
        "listing_url": detail_url,
        "thumbnail_url": item.get("imgSrc", ""),
        # New fields from API
        "zestimate": zestimate,
        "price_reduction": price_reduction,
        "listing_sub_type": listing_sub_type,
        "broker_name": item.get("brokerName", ""),
        "has_3d_tour": bool(item.get("has3DModel")),
    }


def _extract_listing_sub_type(item: dict, hd: dict) -> str:
    sub = item.get("hdpData", {}).get("homeInfo", {}).get("listing_sub_type", {})
    if isinstance(sub, dict):
        if sub.get("is_newHome"):
            return "new_construction"
        if sub.get("is_foreclosure"):
            return "foreclosure"
        if sub.get("is_FSBO"):
            return "fsbo"
        if sub.get("is_bankOwned"):
            return "bank_owned"
    return "standard"


# ---------------------------------------------------------------------------
# Scrapling fallback (browser-based, slower)
# ---------------------------------------------------------------------------

def _scrape_with_scrapling(config: dict) -> list[dict]:
    import urllib.parse

    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError:
        logger.error("Neither curl_cffi nor Scrapling is available")
        return []

    bounds = config["search"]["bounds"]
    all_listings: list[dict] = []
    seen_ids: set[str] = set()
    page = 1
    max_pages = 20

    fetcher = DynamicFetcher()

    while page <= max_pages:
        state = {
            "pagination": {"currentPage": page},
            "isMapVisible": True,
            "mapBounds": bounds,
            "filterState": _build_payload(bounds, config, page)["searchQueryState"]["filterState"],
            "isListVisible": True,
        }
        url = (
            "https://www.zillow.com/homes/for_sale/"
            f"?searchQueryState={urllib.parse.quote(json.dumps(state, separators=(',', ':')))}"
        )
        logger.info("Scrapling fallback — page %d", page)

        try:
            result = fetcher.fetch(
                url,
                headless=True,
                network_idle=True,
                timeout=60000,
                extra_flags=["--ignore-certificate-errors", "--ignore-ssl-errors"],
            )
        except Exception as exc:
            logger.error("Scrapling error on page %d: %s", page, exc)
            break

        tag = result.find("script#__NEXT_DATA__")
        if tag is None:
            logger.error("__NEXT_DATA__ not found on page %d", page)
            break

        try:
            page_data = json.loads(tag.text)
        except (json.JSONDecodeError, AttributeError):
            break

        search_state = (
            page_data.get("props", {})
            .get("pageProps", {})
            .get("searchPageState", {})
        )
        raw = (
            search_state.get("cat1", {})
            .get("searchResults", {})
            .get("listResults", [])
        )

        if not raw:
            break

        page_ids = {str(i.get("zpid", "")) for i in raw}
        overlap = page_ids & seen_ids
        if len(overlap) > len(page_ids) * 0.5:
            new = [i for i in raw if str(i.get("zpid", "")) not in seen_ids]
            all_listings.extend(_parse_listing(i) for i in new if i.get("zpid"))
            break

        seen_ids.update(page_ids)
        all_listings.extend(_parse_listing(i) for i in raw if i.get("zpid"))

        if len(raw) < 40:
            break

        page += 1
        time.sleep(random.uniform(1.5, 3.5))

    logger.info("Scrapling fallback complete: %d listings across %d pages", len(all_listings), page)
    return all_listings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_sqft(value: Any, unit: str) -> int | None:
    if value is None:
        return None
    try:
        v = float(value)
        if unit and "acre" in unit.lower():
            return int(v * 43560)
        return int(v)
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any) -> int | None:
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _safe_float(val: Any) -> float | None:
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError, AttributeError):
        return None
