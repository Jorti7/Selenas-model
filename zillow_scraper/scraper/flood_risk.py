"""
Flood risk enrichment using FEMA's National Flood Hazard Layer (NFHL) REST API
and Harris County Appraisal District (HCAD) for Finished Floor Elevation.

FEMA NFHL API (no key required):
  https://hazards.fema.gov/gis/nfhl/rest/services/public/NFHL/MapServer/28/query

HCAD property data (Harris County only):
  https://pdata.hcad.org/PDATA/
"""

import logging
import time
import requests

logger = logging.getLogger(__name__)

FEMA_ZONE_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)
FEMA_BFE_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/16/query"
)
HCAD_SEARCH_URL = "https://pdata.hcad.org/PDATA/addr-building-value"

# Flood zone risk tiers
_ZONE_RISK = {
    'X': 'Minimal',
    'X500': 'Low',
    'B': 'Low',
    'C': 'Minimal',
    'D': 'Unknown',
    'A': 'High',
    'AE': 'High',
    'AH': 'High',
    'AO': 'High',
    'AR': 'High',
    'A99': 'High',
    'V': 'Very High',
    'VE': 'Very High',
}


def enrich_flood_data(listings: list[dict]) -> list[dict]:
    """Add flood_zone, bfe, ffe, freeboard, flood_risk_label to each listing."""
    enriched = []
    for lst in listings:
        lat = lst.get('lat')
        lon = lst.get('lon')
        if lat is None or lon is None:
            lst.update(_unknown_flood())
            enriched.append(lst)
            continue

        fema_data = _query_fema(lat, lon)
        ffe = _query_hcad_ffe(lst.get('address', ''), lst.get('zip_code', ''))
        flood_info = _compute_risk(fema_data, ffe)
        lst.update(flood_info)
        enriched.append(lst)
        time.sleep(0.3)  # be polite to FEMA API

    return enriched


def _query_fema(lat: float, lon: float) -> dict:
    """Query FEMA NFHL for flood zone (layer 28) and nearest BFE (layer 16)."""
    base_params = {
        'geometry': f'{lon},{lat}',
        'geometryType': 'esriGeometryPoint',
        'inSR': '4326',
        'spatialRel': 'esriSpatialRelIntersects',
        'returnGeometry': 'false',
        'f': 'json',
    }
    try:
        # Flood zone
        resp = requests.get(
            FEMA_ZONE_URL,
            params={**base_params, 'outFields': 'FLD_ZONE,SFHA_TF'},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        features = data.get('features', [])
        if not features:
            return {}
        attrs = features[0].get('attributes', {})
        zone = (attrs.get('FLD_ZONE') or '').strip().upper()

        # BFE — search within 500 m of the point (BFE is a line feature)
        bfe = None
        bfe_resp = requests.get(
            FEMA_BFE_URL,
            params={
                **base_params,
                'outFields': 'ELEV,LEN_UNIT',
                'distance': 500,
                'units': 'esriSRUnit_Meter',
                'spatialRel': 'esriSpatialRelIntersects',
            },
            timeout=10,
        )
        if bfe_resp.ok:
            bfe_data = bfe_resp.json()
            bfe_features = bfe_data.get('features', [])
            if bfe_features:
                elev = bfe_features[0].get('attributes', {}).get('ELEV')
                if elev not in (None, -9999, -9998):
                    bfe = float(elev)

        return {'flood_zone': zone or 'Unknown', 'bfe': bfe}
    except Exception as exc:
        logger.warning("FEMA API error for (%s, %s): %s", lat, lon, exc)
        return {}


def _query_hcad_ffe(address: str, zip_code: str) -> float | None:
    """
    Attempt to retrieve Finished Floor Elevation from HCAD.
    Only works for Harris County (zip codes 770xx).
    Returns None if not found or not in Harris County.
    """
    if not zip_code.startswith('770'):
        return None
    try:
        resp = requests.get(
            HCAD_SEARCH_URL,
            params={'address': address, 'zip': zip_code},
            timeout=10,
            headers={'Accept': 'application/json'},
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        # HCAD response shape varies; try common paths
        items = data.get('data', data.get('results', []))
        if items:
            ffe = items[0].get('finished_floor_elevation') or items[0].get('ffe')
            return float(ffe) if ffe else None
    except Exception:
        pass
    return None


def _compute_risk(fema_data: dict, ffe: float | None) -> dict:
    if not fema_data:
        return _unknown_flood()

    zone = fema_data.get('flood_zone', 'Unknown')
    bfe = fema_data.get('bfe')

    freeboard = None
    if ffe is not None and bfe is not None:
        freeboard = round(ffe - bfe, 2)

    # Determine label
    zone_key = zone.replace(' ', '').upper()
    base_risk = _ZONE_RISK.get(zone_key, 'Unknown')

    if freeboard is not None:
        if freeboard >= 2:
            label = 'Low'
        elif 0 <= freeboard < 2:
            label = 'Moderate'
        else:
            label = 'High'
    else:
        label = base_risk

    return {
        'flood_zone': zone,
        'bfe': bfe,
        'ffe': ffe,
        'freeboard': freeboard,
        'flood_risk_label': label,
    }


def _unknown_flood() -> dict:
    return {
        'flood_zone': None,
        'bfe': None,
        'ffe': None,
        'freeboard': None,
        'flood_risk_label': 'Unknown',
    }
