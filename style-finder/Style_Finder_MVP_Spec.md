# Style Finder: MVP Spec

## Goal

A personal tool. Drop in a screenshot, Instagram save, or product name. It pre-fills a product card, then continuously searches the open web for exact and same-lineage matches, and alerts only on new listings or price drops. No accounts, no social, no taste-learning beyond your own saves.

## Principles

- Fewest steps possible. Intake is the product.
- No fixed source list. Discovery happens on every search.
- Similar means same design language and price tier, not just same category.
- Build the thinnest thing that works tonight. Add embeddings and extras later.

## Interface

Telegram bot (fastest build, least clunky). Forward a screenshot or type a product name. Bot replies with a pre-filled card to confirm or correct, then results and alerts arrive as messages.

## Step 1: Intake and pre-fill

- Input: image, text, or both.
- A vision-capable LLM does structured extraction: brand, product name and likely aliases, silhouette, color, material, season if visible, price tier. OCR any visible text such as captions or tags.
- Return a short card. The user fixes only what is wrong or adds unknowns, then confirms.
- Each confirmed card lands in the wishlist and search starts in the background.

## Step 2: Search (dynamic discovery)

Run in parallel on every query:

- Web search API with the product name, each alias, brand plus silhouette plus color, and an image search if available.
- Resale and aggregator sources as additional queries, such as Grailed, Vestiaire, The RealReal, Farfetch, SSENSE. Treat them as just more domains, not a hardcoded roster.
- Any domain returned is a candidate, including brand-new boutiques.

## Step 3: Universal extraction

No per-site scrapers. For each candidate page:

- Read schema.org Product markup (title, price, currency, image, availability) when present.
- Otherwise have the LLM read the page text and pull the same fields.
- Shopify stores expose a products JSON endpoint, so try that first when the domain looks like Shopify.
- Normalize to one format: title, brand, price, currency, sizes in stock, image URL, condition, URL.

## Step 4: Verification funnel

Cheap checks first, expensive last.

1. Hard attributes: brand must match. Check model name or alias, color, material. Fail fast.
2. Style code or SKU: an exact match settles it immediately.
3. Image embeddings (CLIP or SigLIP) comparing the candidate photo to the reference. Threshold filters most noise.
4. LLM side-by-side judgment on survivors only: same product, same design lineage, or different. Also check season and condition (new, worn, deadstock).

Each result gets a label: Exact, Same model different season, or Lineage match.

## Step 5: Ranking and the strictness dial

Rank on separate signals, not one blob:

- Visual profile: last shape, toe box, sole, proportions.
- Brand neighborhood: a small map of designers who sit near each other (for example Dries Van Noten near Lemaire, Margiela, Rick Owens, Yohji). Generate it with the model once, then edit by hand. Saves refine it over time.
- Price tier as a sanity check, so a four-figure shoe never surfaces a cheap lookalike.

Per-search strictness setting: Exact only, Same lineage (default), Loosely inspired.

## Step 6: Memory and alerts

- Seen list per wishlist item. Alert only on new listings, new sellers, or price drops.
- Domain memory: any site that yields a verified match is promoted to a watched set and rechecked more often. Sites that never match fade out.
- Size set once globally. Hide anything not in stock in that size.
- Show landed cost for overseas results: converted currency plus estimated shipping and duties.
- Optional per-item target price.

## Build order for tonight

1. Telegram bot that accepts an image or text and returns a pre-filled card from a vision LLM.
2. Web search fan-out from the confirmed card.
3. Page extraction into the normalized format.
4. LLM verification with labels. Skip embeddings for now.
5. Seen list so repeats never surface twice.
6. Then: price watching, size filter, landed cost, embeddings, brand map.

## Known limits

- Only finds what search engines have indexed. Brand-new boutiques can take days to appear.
- Resale items are one-offs, so check frequency matters more there.
- Landed cost estimates will be approximate at first.

## Starter code, part 1: setup and intake

This is the Telegram bot skeleton. It accepts a photo or text, sends the image to a vision model, and replies with a pre-filled card to confirm.

**Setup**

1. In Telegram, message @BotFather, send /newbot, and copy the token it gives you.
2. Get an Anthropic API key.
3. Install dependencies: `pip install python-telegram-bot anthropic python-dotenv`
4. Create a `.env` file with TELEGRAM\_TOKEN and ANTHROPIC\_API\_KEY.

**bot.py**

