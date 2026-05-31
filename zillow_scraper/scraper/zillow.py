import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

ZILLOW_SEARCH_URL = "https://www.zillow.com/homes/for_sale/"

# Map Zillow homeType strings to our canonical names
VALID_HOME_TYPES = {"SINGLE_FAMILY"}


def _build_search_url(config: dict, page: int = 1) -> str:
    b = config['search']['bounds']
    f = config['filters']
    state = {
        "pagination": {"currentPage": page},
        "isMapVisible": True,
        "mapBounds": {
            "north": b['north'],
            "south": b['south'],
            "east": b['east'],
            "west": b['west'],
        },
        "filterState": {
            "price": {"min": f['price_min'], "max": f['price_max']},
            "beds": {"min": f['min_beds']},
            "baths": {"min": f['min_baths']},
            "sqft": {"min": 800},
            "lotSize": {"min": f['min_lot_sqft']},
            "isHouseType": {"value": True},
            "isCondoType": {"value": False},
            "isApartmentType": {"value": False},
            "isManufacturedType": {"value": False},
            "isLotType": {"value": False},
            "isTownhouseType": {"value": False},
            "isMultiFamilyType": {"value": False},
            "doz": {"value": "90"},
        },
        "isListVisible": True,
    }
    encoded = urllib.parse.quote(json.dumps(state, separators=(',', ':')))
    return f"{ZILLOW_SEARCH_URL}?searchQueryState={encoded}"


def _parse_listings_from_page_data(data: dict) -> tuple[list[dict], int]:
    """Extract listing records and total count from Zillow's __NEXT_DATA__ JSON."""
    try:
        search_state = (
            data.get('props', {})
                .get('pageProps', {})
                .get('searchPageState', {})
        )
        cat = search_state.get('cat1', {})
        results = cat.get('searchResults', {})
        total = results.get('totalResultCount', 0)
        raw_listings = results.get('listResults', [])
    except (KeyError, AttributeError):
        return [], 0

    listings = []
    for item in raw_listings:
        try:
            hd = item.get('hdpData', {}).get('homeInfo', {})
            home_type = hd.get('homeType', '')
            if home_type not in VALID_HOME_TYPES:
                continue

            lat_lon = item.get('latLong', {})
            # lotAreaValue lives in homeInfo, not at the top level
            lot_raw = hd.get('lotAreaValue') or item.get('lotAreaValue')
            lot_unit = hd.get('lotAreaUnit') or item.get('lotAreaUnit') or 'sqft'
            lot_sqft = _to_sqft(lot_raw, lot_unit)

            hoa = hd.get('hoaFee') or hd.get('monthlyHoaFee')

            # detailUrl is already a full URL from Zillow
            detail_url = item.get('detailUrl', '')
            if detail_url and not detail_url.startswith('http'):
                detail_url = 'https://www.zillow.com' + detail_url

            # Price comes as formatted string like "$367,000" or as unformattedPrice int
            price_raw = item.get('unformattedPrice') or item.get('price', '')
            price = _safe_int(str(price_raw).replace('$', '').replace(',', '').replace('+', ''))

            listing = {
                'zillow_id': str(item.get('zpid', '')),
                'address': item.get('addressStreet') or item.get('address', ''),
                'city': item.get('addressCity') or hd.get('city', ''),
                'zip_code': str(item.get('addressZipcode') or hd.get('zipcode', '')),
                'lat': lat_lon.get('latitude') or hd.get('latitude'),
                'lon': lat_lon.get('longitude') or hd.get('longitude'),
                'price': price,
                'beds': _safe_int(item.get('beds') or hd.get('bedrooms')),
                'baths': _safe_float(item.get('baths') or hd.get('bathrooms')),
                'living_sqft': _safe_int(item.get('area') or hd.get('livingArea')),
                'lot_sqft': lot_sqft,
                'year_built': _safe_int(hd.get('yearBuilt')),
                'hoa_monthly': _safe_int(hoa),
                'dom': _safe_int(hd.get('daysOnZillow') or item.get('daysOnZillow')),
                'list_date': hd.get('datePostedString', ''),
                'property_type': home_type,
                'listing_url': detail_url,
                'thumbnail_url': item.get('imgSrc', ''),
            }
            if listing['zillow_id']:
                listings.append(listing)
        except Exception as exc:
            logger.warning("Failed to parse listing item: %s", exc)
            continue

    return listings, total


def _to_sqft(value, unit: str) -> int | None:
    if value is None:
        return None
    try:
        v = float(value)
        if unit and 'acre' in unit.lower():
            return int(v * 43560)
        return int(v)
    except (ValueError, TypeError):
        return None


def _safe_int(val) -> int | None:
    try:
        return int(float(str(val).replace(',', '').strip()))
    except (ValueError, TypeError, AttributeError):
        return None


def _safe_float(val) -> float | None:
    try:
        return float(str(val).replace(',', '').strip())
    except (ValueError, TypeError, AttributeError):
        return None


def scrape_zillow(config: dict) -> list[dict]:
    """
    Scrape Zillow for Houston-area listings matching config filters.
    Returns a list of raw listing dicts (before flood enrichment).
    """
    try:
        from scrapling.fetchers import DynamicFetcher
    except ImportError:
        logger.error("Scrapling not installed. Run: pip install 'scrapling[fetchers]'")
        return []

    all_listings = []
    seen_ids: set[str] = set()
    page = 1
    max_pages = 20  # safety cap

    fetcher = DynamicFetcher()

    while page <= max_pages:
        url = _build_search_url(config, page)
        logger.info("Fetching Zillow page %d ...", page)

        try:
            result = fetcher.fetch(
                url,
                headless=True,
                network_idle=True,
                timeout=60000,
                # Allow sandbox TLS interception proxy
                extra_flags=['--ignore-certificate-errors', '--ignore-ssl-errors'],
            )
        except Exception as exc:
            logger.error("DynamicFetcher error on page %d: %s", page, exc)
            break

        # Extract __NEXT_DATA__ embedded JSON (find() returns first match or None)
        next_data_tag = result.find('script#__NEXT_DATA__')
        if next_data_tag is None:
            logger.warning("__NEXT_DATA__ not found on page %d, trying fallback", page)
            next_data_tag = result.find('script[id="__NEXT_DATA__"]')

        if next_data_tag is None:
            logger.error("Could not find page data on page %d. Zillow may have blocked the request.", page)
            break

        try:
            page_data = json.loads(next_data_tag.text)
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.error("Failed to parse __NEXT_DATA__ JSON on page %d: %s", page, exc)
            break

        listings, total = _parse_listings_from_page_data(page_data)
        logger.info("Page %d: got %d listings (total reported: %s)", page, len(listings), total or 'N/A')

        if not listings:
            break

        # Dedup check: if more than half the page IDs are ones we've seen, Zillow is cycling
        page_ids = {l['zillow_id'] for l in listings}
        overlap = page_ids & seen_ids
        if len(overlap) > len(page_ids) * 0.5:
            logger.info("Page %d has %d/%d duplicate IDs — reached end of results", page, len(overlap), len(page_ids))
            # Still add the new ones
            new_on_page = [l for l in listings if l['zillow_id'] not in seen_ids]
            all_listings.extend(new_on_page)
            break

        seen_ids.update(page_ids)
        all_listings.extend(listings)

        # Stop when we've collected all results or received a partial page (last page)
        if total and len(all_listings) >= total:
            break
        if len(listings) < 40:
            break

        page += 1
        time.sleep(2)  # polite delay between pages

    logger.info("Scrape complete: %d listings collected across %d pages", len(all_listings), page)
    return all_listings
