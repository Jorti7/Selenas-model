"""
Preference learning and scoring.

Phase 1 (< 5 labeled): weighted heuristic scoring.
Phase 2 (>= 5 labeled): RandomForest trained on user labels.
Model persisted to data/model.pkl.
"""

import os
import pickle
import logging
import numpy as np

logger = logging.getLogger(__name__)

MODEL_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data', 'model.pkl')

_FLOOD_SCORE = {
    'Minimal': 1.0,
    'Low': 0.85,
    'Moderate': 0.5,
    'High': 0.2,
    'Very High': 0.0,
    'Unknown': 0.6,
    None: 0.6,
}

_PRICE_MID = 350_000
_PRICE_RANGE = 100_000


def _features(listing: dict) -> list[float]:
    price = listing.get('price') or _PRICE_MID
    beds = listing.get('beds') or 3
    baths = listing.get('baths') or 2
    living = listing.get('living_sqft') or 1500
    lot = listing.get('lot_sqft') or 5000
    hoa = listing.get('hoa_monthly') or 0
    dom = listing.get('dom') or 45
    year = listing.get('year_built') or 1990
    flood_score = _FLOOD_SCORE.get(listing.get('flood_risk_label'), 0.6)

    return [
        # Normalised price proximity to budget midpoint (higher = closer to middle)
        max(0, 1 - abs(price - _PRICE_MID) / _PRICE_RANGE),
        min(beds / 5, 1.0),
        min(baths / 4, 1.0),
        min(living / 3000, 1.0),
        min(lot / 20000, 1.0),
        max(0, 1 - hoa / 300),           # lower HOA = better
        max(0, 1 - dom / 90),            # fresher listing = better
        min((year - 1960) / 60, 1.0),    # newer = better
        flood_score,
    ]


def heuristic_score(listing: dict) -> float:
    f = _features(listing)
    weights = [0.20, 0.15, 0.10, 0.15, 0.10, 0.10, 0.05, 0.05, 0.10]
    return round(sum(w * v for w, v in zip(weights, f)), 4)


def train_and_score(labeled: list[dict], all_listings: list[dict]) -> dict[str, float]:
    """
    Train a classifier on labeled listings and return score dict keyed by zillow_id.
    Falls back to heuristic if not enough data.
    """
    positives = [l for l in labeled if l.get('user_score') == 1]
    negatives = [l for l in labeled if l.get('user_score') == -1]

    if len(positives) < 3 or len(negatives) < 2:
        logger.info("Not enough labels yet (%d pos, %d neg) — using heuristic", len(positives), len(negatives))
        return {lst['zillow_id']: heuristic_score(lst) for lst in all_listings}

    try:
        from sklearn.ensemble import RandomForestClassifier
        from sklearn.preprocessing import StandardScaler
        from sklearn.pipeline import Pipeline

        X_train = np.array([_features(l) for l in labeled])
        y_train = np.array([1 if l['user_score'] == 1 else 0 for l in labeled])

        model = Pipeline([
            ('scaler', StandardScaler()),
            ('clf', RandomForestClassifier(n_estimators=100, random_state=42)),
        ])
        model.fit(X_train, y_train)

        os.makedirs(os.path.dirname(MODEL_PATH), exist_ok=True)
        with open(MODEL_PATH, 'wb') as f:
            pickle.dump(model, f)

        X_all = np.array([_features(lst) for lst in all_listings])
        probs = model.predict_proba(X_all)[:, 1]

        return {lst['zillow_id']: round(float(p), 4) for lst, p in zip(all_listings, probs)}

    except Exception as exc:
        logger.error("ML training failed: %s — falling back to heuristic", exc)
        return {lst['zillow_id']: heuristic_score(lst) for lst in all_listings}


def score_all(labeled: list[dict], all_listings: list[dict]) -> dict[str, float]:
    return train_and_score(labeled, all_listings)
