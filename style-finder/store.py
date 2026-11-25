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
