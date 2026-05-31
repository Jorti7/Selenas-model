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
            lot_raw = item.get('lotAreaValue')
            lot_unit = item.get('lotAreaUnit', 'sqft')
            lot_sqft = _to_sqft(lot_raw, lot_unit)

            hoa = hd.get('hoaFee') or item.get('hdpData', {}).get('homeInfo', {}).get('monthlyHoaFee')

            listing = {
                'zillow_id': str(item.get('zpid', '')),
                'address': item.get('address', ''),
                'city': hd.get('city', ''),
                'zip_code': str(hd.get('zipcode', '')),
                'lat': lat_lon.get('latitude'),
                'lon': lat_lon.get('longitude'),
                'price': _safe_int(item.get('price', '').replace('$', '').replace(',', '').replace('+', '')),
                'beds': _safe_int(item.get('beds')),
                'baths': _safe_float(item.get('baths')),
                'living_sqft': _safe_int(item.get('area')),
                'lot_sqft': lot_sqft,
                'year_built': _safe_int(hd.get('yearBuilt')),
                'hoa_monthly': _safe_int(hoa),
                'dom': _safe_int(item.get('daysOnZillow') or hd.get('daysOnZillow')),
                'list_date': hd.get('datePostedString', ''),
                'property_type': home_type,
                'listing_url': 'https://www.zillow.com' + item.get('detailUrl', ''),
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
    page = 1
    max_pages = 20  # safety cap (~800 listings)

    fetcher = DynamicFetcher(auto_match=False)

    while page <= max_pages:
        url = _build_search_url(config, page)
        logger.info("Fetching Zillow page %d ...", page)

        try:
            result = fetcher.fetch(
                url,
                headless=True,
                network_idle=True,
                timeout=60000,
            )
        except Exception as exc:
            logger.error("DynamicFetcher error on page %d: %s", page, exc)
            break

        # Extract __NEXT_DATA__ embedded JSON
        next_data_tag = result.find('script#__NEXT_DATA__', first=True)
        if next_data_tag is None:
            # Fallback: try json-ld or embedded window.__data__
            logger.warning("__NEXT_DATA__ not found on page %d, trying fallback", page)
            next_data_tag = result.find('script[id="__NEXT_DATA__"]', first=True)

        if next_data_tag is None:
            logger.error("Could not find page data on page %d. Zillow may have blocked the request.", page)
            break

        try:
            page_data = json.loads(next_data_tag.text)
        except (json.JSONDecodeError, AttributeError) as exc:
            logger.error("Failed to parse __NEXT_DATA__ JSON on page %d: %s", page, exc)
            break

        listings, total = _parse_listings_from_page_data(page_data)
        logger.info("Page %d: got %d listings (total reported: %d)", page, len(listings), total)

        if not listings:
            break

        all_listings.extend(listings)

        # Check if we've collected all results
        if len(all_listings) >= total or len(listings) < 40:
            break

        page += 1
        time.sleep(2)  # polite delay between pages

    logger.info("Scrape complete: %d listings collected across %d pages", len(all_listings), page)
    return all_listings
