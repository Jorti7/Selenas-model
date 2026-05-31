import statistics
import logging

logger = logging.getLogger(__name__)


def apply_filters(listings: list[dict], config: dict) -> list[dict]:
    """Hard-filter listings against config criteria."""
    f = config['filters']
    passed = []

    for lst in listings:
        if not _passes(lst, f):
            continue
        passed.append(lst)

    logger.info("Filters: %d → %d listings", len(listings), len(passed))
    return passed


def _passes(lst: dict, f: dict) -> bool:
    price = lst.get('price')
    if price is None or price < f['price_min'] or price > f['price_max']:
        return False

    beds = lst.get('beds')
    if beds is None or beds < f['min_beds']:
        return False

    baths = lst.get('baths')
    if baths is None or baths < f['min_baths']:
        return False

    lot = lst.get('lot_sqft')
    if lot is not None and lot < f['min_lot_sqft']:
        return False

    hoa = lst.get('hoa_monthly')
    if hoa is not None and hoa > f['max_hoa_monthly']:
        return False

    # Property type guard (already filtered in scraper but double-check)
    ptype = lst.get('property_type', '')
    if ptype and ptype not in ('SINGLE_FAMILY', ''):
        return False

    return True


def apply_dom_filter(listings: list[dict], config: dict) -> list[dict]:
    """
    Keep listings where DOM <= min(config max, area median DOM).
    Listings with unknown DOM pass through (can't filter what we don't know).
    """
    max_dom = config['filters']['max_days_on_market']
    doms = [lst['dom'] for lst in listings if lst.get('dom') is not None]

    area_median = max_dom
    if doms:
        area_median = int(statistics.median(doms))
        logger.info("Area median DOM: %d days (config max: %d)", area_median, max_dom)

    cutoff = min(max_dom, area_median)
    result = []
    for lst in listings:
        dom = lst.get('dom')
        if dom is None or dom <= cutoff:
            result.append(lst)

    logger.info("DOM filter (cutoff=%d): %d → %d listings", cutoff, len(listings), len(result))
    return result
