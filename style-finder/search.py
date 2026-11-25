"""Discovery, universal extraction, verification funnel, ranking."""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, urlunparse

import httpx
from bs4 import BeautifulSoup

from llm import LLMError, ask_json
from store import (get_neighbors, mark_seen, promote_domain, seen_status,
                   watched_domains)

BRAVE = os.environ.get("BRAVE_API_KEY")
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"),
           "Accept-Language": "en-US,en;q=0.9"}
RESALE = ["grailed.com", "vestiairecollective.com", "therealreal.com",
          "farfetch.com", "ssense.com"]
# Pages that are never a single listing; skip without fetching.
SKIP_DOMAINS = ("instagram.com", "pinterest.", "reddit.com", "youtube.com",
                "tiktok.com", "facebook.com", "x.com", "twitter.com", "wikipedia.org")
MAX_URLS = int(os.environ.get("STYLE_FINDER_MAX_URLS", "40"))

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
    brand = card.get("brand") or ""
    names = [card.get("product_name")] + (card.get("aliases") or [])
    qs = [f"{brand} {n}".strip() for n in names if n]
    if card.get("style_code"):
        qs.insert(0, f"{brand} {card['style_code']}".strip())
    qs.append(" ".join(filter(None, [brand, card.get("color"),
                                     card.get("silhouette"), card.get("category")])))
    qs = [q for q in qs if q]
    if not qs:
        return []
    # site: queries use the plainest name, not the style code
    head = qs[1] if card.get("style_code") and len(qs) > 1 else qs[0]
    # resale/aggregators and previously-matching domains are just more queries
    for d in list(dict.fromkeys(watched_domains()[:5] + RESALE)):
        qs.append(f"{head} site:{d}")
    return list(dict.fromkeys(qs))[:14]


def web_search(q):
    r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                  params={"q": q, "count": 15},
                  headers={"X-Subscription-Token": BRAVE, "Accept": "application/json"},
                  timeout=15)
    r.raise_for_status()
    return [x["url"] for x in r.json().get("web", {}).get("results", [])]


def discover(card):
    if not BRAVE:
        raise RuntimeError("BRAVE_API_KEY is not set")
    urls = []
    with ThreadPoolExecutor(4) as pool:
        for batch in pool.map(_safe_search, build_queries(card)):
            urls += batch
    urls = [u for u in dict.fromkeys(urls)
            if not any(s in urlparse(u).netloc for s in SKIP_DOMAINS)]
    return urls[:MAX_URLS]


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


def _shopify(url, client):
    """Any Shopify store answers /products/<handle>.json with full product data."""
    p = urlparse(url)
    if "/products/" not in p.path:
        return None
    path = p.path.rstrip("/")
    if not path.endswith(".json"):
        path += ".json"
    try:
        r = client.get(urlunparse(p._replace(path=path, query="", fragment="")))
        prod = r.json()["product"]
    except Exception:
        return None
    variants = prod.get("variants") or []
    sizes = [v.get("title") for v in variants if v.get("available", True)]
    img = (prod.get("image") or {}).get("src")
    return {"title": prod.get("title"), "brand": prod.get("vendor"),
            "price": variants[0].get("price") if variants else None,
            "currency": None, "image": img, "sku": variants[0].get("sku") if variants else None,
            "sizes": sizes, "condition": None}


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


def _jsonld(soup):
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        for node in _jsonld_nodes(data):
            if not _is_product(node):
                continue
            offers = node.get("offers") or {}
            offers = offers if isinstance(offers, list) else [offers]
            offer = next((o for o in offers if isinstance(o, dict)), {})
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
                    "availability": str(offer.get("availability") or "").rsplit("/", 1)[-1] or None,
                    "condition": cond}
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
    },
    "required": ["is_single_product", "title", "brand", "price", "currency",
                 "sku", "condition", "sizes"],
    "additionalProperties": False,
}


def _llm_page(soup):
    text = soup.get_text(" ", strip=True)[:8000]
    if len(text) < 200:
        return None
    d = ask_json("Does this web page sell one specific product (a product listing, not a "
                 "category, search or editorial page)? If so extract its fields; condition is "
                 "new, used, or deadstock if stated; sizes are only sizes shown in stock.\n\n"
                 + text, PAGE_SCHEMA, max_tokens=2000)
    if not d.pop("is_single_product") or not d.get("title"):
        return None
    og = soup.find("meta", property="og:image")
    d["image"] = og.get("content") if og else None
    return d


def extract_product(url):
    try:
        with httpx.Client(headers=HEADERS, timeout=15, follow_redirects=True) as c:
            p = _shopify(url, c)
            if not p:
                r = c.get(url)
                if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
                    return None
                soup = BeautifulSoup(r.text, "html.parser")
                p = _jsonld(soup) or _llm_page(soup)
    except (httpx.HTTPError, LLMError, ValueError):
        return None
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


def verify(card, product):
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
        "Give a one-sentence reason.",
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
    if not p:
        return None
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
            label, reason = verify(card, p)
        except LLMError:
            return None  # not marked seen, so it gets retried next run
    mark_seen(item_id, url, p["price"], label)
    if label not in STRICTNESS[strictness]:
        return None
    promote_domain(p["domain"])
    return {**p, "label": label, "reason": reason, "status": status}


def run_search(item_id, card, strictness="lineage"):
    urls = discover(card)
    with ThreadPoolExecutor(6) as pool:
        out = [r for r in pool.map(lambda u: _process(item_id, card, strictness, u), urls) if r]
    return sorted(out, key=lambda r: (LABEL_ORDER[r["label"]],
                                      r["price"] if r["price"] is not None else 1e12))


def format_result(r, prefix=""):
    price = f"{r['price']:,.2f} {r.get('currency') or ''}".strip() if r["price"] else "price ?"
    tag = "Price drop: " if r["status"] == "cheaper" else prefix
    extra = []
    if r.get("condition"):
        extra.append(r["condition"])
    if r.get("sizes"):
        extra.append("sizes: " + ", ".join(r["sizes"][:8]))
    lines = [f"{tag}[{r['label']}] {r['title']}",
             f"{price}  |  {r['domain']}" + (f"  |  {'; '.join(extra)}" if extra else ""),
             r["reason"], r["url"]]
    return "\n".join(lines)