```python
import os, base64, json
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes
import anthropic

load_dotenv()
client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-5"

EXTRACT_PROMPT = """You are helping someone find a fashion product online.
From the image and/or text, return ONLY JSON with these keys:
brand, product_name, aliases (list), category, silhouette, color, material,
season (or null), price_tier (budget, mid, premium, luxury), notes.
Use null for anything you cannot determine. Do not guess brands you cannot see or infer."""

def extract_card(image_bytes, text):
    content = []
    if image_bytes:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.b64encode(image_bytes).decode()}})
    content.append({"type": "text", "text": EXTRACT_PROMPT + "\n\nUser text: " + (text or "none")})
    resp = client.messages.create(model=MODEL, max_tokens=800,
                                  messages=[{"role": "user", "content": content}])
    raw = resp.content[0].text.strip().strip("`")
    if raw.startswith("json"):
        raw = raw[4:]
    return json.loads(raw)

def format_card(card):
    lines = [f"{k}: {v}" for k, v in card.items() if v]
    return "\n".join(lines)

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    image_bytes = None
    if msg.photo:
        f = await msg.photo[-1].get_file()
        image_bytes = bytes(await f.download_as_bytearray())
    text = msg.caption or msg.text
    card = extract_card(image_bytes, text)
    ctx.user_data["pending_card"] = card
    await msg.reply_text(
        "Here's what I see:\n\n" + format_card(card) +
        "\n\nReply 'ok' to start searching, or tell me what to fix.")

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Send a screenshot, a photo, or a product name.")

def main():
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()

if __name__ == "__main__":
    main()
```

Run it with `python bot.py`. Part 2 adds the confirm-and-correct step and the search fan-out.

## Starter code, part 2: confirm, correct, search

This replaces the `handle` function from part 1 and adds three new modules. Install two more packages: `pip install httpx beautifulsoup4`. For web search, use the Brave Search API (free tier, key from brave.com/search/api) and add BRAVE\_API\_KEY to your `.env`.

**Replace handle in bot.py with this**

```python
import asyncio
from search import run_search
from store import save_item

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    pending = ctx.user_data.get("pending_card")
    text = (msg.caption or msg.text or "").strip()

    # Confirm step
    if pending and not msg.photo and text.lower() in ("ok", "yes", "go"):
        item_id = save_item(update.effective_chat.id, pending)
        ctx.user_data["pending_card"] = None
        await msg.reply_text("Searching. I'll message you when I find matches.")
        asyncio.create_task(search_and_report(update, ctx, item_id, pending))
        return

    # Correction step: user typed a fix while a card is pending
    if pending and not msg.photo:
        fixed = apply_correction(pending, text)
        ctx.user_data["pending_card"] = fixed
        await msg.reply_text("Updated:\n\n" + format_card(fixed) + "\n\nReply 'ok' to search.")
        return

    # New intake
    image_bytes = None
    if msg.photo:
        f = await msg.photo[-1].get_file()
        image_bytes = bytes(await f.download_as_bytearray())
    card = extract_card(image_bytes, text)
    card["_image"] = base64.b64encode(image_bytes).decode() if image_bytes else None
    ctx.user_data["pending_card"] = card
    await msg.reply_text(
        "Here's what I see:\n\n" + format_card(card) +
        "\n\nReply 'ok' to search, or tell me what to fix.")

def apply_correction(card, text):
    prompt = ("Here is a product card as JSON:\n" + json.dumps({k: v for k, v in card.items() if k != "_image"}) +
              "\n\nThe user says: " + text +
              "\n\nReturn ONLY the corrected JSON, same keys.")
    resp = client.messages.create(model=MODEL, max_tokens=800,
                                  messages=[{"role": "user", "content": prompt}])
    raw = resp.content[0].text.strip().strip("`")
    if raw.startswith("json"):
        raw = raw[4:]
    fixed = json.loads(raw)
    fixed["_image"] = card.get("_image")
    return fixed

async def search_and_report(update, ctx, item_id, card):
    results = await asyncio.to_thread(run_search, item_id, card)
    if not results:
        await update.message.reply_text("Nothing yet. I'll keep watching.")
        return
    for r in results[:8]:
        await update.message.reply_text(
            f"[{r['label']}] {r['title']}\n{r['price']} {r['currency']}  |  {r['domain']}\n{r['url']}")
```

**store.py: wishlist and seen list (SQLite, no setup)**

```python
import sqlite3, json

db = sqlite3.connect("finder.db", check_same_thread=False)
db.execute("CREATE TABLE IF NOT EXISTS items (id INTEGER PRIMARY KEY, chat_id INTEGER, card TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS seen (item_id INTEGER, url TEXT, price REAL, PRIMARY KEY (item_id, url))")
db.execute("CREATE TABLE IF NOT EXISTS domains (domain TEXT PRIMARY KEY, hits INTEGER DEFAULT 0)")

