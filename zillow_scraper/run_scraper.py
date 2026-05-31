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
        get_labeled_listings, get_active_listings,
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

    # 1. Scrape
    try:
        raw = scrape_zillow(config)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc)
        log_run_finish(conn, run_id, 0, 0, 0, 0, str(exc))
        conn.close()
        return

    # 2. Filter
    filtered = apply_filters(raw, config)
    filtered = apply_dom_filter(filtered, config)

    # 3. Flood enrichment
    try:
        filtered = enrich_flood_data(filtered)
    except Exception as exc:
        logger.warning("Flood enrichment failed: %s", exc)
        errors.append(str(exc))

    # 4. Persist to DB
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
            logger.info("RELISTING detected: %s (was %s, off market %d days)", zid, prev_id, days_dark)

        change = upsert_listing(conn, listing)

        if change['is_new']:
            new_count += 1

        if change['price_dropped']:
            drop_count += 1
            pct = change['drop_pct']
            usd = change['drop_usd']
            cfg_a = config.get('alerts', {})
            if pct >= cfg_a.get('price_drop_pct', 5.0) or usd >= cfg_a.get('price_drop_min_usd', 10000):
                msg = (
                    f"PRICE DROP: {listing['address']} — "
                    f"dropped ${usd:,} ({pct:.1f}%) to ${listing['price']:,}"
                )
                logger.warning(msg)
                alert_msgs.append(msg)

        # Flood data update (may already be set from upsert, but explicit for existing rows)
        if any(listing.get(k) is not None for k in ('flood_zone', 'bfe', 'ffe', 'freeboard', 'flood_risk_label')):
            update_flood_data(conn, zid, {
                'flood_zone': listing.get('flood_zone'),
                'bfe': listing.get('bfe'),
                'ffe': listing.get('ffe'),
                'freeboard': listing.get('freeboard'),
                'flood_risk_label': listing.get('flood_risk_label'),
            })

    # 5. Mark inactive listings
    gone = mark_inactive(conn, seen_ids)
    if gone:
        logger.info("Marked %d listings as inactive (off Zillow)", len(gone))

    # 6. ML re-scoring
    try:
        labeled = get_labeled_listings(conn)
        all_active = get_active_listings(conn, limit=5000)
        scores = score_all(labeled, all_active)
        for zid, score in scores.items():
            update_ml_score(conn, zid, score)
    except Exception as exc:
        logger.warning("ML scoring failed: %s", exc)
        errors.append(str(exc))

    log_run_finish(conn, run_id, len(filtered), new_count, drop_count, relist_count,
                   '; '.join(errors) if errors else None)

    logger.info(
        "=== Run #%d complete: %d found, %d new, %d price drops, %d relistings ===",
        run_id, len(filtered), new_count, drop_count, relist_count
    )

    if alert_msgs:
        logger.warning("--- ALERTS ---")
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

    # Run immediately on first start, then schedule
    run_full_scrape(config)
    schedule.every(interval).days.do(run_full_scrape, config)

    while True:
        schedule.run_pending()
        time.sleep(3600)


if __name__ == '__main__':
    main()
