"""Discovery, universal extraction, verification funnel, ranking."""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from llm import LLMError, ask_json, web_search_urls
from store import (blocked_domains, feedback_examples, get_neighbors, mark_seen,
                   promote_domain, seen_status, watched_domains)

BRAVE = os.environ.get("BRAVE_API_KEY")
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"),
           "Accept-Language": "en-US,en;q=0.9"}
# Pages that are never a single listing; skip without fetching.
SKIP_DOMAINS = ("instagram.com", "pinterest.", "reddit.com", "youtube.com",
                "tiktok.com", "facebook.com", "x.com", "twitter.com", "wikipedia.org")
MAX_URLS = int(os.environ.get("STYLE_FINDER_MAX_URLS", "150"))
MAX_PER_SHOP = int(os.environ.get("STYLE_FINDER_MAX_PER_SHOP", "4"))  # keeps one shop from crowding out the rest
MAX_QUERIES = int(os.environ.get("STYLE_FINDER_MAX_QUERIES", "30"))
# Phrasings that pull in different kinds of sellers: retail, sale, resale, deadstock.
INTENTS = ["buy", "sale", "in stock", "pre-owned", "used", "resale", "deadstock",
           "new with tags", "boutique", "store"]

STRICTNESS = {
    "exact": {"EXACT"},
    "lineage": {"EXACT", "SEASON", "LINEAGE"},
    "loose": {"EXACT", "SEASON", "LINEAGE", "INSPIRED"},
}
LABEL_ORDER = {"EXACT": 0, "SEASON": 1, "LINEAGE": 2, "INSPIRED": 3}
TIER_RANK = {"budget": 0, "mid": 1, "premium": 2, "luxury": 3}


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


# --- Step 2: discovery ------------------------------------------------------

def build_queries(card):
    """No fixed shop list: many phrasings so any shop that has it can surface."""
    brand = card.get("brand") or ""
    names = [card.get("product_name")] + (card.get("aliases") or [])
    qs = [f"{brand} {n}".strip() for n in names if n]
    code = card.get("style_code")
    if code:
        qs[:0] = [f"{brand} {code}".strip(), code]  # a bare style code finds resale and overseas shops
    qs.append(" ".join(filter(None, [brand, card.get("color"),
                                     card.get("silhouette"), card.get("category")])))
    qs = [q for q in qs if q]
    if not qs:
        return []
    head = next((f"{brand} {n}".strip() for n in names if n), qs[0])
    qs += [f"{head} {intent}" for intent in INTENTS]
    # shops that matched before get a direct look, on top of the open search
    qs += [f"{head} site:{d}" for d in watched_domains()[:10]]
    return list(dict.fromkeys(qs))[:MAX_QUERIES]


def web_search(q):
    r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                  params={"q": q, "count": 15},
                  headers={"X-Subscription-Token": BRAVE, "Accept": "application/json"},
                  timeout=15)
    r.raise_for_status()
    return [x["url"] for x in r.json().get("web", {}).get("results", [])]


EXTRA_ROUNDS = int(os.environ.get("STYLE_FINDER_EXTRA_ROUNDS", "2"))


def _host(u):
    return urlparse(u).netloc.removeprefix("www.")


def _run_queries(queries, exclude=()):
    if BRAVE:
        suffix = "".join(f" -site:{d}" for d in list(exclude)[:20])
        with ThreadPoolExecutor(4) as pool:
            return [u for batch in pool.map(_safe_search, [q + suffix for q in queries])
                    for u in batch]
    batches = [queries[i:i + 8] for i in range(0, len(queries), 8)]
    with ThreadPoolExecutor(len(batches)) as pool:
        results = list(pool.map(lambda b: _safe_claude_search(b, exclude), batches))
    errors = [r for r in results if isinstance(r, LLMError)]
    if len(errors) == len(results):
        raise errors[0]  # every batch failed (e.g. out of credit): say so, don't report "nothing found"
    return [u for r in results if not isinstance(r, LLMError) for u in r]


def discover(card):
    """Open-web discovery with no shop list.

    Round 1 runs every query. Search engines keep returning the same few big sites
    (often the brand's own), so each extra round re-runs the core queries with every
    shop found so far excluded, which forces new shops to surface.
    """
    queries = build_queries(card)
    if not queries:
        return []
    skip = blocked_domains()
    urls = _run_queries(queries, exclude=skip)
    core = [q for q in queries if "site:" not in q][:10]
    for _ in range(EXTRA_ROUNDS):
        found = {_host(u) for u in urls}
        more = [u for u in _run_queries(core, exclude=found | skip) if _host(u) not in found]
        if not more:
            break
        urls += more
    per_shop, out = {}, []
    for u in dict.fromkeys(urls):
        host = _host(u)
        if host in skip or any(s in host for s in SKIP_DOMAINS):
            continue
        per_shop[host] = per_shop.get(host, 0) + 1
        if per_shop[host] <= MAX_PER_SHOP:
            out.append(u)
    return out[:MAX_URLS]