def save_item(chat_id, card):
    cur = db.execute("INSERT INTO items (chat_id, card) VALUES (?, ?)", (chat_id, json.dumps(card)))
    db.commit()
    return cur.lastrowid

def is_new_or_cheaper(item_id, url, price):
    row = db.execute("SELECT price FROM seen WHERE item_id=? AND url=?", (item_id, url)).fetchone()
    if row is None:
        return True
    return price is not None and row[0] is not None and price < row[0]

def mark_seen(item_id, url, price):
    db.execute("INSERT OR REPLACE INTO seen VALUES (?, ?, ?)", (item_id, url, price))
    db.commit()

def promote_domain(domain):
    db.execute("INSERT INTO domains (domain, hits) VALUES (?, 1) ON CONFLICT(domain) DO UPDATE SET hits = hits + 1", (domain,))
    db.commit()

def watched_domains():
    return [r[0] for r in db.execute("SELECT domain FROM domains ORDER BY hits DESC LIMIT 30")]
```

**search.py: discovery, extraction, verification**

```python
import os, json, re
import httpx
from urllib.parse import urlparse
from bs4 import BeautifulSoup
import anthropic
from store import is_new_or_cheaper, mark_seen, promote_domain, watched_domains

client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-5"
BRAVE = os.environ.get("BRAVE_API_KEY")
HEADERS = {"User-Agent": "Mozilla/5.0"}

def build_queries(card):
    brand = card.get("brand") or ""
    names = [card.get("product_name")] + (card.get("aliases") or [])
    qs = [f"{brand} {n}".strip() for n in names if n]
    qs.append(" ".join(filter(None, [brand, card.get("color"), card.get("silhouette"), card.get("category")])))
    # also re-check domains that have matched before
    for d in watched_domains()[:5]:
        if qs:
            qs.append(f"{qs[0]} site:{d}")
    return list(dict.fromkeys(qs))[:10]

def web_search(q):
    r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                  params={"q": q, "count": 15},
                  headers={"X-Subscription-Token": BRAVE, "Accept": "application/json"}, timeout=15)
    return [x["url"] for x in r.json().get("web", {}).get("results", [])]

def extract_product(url):
    try:
        r = httpx.get(url, headers=HEADERS, timeout=15, follow_redirects=True)
    except Exception:
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    # 1. try schema.org JSON-LD
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for node in (data if isinstance(data, list) else [data]):
            if isinstance(node, dict) and node.get("@type") in ("Product", ["Product"]):
                offer = node.get("offers") or {}
                offer = offer[0] if isinstance(offer, list) and offer else offer
                img = node.get("image")
                img = img[0] if isinstance(img, list) and img else img
                return {"title": node.get("name"), "brand": (node.get("brand") or {}).get("name") if isinstance(node.get("brand"), dict) else node.get("brand"),
                        "price": offer.get("price") if isinstance(offer, dict) else None,
                        "currency": offer.get("priceCurrency") if isinstance(offer, dict) else None,
                        "image": img, "url": url, "sku": node.get("sku")}
    # 2. fall back to the model reading page text
    text = soup.get_text(" ", strip=True)[:6000]
    resp = client.messages.create(model=MODEL, max_tokens=400, messages=[{"role": "user", "content":
        "If this page sells a single product, return ONLY JSON with title, brand, price (number), currency, image, sku. "
        "If it is not a product page, return {}.\n\n" + text}])
    try:
        raw = resp.content[0].text.strip().strip("`")
        raw = raw[4:] if raw.startswith("json") else raw
        d = json.loads(raw)
        if d.get("title"):
            d["url"] = url
            return d
    except Exception:
        pass
    return None

def verify(card, product):
    ref = {k: v for k, v in card.items() if k != "_image"}
    resp = client.messages.create(model=MODEL, max_tokens=200, messages=[{"role": "user", "content":
        "Reference product:\n" + json.dumps(ref) + "\n\nCandidate listing:\n" + json.dumps(product) +
        "\n\nAnswer with exactly one label: EXACT (same product), SEASON (same model, different season), "
        "LINEAGE (same designer and design language, same price tier), or NO. "
        "Different brand is always NO. A much cheaper lookalike is NO."}])
    label = resp.content[0].text.strip().upper().split()[0]
    return label if label in ("EXACT", "SEASON", "LINEAGE") else None

