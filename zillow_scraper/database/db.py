import sqlite3
import os
from datetime import datetime, timezone


def get_db_path(config):
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_dir, config['database']['path'])


def get_conn(config):
    path = get_db_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(config):
    conn = get_conn(config)
    cur = conn.cursor()
    cur.executescript("""
        CREATE TABLE IF NOT EXISTS listings (
            zillow_id           TEXT PRIMARY KEY,
            address             TEXT,
            city                TEXT,
            zip_code            TEXT,
            lat                 REAL,
            lon                 REAL,
            price               INTEGER,
            beds                INTEGER,
            baths               REAL,
            living_sqft         INTEGER,
            lot_sqft            INTEGER,
            year_built          INTEGER,
            hoa_monthly         INTEGER,
            dom                 INTEGER,
            list_date           TEXT,
            property_type       TEXT,
            flood_zone          TEXT,
            bfe                 REAL,
            ffe                 REAL,
            freeboard           REAL,
            flood_risk_label    TEXT,
            listing_url         TEXT,
            thumbnail_url       TEXT,
            first_seen          TIMESTAMP,
            last_seen           TIMESTAMP,
            is_active           INTEGER DEFAULT 1,
            relisted_from_id    TEXT,
            days_off_market     INTEGER,
            user_score          INTEGER,
            ml_score            REAL DEFAULT 0.5,
            zestimate           INTEGER,
            price_reduction     INTEGER,
            listing_sub_type    TEXT,
            broker_name         TEXT,
            has_3d_tour         INTEGER DEFAULT 0,
            garage_spaces       INTEGER,
            has_garage          INTEGER,
            parking_type        TEXT,
            tax_annual          INTEGER,
            school_elementary   TEXT,
            school_middle       TEXT,
            school_high         TEXT,
            has_virtual_tour    INTEGER DEFAULT 0,
            open_house_start    TEXT,
            listing_agent       TEXT
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            zillow_id   TEXT NOT NULL,
            price       INTEGER NOT NULL,
            event       TEXT,
            event_date  TEXT,
            recorded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (zillow_id) REFERENCES listings(zillow_id)
        );

        CREATE TABLE IF NOT EXISTS scrape_runs (
            run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at      TIMESTAMP,
            finished_at     TIMESTAMP,
            listings_found  INTEGER DEFAULT 0,
            new_listings    INTEGER DEFAULT 0,
            price_drops     INTEGER DEFAULT 0,
            relistings      INTEGER DEFAULT 0,
            errors          TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_listings_active ON listings(is_active);
        CREATE INDEX IF NOT EXISTS idx_listings_ml_score ON listings(ml_score DESC);
        CREATE INDEX IF NOT EXISTS idx_price_history_zpid ON price_history(zillow_id);
    """)

    # Migrate existing DB: add new columns if they don't exist yet
    new_columns = [
        ("zestimate",          "INTEGER"),
        ("price_reduction",    "INTEGER"),
        ("listing_sub_type",   "TEXT"),
        ("broker_name",        "TEXT"),
        ("has_3d_tour",        "INTEGER DEFAULT 0"),
        ("garage_spaces",      "INTEGER"),
        ("has_garage",         "INTEGER"),
        ("parking_type",       "TEXT"),
        ("tax_annual",         "INTEGER"),
        ("school_elementary",  "TEXT"),
        ("school_middle",      "TEXT"),
        ("school_high",        "TEXT"),
        ("has_virtual_tour",   "INTEGER DEFAULT 0"),
        ("open_house_start",   "TEXT"),
        ("listing_agent",      "TEXT"),
    ]
    existing = {row[1] for row in cur.execute("PRAGMA table_info(listings)").fetchall()}
    for col_name, col_type in new_columns:
        if col_name not in existing:
            cur.execute(f"ALTER TABLE listings ADD COLUMN {col_name} {col_type}")

    # Add event/event_date columns to price_history if missing
    ph_existing = {row[1] for row in cur.execute("PRAGMA table_info(price_history)").fetchall()}
    if "event" not in ph_existing:
        cur.execute("ALTER TABLE price_history ADD COLUMN event TEXT")
    if "event_date" not in ph_existing:
        cur.execute("ALTER TABLE price_history ADD COLUMN event_date TEXT")

    conn.commit()
    conn.close()


_INSERT_COLS = (
    "zillow_id, address, city, zip_code, lat, lon, price, beds, baths, "
    "living_sqft, lot_sqft, year_built, hoa_monthly, dom, list_date, property_type, "
    "listing_url, thumbnail_url, first_seen, last_seen, is_active, "
    "zestimate, price_reduction, listing_sub_type, broker_name, has_3d_tour"
)
_INSERT_VALS = (
    ":zillow_id, :address, :city, :zip_code, :lat, :lon, :price, :beds, :baths, "
    ":living_sqft, :lot_sqft, :year_built, :hoa_monthly, :dom, :list_date, :property_type, "
    ":listing_url, :thumbnail_url, :first_seen, :last_seen, 1, "
    ":zestimate, :price_reduction, :listing_sub_type, :broker_name, :has_3d_tour"
)