def _safe_claude_search(batch, exclude=()):
    try:
        return web_search_urls(batch, blocked_domains=exclude)
    except LLMError as e:
        return e


def _safe_search(q):
    try:
        return web_search(q)
    except Exception:
        return []


# --- Step 3: universal extraction -------------------------------------------

def parse_price(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v) or None
    s = re.sub(r"[^0-9.,]", "", str(v))
    if not s:
        return None
    # "1.234,56" or "1234,56" -> European decimal comma
    if "," in s and (s.rfind(",") > s.rfind(".")) and len(s) - s.rfind(",") == 3:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return float(s) or None
    except ValueError:
        return None


IN_STOCK = {"instock", "limitedavailability", "onlineonly", "instoreonly", "preorder",
            "presale", "backorder", "in stock"}
OUT_OF_STOCK = {"outofstock", "soldout", "discontinued", "oos", "out of stock", "sold out"}


def _stock(values):
    """True if any value says in stock, False if all known values say out, else None."""
    vals = {str(v).rsplit("/", 1)[-1].strip().lower() for v in values if v}
    if vals & IN_STOCK:
        return True
    if vals & OUT_OF_STOCK:
        return False
    return None


def _shopify(url, client):
    """Any Shopify store answers /products/<handle>.js with product data and stock."""
    p = urlparse(url)
    if "/products/" not in p.path:
        return None
    base = p.path.rstrip("/").removesuffix(".json").removesuffix(".js")
    try:
        r = client.get(urlunparse(p._replace(path=base + ".js", query="", fragment="")))
        prod = r.json()
        variants = prod["variants"]
    except Exception:
        return None
    sizes = [v.get("title") for v in variants if v.get("available")]
    price = prod.get("price")
    img = prod.get("featured_image")
    return {"title": prod.get("title"), "brand": prod.get("vendor"),
            "price": price / 100 if isinstance(price, (int, float)) else None,  # cents
            "currency": None, "image": ("https:" + img) if img and img.startswith("//") else img,
            "sku": variants[0].get("sku") if variants else None,
            "sizes": sizes, "condition": None,
            "in_stock": bool(prod.get("available")) and bool(sizes)}


def _jsonld_nodes(data):
    stack = [data]
    while stack:
        n = stack.pop()
        if isinstance(n, list):
            stack.extend(n)
        elif isinstance(n, dict):
            yield n
            if "@graph" in n:
                stack.append(n["@graph"])
            if "hasVariant" in n:
                stack.append(n["hasVariant"])


def _is_product(node):
    t = node.get("@type")
    t = t if isinstance(t, list) else [t]
    return any(x in ("Product", "ProductGroup", "IndividualProduct") for x in t)


def _offers(node):
    offers = node.get("offers") or {}
    return [o for o in (offers if isinstance(offers, list) else [offers]) if isinstance(o, dict)]


def _jsonld(soup):
    nodes = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            nodes += [n for n in _jsonld_nodes(json.loads(tag.string or "")) if _is_product(n)]
        except Exception:
            continue
    # stock across every product/variant offer on the page
    in_stock = _stock(o.get("availability") for n in nodes for o in _offers(n))
    for node in nodes:
            offer = next(iter(_offers(node)), {})
            price = offer.get("price") or offer.get("lowPrice")
            if price is None and isinstance(offer.get("priceSpecification"), dict):
                price = offer["priceSpecification"].get("price")
            img = node.get("image")
            img = img[0] if isinstance(img, list) and img else img
            img = img.get("url") if isinstance(img, dict) else img
            brand = node.get("brand")
            brand = brand.get("name") if isinstance(brand, dict) else brand
            cond = str(offer.get("itemCondition") or "").rsplit("/", 1)[-1]
            cond = cond.removesuffix("Condition").lower() or None
            return {"title": node.get("name"), "brand": brand, "price": price,
                    "currency": offer.get("priceCurrency"), "image": img,
                    "sku": node.get("sku") or node.get("mpn"),
                    "in_stock": in_stock, "condition": cond}
    return None


PAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "is_single_product": {"type": "boolean"},
        "title": {"type": ["string", "null"]},
        "brand": {"type": ["string", "null"]},
        "price": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"]},
        "sku": {"type": ["string", "null"]},
        "condition": {"type": ["string", "null"]},
        "sizes": {"type": "array", "items": {"type": "string"}},
        "in_stock": {"type": ["boolean", "null"]},
    },
    "required": ["is_single_product", "title", "brand", "price", "currency",
                 "sku", "condition", "sizes", "in_stock"],
    "additionalProperties": False,
}


def _llm_page(soup):
    text = soup.get_text(" ", strip=True)[:8000]
    if len(text) < 200:
        return None
    d = ask_json("Does this web page sell one specific product (a product listing, not a "
                 "category, search or editorial page)? If so extract its fields; condition is "
                 "new, used, or deadstock if stated; sizes are only sizes shown in stock. in_stock is "
                 "false if the item is sold out, out of stock, or only offers a restock "
                 "notification in every size; true if it can be bought now; null if unclear.\n\n"
                 + text, PAGE_SCHEMA, max_tokens=2000)
    if not d.pop("is_single_product") or not d.get("title"):
        return None
    og = soup.find("meta", property="og:image")
    d["image"] = og.get("content") if og else None
    return d


def _claude_fetch(url):
    """Shops that block automated visitors: Claude fetches and reads the page instead."""
    d = ask_json([{"type": "text", "text":
        f"Fetch {url} with the web_fetch tool. Does it sell one specific product (a product "
        "listing, not a category, search or editorial page)? If so extract its fields; "
        "condition is new, used, or deadstock if stated; sizes are only sizes in stock. "
        "in_stock is false if it is sold out or only offers a restock notification in every "
        "size; true if it can be bought now; null if unclear. If the page can't be fetched, "
        "set is_single_product to false."}],
        PAGE_SCHEMA, max_tokens=4000,
        tools=[{"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 1}])
    if not d.pop("is_single_product") or not d.get("title"):
        return None
    d["image"] = None
    return d


def extract_product(url):
    try:
        with httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True) as c:
            p = _shopify(url, c)
            if not p:
                r = c.get(url)
                if r.status_code in (401, 403, 429, 503):  # bot wall
                    p = _claude_fetch(url)
                    return _finish(p, url)
                if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
                    return None
                soup = BeautifulSoup(r.text, "html.parser")
                p = _jsonld(soup) or _llm_page(soup)
                if p and p.get("in_stock") is None:
                    meta = soup.find("meta", property="product:availability") or \
                        soup.find("meta", property="og:availability")
                    p["in_stock"] = _stock([meta.get("content")]) if meta else None
    except (httpx.HTTPError, LLMError, ValueError):
        return None
    return _finish(p, url)


def _finish(p, url):
    if not p or not p.get("title"):
        return None
    p["url"] = url
    p["domain"] = urlparse(url).netloc.removeprefix("www.")
    p["price"] = parse_price(p.get("price"))
    return p


# --- Step 4: verification funnel --------------------------------------------

def hard_check(card, product, strictness):
    """Cheap filter. Returns 'EXACT' (style code hit), 'PASS', or None (reject)."""
    code = _norm(card.get("style_code")).replace(" ", "")
    if code and len(code) >= 5:
        hay = _norm(" ".join(str(product.get(k) or "") for k in ("sku", "title", "url")))
        if code in hay.replace(" ", ""):
            return "EXACT"
    if strictness == "loose":
        return "PASS"
    brand = _norm(card.get("brand"))
    if not brand:
        return "PASS"
    cand = _norm(f"{product.get('brand') or ''} {product.get('title') or ''}")
    allowed = [brand]
    if strictness == "lineage":
        allowed += [_norm(b) for b, _ in get_neighbors(card.get("brand"))]
    return "PASS" if any(b and b in cand for b in allowed) else None


VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "label": {"type": "string",
                  "enum": ["EXACT", "SEASON", "LINEAGE", "INSPIRED", "NO"]},
        "reason": {"type": "string"},
    },
    "required": ["label", "reason"],
    "additionalProperties": False,
}


def _feedback_text(item_id):
    ex = feedback_examples(item_id) if item_id is not None else []
    if not ex:
        return ""
    return ("\n\nThe user has voted on earlier results (👍 up / 👎 down, with their reason "
            "when given). Treat these as their taste: reject candidates like the downvoted "
            "ones for the same reason, and favour ones like the upvoted ones. Votes marked "
            "for_this_item apply most strongly.\n" + json.dumps(ex))