def run_search(item_id, card, strictness="lineage"):
    allowed = {"exact": {"EXACT"}, "lineage": {"EXACT", "SEASON", "LINEAGE"}}[strictness]
    urls = []
    for q in build_queries(card):
        try:
            urls += web_search(q)
        except Exception:
            continue
    urls = list(dict.fromkeys(urls))[:40]
    out = []
    for url in urls:
        p = extract_product(url)
        if not p:
            continue
        try:
            price = float(re.sub(r"[^0-9.]", "", str(p.get("price") or "")) or 0) or None
        except Exception:
            price = None
        if not is_new_or_cheaper(item_id, url, price):
            continue
        label = verify(card, p)
        mark_seen(item_id, url, price)
        if label in allowed:
            domain = urlparse(url).netloc
            promote_domain(domain)
            out.append({"label": label, "title": p.get("title"), "price": price or "?",
                        "currency": p.get("currency") or "", "domain": domain, "url": url})
    order = {"EXACT": 0, "SEASON": 1, "LINEAGE": 2}
    return sorted(out, key=lambda r: (order[r["label"]], r["price"] if isinstance(r["price"], float) else 1e9))
```

That is a working end-to-end loop: photo or name in, confirm, search, verified results out, repeats suppressed. Part 3 adds the background re-check and price-drop alerts.

## Starter code, part 3: background watching and alerts

This makes the bot re-run every saved item on a schedule and message you only when something new or cheaper shows up. It uses the job queue built into python-telegram-bot, so install it with the extra: `pip install "python-telegram-bot[job-queue]"`.

**Add to store.py**

```python
def all_items():
    rows = db.execute("SELECT id, chat_id, card FROM items").fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]
```

**Add to bot.py**

```python
from store import all_items

async def recheck(ctx: ContextTypes.DEFAULT_TYPE):
    for item_id, chat_id, card in all_items():
        try:
            results = await asyncio.to_thread(run_search, item_id, card)
        except Exception:
            continue
        for r in results[:5]:
            await ctx.bot.send_message(
                chat_id,
                f"New [{r['label']}] {r['title']}\n{r['price']} {r['currency']}  |  {r['domain']}\n{r['url']}")
```

**Replace main in bot.py**

```python
def main():
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.PHOTO | filters.TEXT & ~filters.COMMAND, handle))
    app.job_queue.run_repeating(recheck, interval=6 * 3600, first=300)
    app.run_polling()
```

Because run\_search already skips anything in the seen table unless it is cheaper, the re-check only surfaces new listings, new sellers, and price drops. The watched-domains table also feeds back into the queries, so sites that have matched before get re-checked first.

**What is deliberately not in here yet**

- Size filter, target prices, and landed cost for overseas results. Add these once the loop feels right.
- Image embeddings and the brand-neighborhood map. The model's verify step covers lineage for now.
- Instagram saves. Screenshot them and forward the screenshot.

**To run it tonight**

1. Put all three files, bot.py, store.py, and search.py, in one folder with the .env file.
2. Run `python bot.py` on your laptop and leave it open.
3. Message your bot a screenshot, reply ok, and watch results arrive.

If it is not stable, the usual first fixes are the model name string and the JSON parsing of model replies. Paste any traceback into Claude Code and it will sort it out. To keep it running when your laptop is closed, a small cloud host like Railway or Fly.io works.

## Starter code, part 4: taste profile and brand map

This adds the memory layer. Every confirmed card is logged, a taste profile is derived from those logs, and a brand neighborhood map is seeded by the model and refined nightly. The map then feeds the verify step so lineage matches respect who sits near whom. Everything stays in the same SQLite file, no new packages.

**Add to store.py**

```python
db.execute("CREATE TABLE IF NOT EXISTS taste (id INTEGER PRIMARY KEY, brand TEXT, silhouette TEXT, color TEXT, material TEXT, price_tier TEXT, category TEXT)")
db.execute("CREATE TABLE IF NOT EXISTS brand_map (a TEXT, b TEXT, score REAL, PRIMARY KEY (a, b))")

def log_taste(card):
    db.execute("INSERT INTO taste (brand, silhouette, color, material, price_tier, category) VALUES (?,?,?,?,?,?)",
               tuple((card.get(k) or "").lower() for k in ("brand", "silhouette", "color", "material", "price_tier", "category")))
    db.commit()

def taste_profile(limit=5):
    out = {}
    for col in ("brand", "silhouette", "color", "material", "price_tier"):
        rows = db.execute(f"SELECT {col}, COUNT(*) c FROM taste WHERE {col} != '' GROUP BY {col} ORDER BY c DESC LIMIT ?", (limit,)).fetchall()
        out[col] = rows
    return out

