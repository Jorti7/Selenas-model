"""
Property detail enrichment — fetches individual Zillow listing pages
to extract fields not available in the search API:

  year_built, garage_spaces, has_garage, parking_type,
  tax_annual, school_elementary, school_middle, school_high,
  zestimate (confirmed), has_virtual_tour, open_house_start,
  listing_agent, full price history

Uses the same pyzill-inspired technique: extract gdpClientCache
from __NEXT_DATA__ JSON embedded in the property detail page.
"""

import json
import logging
import random
import time
from typing import Any

logger = logging.getLogger(__name__)

_DETAIL_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
}


def enrich_listing_detail(listing_url: str, zillow_id: str,
                           proxy: str | None = None) -> dict:
    """
    Fetch a single Zillow property page and extract detail fields.
    Returns a dict of enrichment fields (all optional, may be None).
    """
    empty = _empty_detail()
    if not listing_url:
        return empty

    try:
        from curl_cffi import requests as cffi_requests
    except ImportError:
        logger.warning("curl_cffi not available — detail enrichment skipped")
        return empty

    proxies = {"http": proxy, "https": proxy} if proxy else None

    try:
        resp = cffi_requests.get(
            listing_url,
            headers=_DETAIL_HEADERS,
            impersonate="chrome124",
            timeout=20,
            verify=False,
            proxies=proxies,
        )
        if resp.status_code != 200:
            logger.debug("Detail page %s returned %d", zillow_id, resp.status_code)
            return empty
    except Exception as exc:
        logger.debug("Detail fetch failed for %s: %s", zillow_id, exc)
        return empty

    return _parse_detail_page(resp.text)


def _parse_detail_page(html: str) -> dict:
    """Extract property detail from the Next.js __NEXT_DATA__ embedded in HTML."""
    empty = _empty_detail()
    try:
        # Find __NEXT_DATA__ script tag
        start = html.find('"__NEXT_DATA__"')
        if start == -1:
            # Try as a script tag
            marker = '<script id="__NEXT_DATA__"'
            start = html.find(marker)
            if start == -1:
                return empty
            start = html.find(">", start) + 1
            end = html.find("</script>", start)
        else:
            # Inline JSON — find the enclosing braces
            start = html.rfind("{", 0, start)
            end = _find_closing_brace(html, start)

        if start == -1 or end == -1:
            return empty

        raw = html[start:end + 1] if end != -1 else html[start:]
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return empty

    try:
        component_props = (
            data.get("props", {})
            .get("pageProps", {})
            .get("componentProps", {})
        )

        # gdpClientCache is a JSON string inside the JSON (double-encoded)
        gdp_raw = component_props.get("gdpClientCache", "{}")
        if isinstance(gdp_raw, str):
            gdp = json.loads(gdp_raw)
        else:
            gdp = gdp_raw

        # Find the "property" sub-object
        prop = {}
        for value in gdp.values():
            if isinstance(value, dict) and "property" in value:
                prop = value.get("property", {})
                break

        if not prop:
            return empty

        return _extract_fields(prop)
    except Exception as exc:
        logger.debug("Detail parse error: %s", exc)
        return empty


def _extract_fields(prop: dict) -> dict:
    detail = _empty_detail()

    # Basics often missing from search API
    detail["year_built"] = _safe_int(prop.get("yearBuilt"))
    detail["zestimate"] = _safe_int(prop.get("zestimate"))
    detail["tax_annual"] = _safe_int(prop.get("taxAnnualAmount"))

    # Garage / parking
    reso = prop.get("resoFacts", {}) or {}
    detail["garage_spaces"] = _safe_int(reso.get("garageSpaces") or prop.get("garageSpaces"))
    detail["has_garage"] = bool(reso.get("hasGarage") or (detail["garage_spaces"] or 0) > 0)
    detail["parking_type"] = reso.get("parkingFeatures") or prop.get("parkingType") or ""
    if isinstance(detail["parking_type"], list):
        detail["parking_type"] = ", ".join(detail["parking_type"])

    # School district
    schools = prop.get("schools") or []
    for school in schools:
        level = (school.get("level") or "").lower()
        name = school.get("name") or ""
        dist = school.get("districtName") or ""
        label = f"{name} ({dist})" if dist else name
        if "elementary" in level or "primary" in level:
            detail["school_elementary"] = label
        elif "middle" in level or "junior" in level:
            detail["school_middle"] = label
        elif "high" in level or "senior" in level:
            detail["school_high"] = label

    # Virtual tour
    detail["has_virtual_tour"] = bool(
        prop.get("virtualTourUrl") or prop.get("has3DModel") or
        prop.get("virtualTour")
    )

    # Open house
    open_houses = prop.get("openHouseSchedule") or []
    if open_houses:
        first = open_houses[0]
        detail["open_house_start"] = first.get("startTime") or first.get("date") or ""

    # Listing agent
    agent = prop.get("attributionInfo", {}) or {}
    agent_name = agent.get("agentName") or agent.get("listingAgentName") or ""
    agent_phone = agent.get("agentPhoneNumber") or agent.get("listingAgentPhoneNumber") or ""
    if agent_name:
        detail["listing_agent"] = f"{agent_name} {agent_phone}".strip()

    # Price history (for enriching price_history table)
    history = prop.get("priceHistory") or []
    detail["price_history_raw"] = [
        {
            "price": _safe_int(h.get("price")),
            "date": h.get("date") or "",
            "event": h.get("event") or "",
        }
        for h in history
        if h.get("price")
    ]

    return detail


def batch_enrich(listings: list[dict], config: dict) -> list[dict]:
    """
    Enrich a batch of listings with property detail data.
    Only processes listings missing key detail fields.
    Returns enriched listings (mutates in-place and returns).
    """
    limit = config.get("detail_enrichment_limit")
    proxy = config.get("proxy") or None
    enriched_count = 0

    for listing in listings:
        if limit is not None and enriched_count >= limit:
            break

        # Skip if we already have the key detail fields
        if listing.get("year_built") and listing.get("garage_spaces") is not None:
            continue

        url = listing.get("listing_url", "")
        zid = listing.get("zillow_id", "")
        if not url:
            continue

        detail = enrich_listing_detail(url, zid, proxy=proxy)
        if detail:
            # Merge detail fields — don't overwrite fields already populated
            for key, value in detail.items():
                if key == "price_history_raw":
                    listing["price_history_raw"] = value
                elif value is not None and not listing.get(key):
                    listing[key] = value

        enriched_count += 1
        logger.debug("Enriched %s (%d/%s)", zid, enriched_count, limit or "all")
        time.sleep(random.uniform(1.5, 3.0))

    logger.info("Detail enrichment complete: %d listings enriched", enriched_count)
    return listings


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_closing_brace(s: str, start: int) -> int:
    depth = 0
    for i in range(start, len(s)):
        if s[i] == "{":
            depth += 1
        elif s[i] == "}":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _safe_int(val: Any) -> int | None:
    try:
        return int(float(str(val).replace(",", "").strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _empty_detail() -> dict:
    return {
        "year_built": None,
        "zestimate": None,
        "tax_annual": None,
        "garage_spaces": None,
        "has_garage": None,
        "parking_type": None,
        "school_elementary": None,
        "school_middle": None,
        "school_high": None,
        "has_virtual_tour": None,
        "open_house_start": None,
        "listing_agent": None,
        "price_history_raw": [],
    }
