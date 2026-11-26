"""SQLite: wishlist, seen list, domain memory, settings, taste log, brand map."""
import json
import os
import sqlite3
import threading

DB_PATH = os.environ.get("STYLE_FINDER_DB", "finder.db")
if os.path.dirname(DB_PATH):
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)  # e.g. a Railway volume at /data
db = sqlite3.connect(DB_PATH, check_same_thread=False)
_lock = threading.Lock()  # searches run in worker threads

with _lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS items (
        id INTEGER PRIMARY KEY, chat_id INTEGER, card TEXT,
        strictness TEXT DEFAULT 'lineage', active INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS seen (
        item_id INTEGER, url TEXT, price REAL, label TEXT,
        PRIMARY KEY (item_id, url));
    CREATE TABLE IF NOT EXISTS domains (
        domain TEXT PRIMARY KEY, hits INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS taste (
        id INTEGER PRIMARY KEY, brand TEXT, silhouette TEXT, color TEXT,
        material TEXT, price_tier TEXT, category TEXT);
    CREATE TABLE IF NOT EXISTS brand_map (
        a TEXT, b TEXT, score REAL, PRIMARY KEY (a, b));
    """)
    db.commit()


def _exec(sql, args=()):
    with _lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur


def _rows(sql, args=()):
    with _lock:
        return db.execute(sql, args).fetchall()


# --- wishlist ---------------------------------------------------------------

def save_item(chat_id, card, strictness="lineage"):
    return _exec("INSERT INTO items (chat_id, card, strictness) VALUES (?, ?, ?)",
                 (chat_id, json.dumps(card), strictness)).lastrowid


def all_items():
    rows = _rows("SELECT id, chat_id, card, strictness FROM items WHERE active=1")
    return [(r[0], r[1], json.loads(r[2]), r[3]) for r in rows]


def items_for_chat(chat_id):
    return [i for i in all_items() if i[1] == chat_id]


def set_strictness(item_id, chat_id, strictness):
    return _exec("UPDATE items SET strictness=? WHERE id=? AND chat_id=?",
                 (strictness, item_id, chat_id)).rowcount > 0


def remove_item(item_id, chat_id):
    return _exec("UPDATE items SET active=0 WHERE id=? AND chat_id=?",
                 (item_id, chat_id)).rowcount > 0


# --- seen list --------------------------------------------------------------

def seen_status(item_id, url, price):
    """'new', 'cheaper', or None (already seen, no price drop)."""
    row = _rows("SELECT price FROM seen WHERE item_id=? AND url=?", (item_id, url))
    if not row:
        return "new"
    old = row[0][0]
    if price is not None and old is not None and price < old:
        return "cheaper"
    return None


def mark_seen(item_id, url, price, label):
    _exec("INSERT OR REPLACE INTO seen VALUES (?, ?, ?, ?)", (item_id, url, price, label))


# --- domain memory ----------------------------------------------------------

def promote_domain(domain):
    _exec("INSERT INTO domains (domain, hits) VALUES (?, 1) "
          "ON CONFLICT(domain) DO UPDATE SET hits = hits + 1", (domain,))


def watched_domains(limit=30):
    return [r[0] for r in _rows(
        "SELECT domain FROM domains ORDER BY hits DESC LIMIT ?", (limit,))]


# --- taste profile and brand map -------------------------------------------

def log_taste(card):
    _exec("INSERT INTO taste (brand, silhouette, color, material, price_tier, category) "
          "VALUES (?,?,?,?,?,?)",
          tuple((card.get(k) or "").lower() for k in
                ("brand", "silhouette", "color", "material", "price_tier", "category")))


def taste_profile(limit=5):
    out = {}
    for col in ("brand", "silhouette", "color", "material", "price_tier"):
        out[col] = _rows(
            f"SELECT {col}, COUNT(*) c FROM taste WHERE {col} != '' "
            f"GROUP BY {col} ORDER BY c DESC LIMIT ?", (limit,))
    return out


def set_neighbors(brand, neighbors):
    """neighbors: iterable of (other_brand, score 0-1)."""
    with _lock:
        for other, score in neighbors:
            for a, b in ((brand, other), (other, brand)):
                db.execute("INSERT OR REPLACE INTO brand_map VALUES (?,?,?)",
                           (a.lower().strip(), b.lower().strip(), float(score)))
        db.commit()


def get_neighbors(brand, min_score=0.5):
    return _rows("SELECT b, score FROM brand_map WHERE a=? AND score>=? ORDER BY score DESC",
                 ((brand or "").lower().strip(), min_score))


def known_brand(brand):
    return bool(_rows("SELECT 1 FROM brand_map WHERE a=? LIMIT 1",
                      ((brand or "").lower().strip(),)))


# --- feedback (👍 / 👎 in chat) ---------------------------------------------

with _lock:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS results (
        id INTEGER PRIMARY KEY, item_id INTEGER, url TEXT, title TEXT, brand TEXT,
        domain TEXT, label TEXT, price REAL, currency TEXT);
    CREATE TABLE IF NOT EXISTS feedback (
        id INTEGER PRIMARY KEY, item_id INTEGER, result_id INTEGER, vote INTEGER,
        reason TEXT, ts DATETIME DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS blocked_domains (domain TEXT PRIMARY KEY);
    """)
    db.commit()


def save_result(item_id, r):
    return _exec("INSERT INTO results (item_id, url, title, brand, domain, label, price, currency) "
                 "VALUES (?,?,?,?,?,?,?,?)",
                 (item_id, r["url"], r.get("title"), r.get("brand"), r.get("domain"),
                  r.get("label"), r.get("price"), r.get("currency"))).lastrowid


def get_result(result_id):
    row = _rows("SELECT id, item_id, url, title, brand, domain, label, price, currency "
                "FROM results WHERE id=?", (result_id,))
    if not row:
        return None
    keys = ("id", "item_id", "url", "title", "brand", "domain", "label", "price", "currency")
    return dict(zip(keys, row[0]))


def get_item(item_id):
    row = _rows("SELECT id, chat_id, card, strictness FROM items WHERE id=?", (item_id,))
    return (row[0][0], row[0][1], json.loads(row[0][2]), row[0][3]) if row else None


def add_feedback(item_id, result_id, vote, reason=None):
    """vote: 1 or -1. A later vote or reason on the same result replaces the earlier one."""
    with _lock:
        db.execute("DELETE FROM feedback WHERE result_id=?", (result_id,))
        db.execute("INSERT INTO feedback (item_id, result_id, vote, reason) VALUES (?,?,?,?)",
                   (item_id, result_id, vote, reason))
        db.commit()


def feedback_examples(item_id, limit=12):
    """Recent votes: this item's first, then other items' (for general taste)."""
    rows = _rows(
        "SELECT f.item_id = ? AS same, f.vote, f.reason, r.title, r.brand, r.domain, "
        "r.label, r.price, r.currency FROM feedback f JOIN results r ON r.id = f.result_id "
        "ORDER BY same DESC, f.id DESC LIMIT ?", (item_id, limit))
    return [{"for_this_item": bool(s), "vote": "up" if v > 0 else "down", "reason": why,
             "title": t, "brand": b, "shop": d, "label": lab, "price": p, "currency": c}
            for s, v, why, t, b, d, lab, p, c in rows]


def adjust_neighbor(a, b, delta):
    """Nudge how close two brands are (both directions), clamped to 0-1."""
    if not a or not b or a.lower().strip() == b.lower().strip():
        return
    with _lock:
        for x, y in ((a, b), (b, a)):
            x, y = x.lower().strip(), y.lower().strip()
            row = db.execute("SELECT score FROM brand_map WHERE a=? AND b=?", (x, y)).fetchone()
            score = max(0.0, min(1.0, (row[0] if row else 0.5) + delta))
            db.execute("INSERT OR REPLACE INTO brand_map VALUES (?,?,?)", (x, y, score))
        db.commit()


def block_domain(domain):
    _exec("INSERT OR IGNORE INTO blocked_domains VALUES (?)", (domain,))
    _exec("DELETE FROM domains WHERE domain=?", (domain,))


def blocked_domains():
    return {r[0] for r in _rows("SELECT domain FROM blocked_domains")}