def set_neighbors(brand, neighbors):
    # neighbors: list of (other_brand, score 0-1)
    for other, score in neighbors:
        for a, b in ((brand, other), (other, brand)):
            db.execute("INSERT OR REPLACE INTO brand_map VALUES (?,?,?)", (a.lower(), b.lower(), float(score)))
    db.commit()

def get_neighbors(brand, min_score=0.5):
    rows = db.execute("SELECT b, score FROM brand_map WHERE a=? AND score>=? ORDER BY score DESC", ((brand or "").lower(), min_score)).fetchall()
    return rows

def known_brand(brand):
    return db.execute("SELECT 1 FROM brand_map WHERE a=? LIMIT 1", ((brand or "").lower(),)).fetchone() is not None
```

**New file, memory.py: seeding and nightly refinement**

```python
import json
import anthropic
from store import log_taste, set_neighbors, known_brand, taste_profile, all_items, db

client = anthropic.Anthropic()
MODEL = "claude-sonnet-4-5"

def _json(text):
    raw = text.strip().strip("`")
    return json.loads(raw[4:] if raw.startswith("json") else raw)

def learn_from_card(card):
    """Call once when a card is confirmed."""
    log_taste(card)
    brand = card.get("brand")
    if brand and not known_brand(brand):
        resp = client.messages.create(model=MODEL, max_tokens=600, messages=[{"role": "user", "content":
            f"List up to 8 designers or brands that share design language and price tier with {brand}. "
            "Exclude brands that merely make the same product category cheaply. "
            "Return ONLY JSON: [[\"brand\", score_between_0_and_1], ...]"}])
        try:
            set_neighbors(brand, _json(resp.content[0].text))
        except Exception:
            pass

def nightly_refine(app_context=None):
    """Run once a day. Reviews recent saves and tunes the brand map."""
    items = [c for _, _, c in all_items()][-40:]
    if len(items) < 5:
        return
    prof = taste_profile()
    resp = client.messages.create(model=MODEL, max_tokens=900, messages=[{"role": "user", "content":
        "Here are a collector's recent saved products and a count profile of their taste.\n"
        + json.dumps([{k: v for k, v in c.items() if k != "_image"} for c in items])
        + "\n" + json.dumps(prof, default=str)
        + "\n\nSuggest brand neighbor links this taste implies that are worth adding. "
        "Return ONLY JSON: [[\"brand_a\", \"brand_b\", score_between_0_and_1], ...]"}])
    try:
        for a, b, s in _json(resp.content[0].text):
            set_neighbors(a, [(b, s)])
    except Exception:
        pass
```

**Wire it into bot.py**

```python
from memory import learn_from_card, nightly_refine

# In handle(), right after save_item(...) in the confirm step, add:
#     learn_from_card(pending)

async def nightly(ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(nightly_refine)

# In main(), next to the recheck job, add:
#     app.job_queue.run_repeating(nightly, interval=24 * 3600, first=600)
```

**Make verify use the map (search.py)**

Add `from store import get_neighbors` at the top, then replace the verify prompt's final instruction with this version:

```python
def verify(card, product):
    ref = {k: v for k, v in card.items() if k != "_image"}
    near = [b for b, _ in get_neighbors(card.get("brand"))]
    resp = client.messages.create(model=MODEL, max_tokens=200, messages=[{"role": "user", "content":
        "Reference product:\n" + json.dumps(ref) + "\n\nCandidate listing:\n" + json.dumps(product) +
        "\n\nBrands in the same design neighborhood as the reference: " + json.dumps(near) +
        "\n\nAnswer with exactly one label: EXACT (same product), SEASON (same model, different season), "
        "LINEAGE (a different brand from that neighborhood list with the same silhouette and price tier), or NO. "
        "A brand outside the neighborhood is NO. A much cheaper lookalike is NO."}])
    label = resp.content[0].text.strip().upper().split()[0]
    return label if label in ("EXACT", "SEASON", "LINEAGE") else None
```

**What you get**

- A taste profile that builds itself from your confirmations. Query it any time with `taste_profile()`.
- A brand map the model seeds the first time it sees a new designer and tunes nightly from your saves. You can also edit the brand\_map table by hand to correct it.
- Lineage matches that now mean a real neighbor brand, so a Dries derby surfaces Lemaire or Margiela, not Cole Haan.

**Still not here:** a vector store for visual search over your own saves. Add sqlite-vec or Chroma once the text-based loop feels solid.