def upsert_listing(conn, listing: dict) -> dict:
    """Insert or update a listing. Returns metadata about what changed."""
    now = datetime.now(timezone.utc)
    cur = conn.cursor()
    cur.execute("SELECT * FROM listings WHERE zillow_id = ?", (listing['zillow_id'],))
    existing = cur.fetchone()

    result = {'is_new': False, 'price_dropped': False, 'drop_pct': 0.0, 'drop_usd': 0}

    # Ensure new optional fields have defaults for the SQL bind
    row = {
        'zestimate': None, 'price_reduction': None,
        'listing_sub_type': 'standard', 'broker_name': '',
        'has_3d_tour': 0, 'first_seen': now, 'last_seen': now,
        **listing,
    }

    if existing is None:
        result['is_new'] = True
        cur.execute(f"INSERT INTO listings ({_INSERT_COLS}) VALUES ({_INSERT_VALS})", row)
        if listing.get('price'):
            cur.execute(
                "INSERT INTO price_history (zillow_id, price, recorded_at) VALUES (?, ?, ?)",
                (listing['zillow_id'], listing['price'], now)
            )
    else:
        old_price = existing['price']
        new_price = listing.get('price')
        if new_price and new_price != old_price:
            cur.execute(
                "INSERT INTO price_history (zillow_id, price, recorded_at) VALUES (?, ?, ?)",
                (listing['zillow_id'], new_price, now)
            )
            if new_price < old_price:
                drop_usd = old_price - new_price
                drop_pct = (drop_usd / old_price) * 100
                result['price_dropped'] = True
                result['drop_usd'] = drop_usd
                result['drop_pct'] = drop_pct

        cur.execute("""
            UPDATE listings
            SET price=:price, beds=:beds, baths=:baths, living_sqft=:living_sqft,
                lot_sqft=:lot_sqft, hoa_monthly=:hoa_monthly, dom=:dom,
                thumbnail_url=:thumbnail_url, last_seen=:last_seen, is_active=1,
                zestimate=COALESCE(:zestimate, zestimate),
                price_reduction=COALESCE(:price_reduction, price_reduction),
                listing_sub_type=COALESCE(:listing_sub_type, listing_sub_type),
                broker_name=COALESCE(:broker_name, broker_name),
                has_3d_tour=COALESCE(:has_3d_tour, has_3d_tour)
            WHERE zillow_id=:zillow_id
        """, {**row, 'last_seen': now})

    conn.commit()
    return result


def update_detail_data(conn, zillow_id: str, detail: dict):
    """Persist property detail enrichment fields."""
    cur = conn.cursor()
    cur.execute("""
        UPDATE listings SET
            year_built      = COALESCE(:year_built, year_built),
            zestimate       = COALESCE(:zestimate, zestimate),
            tax_annual      = COALESCE(:tax_annual, tax_annual),
            garage_spaces   = COALESCE(:garage_spaces, garage_spaces),
            has_garage      = COALESCE(:has_garage, has_garage),
            parking_type    = COALESCE(:parking_type, parking_type),
            school_elementary = COALESCE(:school_elementary, school_elementary),
            school_middle   = COALESCE(:school_middle, school_middle),
            school_high     = COALESCE(:school_high, school_high),
            has_virtual_tour = COALESCE(:has_virtual_tour, has_virtual_tour),
            open_house_start = COALESCE(:open_house_start, open_house_start),
            listing_agent   = COALESCE(:listing_agent, listing_agent)
        WHERE zillow_id = :zillow_id
    """, {**detail, 'zillow_id': zillow_id})

    # Insert historical price events from the detail page
    for h in detail.get('price_history_raw', []):
        if not h.get('price'):
            continue
        # Only insert if this event date isn't already recorded
        existing = cur.execute(
            "SELECT 1 FROM price_history WHERE zillow_id=? AND event_date=? AND price=?",
            (zillow_id, h['date'], h['price'])
        ).fetchone()
        if not existing:
            cur.execute(
                "INSERT INTO price_history (zillow_id, price, event, event_date) VALUES (?,?,?,?)",
                (zillow_id, h['price'], h.get('event', ''), h.get('date', ''))
            )

    conn.commit()


def mark_inactive(conn, zillow_ids_seen: set):
    """Mark any previously active listing not seen in this run as inactive."""
    cur = conn.cursor()
    cur.execute("SELECT zillow_id FROM listings WHERE is_active=1")
    all_active = {row['zillow_id'] for row in cur.fetchall()}
    gone = all_active - zillow_ids_seen
    if gone:
        now = datetime.now(timezone.utc)
        for zid in gone:
            cur.execute(
                "UPDATE listings SET is_active=0, last_seen=? WHERE zillow_id=?",
                (now, zid)
            )
    conn.commit()
    return gone


