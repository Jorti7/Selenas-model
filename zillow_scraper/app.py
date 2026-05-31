"""
Flask web dashboard for browsing and managing Zillow listings.
"""

import os
import sys
import yaml
from flask import Flask, render_template, request, jsonify, abort

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

app = Flask(__name__, template_folder='templates', static_folder='static')


def load_config() -> dict:
    with open(os.path.join(BASE_DIR, 'config.yaml')) as f:
        return yaml.safe_load(f)


def get_db():
    from database.db import get_conn, init_db
    config = load_config()
    init_db(config)
    return get_conn(config), config


@app.route('/')
def dashboard():
    conn, config = get_db()
    from database.db import get_active_listings, get_scrape_stats, count_active

    sort = request.args.get('sort', 'ml_score')
    page = max(1, int(request.args.get('page', 1)))
    per_page = 24
    offset = (page - 1) * per_page

    filters = {}
    if request.args.get('min_price'):
        filters['min_price'] = int(request.args.get('min_price'))
    if request.args.get('max_price'):
        filters['max_price'] = int(request.args.get('max_price'))
    if request.args.get('flood_risk'):
        filters['flood_risk'] = request.args.get('flood_risk')
    if request.args.get('max_hoa'):
        filters['max_hoa'] = int(request.args.get('max_hoa'))
    if request.args.get('min_beds'):
        filters['min_beds'] = int(request.args.get('min_beds'))

    listings = get_active_listings(conn, sort=sort, limit=per_page, offset=offset, filters=filters)
    total = count_active(conn)
    stats = get_scrape_stats(conn)
    conn.close()

    return render_template(
        'dashboard.html',
        listings=listings,
        total=total,
        page=page,
        per_page=per_page,
        sort=sort,
        filters=filters,
        stats=stats,
        config=config,
    )


@app.route('/listing/<zillow_id>')
def listing_detail(zillow_id: str):
    conn, config = get_db()
    from database.db import get_listing, get_price_history
    listing = get_listing(conn, zillow_id)
    if not listing:
        conn.close()
        abort(404)
    history = get_price_history(conn, zillow_id)
    conn.close()
    return render_template('listing_detail.html', listing=listing, history=history, config=config)


@app.route('/api/feedback/<zillow_id>', methods=['POST'])
def feedback(zillow_id: str):
    data = request.get_json()
    action = data.get('action')
    if action not in ('like', 'dislike', 'neutral'):
        return jsonify({'error': 'Invalid action'}), 400

    score_map = {'like': 1, 'dislike': -1, 'neutral': 0}
    score = score_map[action]

    conn, config = get_db()
    from database.db import set_user_score, get_labeled_listings, get_active_listings, update_ml_score
    from learner.scorer import score_all

    set_user_score(conn, zillow_id, score)

    # Re-score after each feedback
    labeled = get_labeled_listings(conn)
    all_active = get_active_listings(conn, limit=5000)
    scores = score_all(labeled, all_active)
    for zid, s in scores.items():
        update_ml_score(conn, zid, s)

    new_score = scores.get(zillow_id, 0.5)
    conn.close()
    return jsonify({'ok': True, 'new_ml_score': new_score})


@app.route('/api/run-scraper', methods=['POST'])
def trigger_scrape():
    import threading
    from run_scraper import run_full_scrape
    config = load_config()
    t = threading.Thread(target=run_full_scrape, args=(config,), daemon=True)
    t.start()
    return jsonify({'ok': True, 'message': 'Scrape started in background'})


@app.route('/api/stats')
def stats():
    conn, _ = get_db()
    from database.db import get_scrape_stats, count_active
    result = {
        'total_active': count_active(conn),
        'recent_runs': get_scrape_stats(conn),
    }
    conn.close()
    return jsonify(result)


if __name__ == '__main__':
    cfg = load_config()
    app.run(
        host=cfg['flask']['host'],
        port=cfg['flask']['port'],
        debug=cfg['flask']['debug'],
    )