def verify(card, product, item_id=None):
    ref = {k: v for k, v in card.items() if not k.startswith("_")}
    near = [b for b, _ in get_neighbors(card.get("brand"))]
    d = ask_json(
        "Reference product:\n" + json.dumps(ref) +
        "\n\nCandidate listing:\n" + json.dumps(product) +
        "\n\nBrands in the same design neighborhood as the reference: " + json.dumps(near) +
        "\n\nLabel the candidate:\n"
        "EXACT: the same product (same model, same colorway).\n"
        "SEASON: same model from the same brand, different season or colorway.\n"
        "LINEAGE: same brand with the same design language, or a neighborhood brand with "
        "the same silhouette and price tier.\n"
        "INSPIRED: similar silhouette and price tier from another serious designer.\n"
        "NO: anything else, including a much cheaper lookalike, a different category, or "
        "a page that is not really a listing for one item.\n"
        "Give a one-sentence reason." + _feedback_text(item_id),
        VERIFY_SCHEMA, max_tokens=2000)
    return d["label"], d["reason"]


def _tier_ok(card, product):
    """Price tier sanity: a luxury reference should not surface a $60 lookalike."""
    tier = TIER_RANK.get(card.get("price_tier"))
    price = product.get("price")
    if tier is None or price is None:
        return True
    floor = {0: 0, 1: 30, 2: 120, 3: 250}[tier]
    return price >= floor


# --- Orchestration ----------------------------------------------------------

def _process(item_id, card, strictness, url):
    p = extract_product(url)
    if not p or p.get("in_stock") is False:
        return None  # not marked seen: if it comes back in stock it shows up as new
    status = seen_status(item_id, url, p["price"])
    if status is None:
        return None
    hc = hard_check(card, p, strictness)
    if hc is None or not _tier_ok(card, p):
        mark_seen(item_id, url, p["price"], "NO")
        return None
    if hc == "EXACT":
        label, reason = "EXACT", "style code match"
    else:
        try:
            label, reason = verify(card, p, item_id)
        except LLMError:
            return None  # not marked seen, so it gets retried next run
    mark_seen(item_id, url, p["price"], label)
    if label not in STRICTNESS[strictness]:
        return None
    promote_domain(p["domain"])
    return {**p, "label": label, "reason": reason, "status": status}


# Rough USD value of one unit, for comparing prices across shops. Approximate on
# purpose: it only orders results, it is not a landed-cost quote.
USD_PER = {"USD": 1, "EUR": 1.08, "GBP": 1.27, "CHF": 1.13, "CAD": 0.72, "AUD": 0.65,
           "JPY": 0.0066, "HKD": 0.128, "SGD": 0.74, "KRW": 0.00072, "CNY": 0.138,
           "SEK": 0.095, "DKK": 0.145, "NOK": 0.092}


def _usd(price, currency, default="USD"):
    if price is None:
        return None
    rate = USD_PER.get((currency or default).upper())
    return price * rate if rate else None


def reference_price(card, results):
    """Retail in USD: from the card if known, else the median price found."""
    ref = _usd(card.get("retail_price"), card.get("retail_currency"))
    if ref:
        return ref
    found = sorted(p for p in (r["usd"] for r in results) if p)
    return found[len(found) // 2] if found else None


def rank(card, results):
    """Match group first (exact, season, lineage, inspired), then best deal."""
    default_cur = card.get("retail_currency") or "USD"
    for r in results:
        r["usd"] = _usd(r["price"], r.get("currency"), default_cur)
    ref = reference_price(card, results)
    for r in results:
        r["discount"] = (1 - r["usd"] / ref) if ref and r["usd"] else None
    return sorted(results, key=lambda r: (
        LABEL_ORDER[r["label"]],
        -(r["discount"] if r["discount"] is not None else -1e9)))  # unknown price last


def run_search(item_id, card, strictness="lineage"):
    urls = discover(card)
    with ThreadPoolExecutor(10) as pool:
        out = [r for r in pool.map(lambda u: _process(item_id, card, strictness, u), urls) if r]
    return rank(card, out)


def format_result(r, prefix=""):
    price = f"{r['price']:,.2f} {r.get('currency') or ''}".strip() if r["price"] else "price ?"
    tag = "Price drop: " if r["status"] == "cheaper" else prefix
    d = r.get("discount")
    if d is not None and d >= 0.05:
        price += f"  ({d:.0%} below retail)"
    elif d is not None and d <= -0.05:
        price += f"  ({-d:.0%} above retail)"
    extra = []
    if r.get("condition"):
        extra.append(r["condition"])
    if r.get("sizes"):
        extra.append("sizes: " + ", ".join(r["sizes"][:8]))
    lines = [f"{tag}[{r['label']}] {r['title']}",
             f"{price}  |  {r['domain']}" + (f"  |  {'; '.join(extra)}" if extra else ""),
             r["reason"], r["url"]]
    return "\n".join(lines)