def detect_relisting(conn, listing: dict) -> str | None:
    """
    Check if a new listing is actually a relisted property.
    Returns the previous zillow_id if a match is found, else None.
    """
    cur = conn.cursor()
    cur.execute("""
        SELECT zillow_id, last_seen
        FROM listings
        WHERE address = ? AND is_active = 0
        ORDER BY last_seen DESC
        LIMIT 1
    """, (listing['address'],))
    row = cur.fetchone()
    if row:
        last_seen = row['last_seen']
        if isinstance(last_seen, str):
            last_seen = datetime.fromisoformat(last_seen)
        if last_seen.tzinfo is None:
            last_seen = last_seen.replace(tzinfo=timezone.utc)
        days_dark = (datetime.now(timezone.utc) - last_seen).days
        if days_dark <= 365:
            return row['zillow_id'], days_dark
    return None, 0


def update_flood_data(conn, zillow_id: str, flood_data: dict):
    cur = conn.cursor()
    cur.execute("""
        UPDATE listings
        SET flood_zone=:flood_zone, bfe=:bfe, ffe=:ffe,
            freeboard=:freeboard, flood_risk_label=:flood_risk_label
        WHERE zillow_id=:zillow_id
    """, {**flood_data, 'zillow_id': zillow_id})
    conn.commit()


def update_ml_score(conn, zillow_id: str, score: float):
    cur = conn.cursor()
    cur.execute("UPDATE listings SET ml_score=? WHERE zillow_id=?", (score, zillow_id))
    conn.commit()


def set_user_score(conn, zillow_id: str, score: int):
    """score: 1=like, -1=dislike, 0=neutral"""
    cur = conn.cursor()
    cur.execute("UPDATE listings SET user_score=? WHERE zillow_id=?", (score, zillow_id))
    conn.commit()


def get_active_listings(conn, sort='ml_score', limit=200, offset=0, filters=None):
    filters = filters or {}
    wheres = ['is_active=1']
    params = []

    if 'min_price' in filters:
        wheres.append('price >= ?')
        params.append(filters['min_price'])
    if 'max_price' in filters:
        wheres.append('price <= ?')
        params.append(filters['max_price'])
    if 'flood_risk' in filters:
        wheres.append('flood_risk_label = ?')
        params.append(filters['flood_risk'])
    if 'max_hoa' in filters:
        wheres.append('(hoa_monthly IS NULL OR hoa_monthly <= ?)')
        params.append(filters['max_hoa'])
    if 'min_beds' in filters:
        wheres.append('beds >= ?')
        params.append(filters['min_beds'])

    allowed_sorts = {'ml_score', 'price', 'dom', 'first_seen', 'lot_sqft'}
    sort_col = sort if sort in allowed_sorts else 'ml_score'
    direction = 'ASC' if sort_col in ('price', 'dom') else 'DESC'

    sql = f"""
        SELECT * FROM listings
        WHERE {' AND '.join(wheres)}
        ORDER BY {sort_col} {direction}
        LIMIT ? OFFSET ?
    """
    params.extend([limit, offset])
    cur = conn.cursor()
    cur.execute(sql, params)
    return [dict(row) for row in cur.fetchall()]


def get_listing(conn, zillow_id: str):
    cur = conn.cursor()
    cur.execute("SELECT * FROM listings WHERE zillow_id=?", (zillow_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def get_price_history(conn, zillow_id: str):
    cur = conn.cursor()
    cur.execute(
        "SELECT price, recorded_at FROM price_history WHERE zillow_id=? ORDER BY recorded_at ASC",
        (zillow_id,)
    )
    return [dict(row) for row in cur.fetchall()]


def get_labeled_listings(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT zillow_id, price, beds, baths, living_sqft, lot_sqft, hoa_monthly,
               dom, year_built, flood_risk_label, user_score
        FROM listings
        WHERE user_score IS NOT NULL
    """)
    return [dict(row) for row in cur.fetchall()]


def get_scrape_stats(conn):
    cur = conn.cursor()
    cur.execute("""
        SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT 10
    """)
    return [dict(row) for row in cur.fetchall()]


def count_active(conn):
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) as n FROM listings WHERE is_active=1")
    return cur.fetchone()['n']


def log_run_start(conn) -> int:
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO scrape_runs (started_at) VALUES (?)",
        (datetime.now(timezone.utc),)
    )
    conn.commit()
    return cur.lastrowid


def log_run_finish(conn, run_id: int, found: int, new: int, drops: int, relistings: int, errors: str = None):
    cur = conn.cursor()
    cur.execute("""
        UPDATE scrape_runs
        SET finished_at=?, listings_found=?, new_listings=?, price_drops=?, relistings=?, errors=?
        WHERE run_id=?
    """, (datetime.now(timezone.utc), found, new, drops, relistings, errors, run_id))
    conn.commit()
