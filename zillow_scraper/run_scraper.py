"""
Entry point for the Zillow scraper.

Usage:
  python run_scraper.py --now          # run once immediately
  python run_scraper.py                # start scheduler (runs every 7 days)
"""

import argparse
import logging
import os
import sys
import yaml

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config() -> dict:
    with open(os.path.join(BASE_DIR, 'config.yaml')) as f:
        return yaml.safe_load(f)


def run_full_scrape(config: dict):
    from database.db import (
        get_conn, init_db, upsert_listing, mark_inactive,
        detect_relisting, update_flood_data, update_ml_score,
        update_detail_data, get_labeled_listings, get_active_listings,
        log_run_start, log_run_finish,
    )
    from scraper.zillow import scrape_zillow
    from scraper.filters import apply_filters, apply_dom_filter
    from scraper.flood_risk import enrich_flood_data
    from learner.scorer import score_all

    init_db(config)
    conn = get_conn(config)
    run_id = log_run_start(conn)

    logger.info("=== Zillow scrape run #%d started ===", run_id)
    errors = []

    # 1. Scrape (internal API → curl_cffi, fallback → Scrapling)
    try:
        raw = scrape_zillow(config)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc)
        log_run_finish(conn, run_id, 0, 0, 0, 0, str(exc))
        conn.close()
        return

    # 2. Hard filters + adaptive DOM filter
    filtered = apply_filters(raw, config)
    filtered = apply_dom_filter(filtered, config)

    # 3. Property detail enrichment (new listings only — garage, taxes, schools, etc.)
    if config.get('detail_enrichment', True):
        try:
            from scraper.zillow_detail import batch_enrich
            # Only enrich listings not yet in DB (or those missing key detail fields)
            cur = conn.cursor()
            existing_ids = {
                row[0] for row in cur.execute(
                    "SELECT zillow_id FROM listings WHERE year_built IS NOT NULL OR garage_spaces IS NOT NULL"
                ).fetchall()
            }
            to_enrich = [l for l in filtered if l['zillow_id'] not in existing_ids]
            logger.info("Detail enrichment: %d new listings to enrich", len(to_enrich))
            batch_enrich(to_enrich, config)
        except Exception as exc:
            logger.warning("Detail enrichment failed: %s", exc)
            errors.append(f"detail: {exc}")

    # 4. Flood enrichment
    try:
        filtered = enrich_flood_data(filtered)
    except Exception as exc:
        logger.warning("Flood enrichment failed: %s", exc)
        errors.append(f"flood: {exc}")

    # 5. Persist to DB
    seen_ids = set()
    new_count = 0
    drop_count = 0
    relist_count = 0
    alert_msgs = []

    for listing in filtered:
        zid = listing['zillow_id']
        seen_ids.add(zid)

        # Relisting detection
        prev_id, days_dark = detect_relisting(conn, listing)
        if prev_id:
            listing['relisted_from_id'] = prev_id
            listing['days_off_market'] = days_dark
            relist_count += 1
            logger.info(
                "RELISTING: %s (prev %s, off market %d days)", zid, prev_id, days_dark
            )

        change = upsert_listing(conn, listing)

        if change['is_new']:
            new_count += 1

        if change['price_dropped']:
            drop_count += 1
            pct = change['drop_pct']
            usd = change['drop_usd']
            cfg_a = config.get('alerts', {})
            if (pct >= cfg_a.get('price_drop_pct', 5.0) or
                    usd >= cfg_a.get('price_drop_min_usd', 10000)):
                msg = (
                    f"PRICE DROP: {listing['address']} — "
                    f"${usd:,} ({pct:.1f}%) → now ${listing['price']:,}"
                )
                logger.warning(msg)
                alert_msgs.append(msg)

        # Flood data
        flood_keys = ('flood_zone', 'bfe', 'ffe', 'freeboard', 'flood_risk_label')
        if any(listing.get(k) is not None for k in flood_keys):
            update_flood_data(conn, zid, {k: listing.get(k) for k in flood_keys})

        # Detail data (if enriched)
        detail_keys = (
            'year_built', 'zestimate', 'tax_annual', 'garage_spaces', 'has_garage',
            'parking_type', 'school_elementary', 'school_middle', 'school_high',
            'has_virtual_tour', 'open_house_start', 'listing_agent', 'price_history_raw',
        )
        if any(listing.get(k) is not None for k in detail_keys):
            update_detail_data(conn, zid, {k: listing.get(k) for k in detail_keys})

    # 6. Mark listings no longer on Zillow as inactive
    gone = mark_inactive(conn, seen_ids)
    if gone:
        logger.info("Marked %d listings inactive (off Zillow)", len(gone))

    # 7. ML re-scoring
    try:
        labeled = get_labeled_listings(conn)
        all_active = get_active_listings(conn, limit=5000)
        scores = score_all(labeled, all_active)
        for zid, score in scores.items():
            update_ml_score(conn, zid, score)
    except Exception as exc:
        logger.warning("ML scoring failed: %s", exc)
        errors.append(f"ml: {exc}")

    log_run_finish(
        conn, run_id, len(filtered), new_count, drop_count, relist_count,
        '; '.join(errors) if errors else None,
    )

    logger.info(
        "=== Run #%d complete: %d found, %d new, %d price drops, %d relistings ===",
        run_id, len(filtered), new_count, drop_count, relist_count,
    )
    if alert_msgs:
        logger.warning("--- PRICE DROP ALERTS ---")
        for msg in alert_msgs:
            logger.warning(msg)

    conn.close()


def main():
    parser = argparse.ArgumentParser(description='Zillow Houston Scraper')
    parser.add_argument('--now', action='store_true', help='Run once immediately')
    args = parser.parse_args()

    config = load_config()
    sys.path.insert(0, BASE_DIR)

    if args.now:
        run_full_scrape(config)
        return

    import schedule
    import time

    interval = config['scheduler']['interval_days']
    logger.info("Scheduler starting — will run every %d days", interval)
    run_full_scrape(config)
    schedule.every(interval).days.do(run_full_scrape, config)

    while True:
        schedule.run_pending()
        time.sleep(3600)


if __name__ == '__main__':
    main()
