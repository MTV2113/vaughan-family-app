"""Step 0 of every search: look the product up on the brand's own site.

The brand's page is the authority for the style code / SKU, full retail price and season.
Those fill the card before any shop is searched, so style-code matching and the
price-below-retail ranking work from the start.
"""
import re
import unicodedata

from llm import LLMError, ask_json
from search import extract_product

OFFICIAL_SCHEMA = {
    "type": "object",
    "properties": {
        "official_url": {"type": ["string", "null"]},
        "style_code": {"type": ["string", "null"]},
        "retail_price": {"type": ["number", "null"]},
        "retail_currency": {"type": ["string", "null"]},
        "season": {"type": ["string", "null"]},
        "product_name": {"type": ["string", "null"]},
    },
    "required": ["official_url", "style_code", "retail_price", "retail_currency",
                 "season", "product_name"],
    "additionalProperties": False,
}


def _tokens(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _brand_words(brand):
    """Words of the brand name that identify it, accents and punctuation removed."""
    s = unicodedata.normalize("NFKD", brand or "").encode("ascii", "ignore").decode()
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) >= 4]


def find_official(card):
    """Return fields found on the brand's own product page, or {} if none."""
    brand = card.get("brand")
    if not brand:
        return {}
    ref = {k: card.get(k) for k in ("brand", "product_name", "aliases", "category",
                                    "silhouette", "color", "material", "season")}
    try:
        d = ask_json(
            "Find this product on the brand's OWN website (a domain the brand owns, not a "
            "retailer, marketplace, resale site or price-comparison site) using web search.\n"
            f"Product: {ref}\n\n"
            "Return the product page URL and, as printed on that page: the style code / SKU / "
            "reference number, the FULL retail price (not a sale price) with its currency, the "
            "season or collection, and the product's official name. Use null for anything the "
            "page does not show. Never guess a code. If the brand has no page for this exact "
            "product, return null for official_url and everything else.",
            OFFICIAL_SCHEMA, max_tokens=3000,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 4}])
    except LLMError:
        return {}
    url = d.get("official_url")
    if not url:
        return {}
    # The page itself is more reliable than the model's reading of it: take the SKU and
    # price from its product markup when there is any.
    page = extract_product(url)
    host = _tokens(url.split("/")[2] if "//" in url else url)
    words = _brand_words(brand) or [_tokens(brand)]
    # must be the brand's own domain, or its page must say the brand is the product's brand
    on_brand_domain = max(words, key=len) in host  # longest word: 'margiela', not 'maison'
    page_brand = _tokens((page or {}).get("brand"))
    if not on_brand_domain and not any(w in page_brand for w in words):
        return {}
    out = {"official_url": url}
    code = (page or {}).get("sku") or d.get("style_code")
    if code:
        out["style_code"] = str(code).strip()
    price = (page or {}).get("price") or d.get("retail_price")
    if price:
        out["retail_price"] = float(price)
        out["retail_currency"] = (page or {}).get("currency") or d.get("retail_currency")
    if d.get("season"):
        out["season"] = d["season"]
    if d.get("product_name"):
        out["official_name"] = d["product_name"]
    return out


def enrich(card):
    """Fill the card from the brand's site. Returns (card, found_something)."""
    found = find_official(card)
    if not found:
        return card, False
    card = dict(card)
    card["official_url"] = found["official_url"]
    if found.get("style_code"):
        card["style_code"] = found["style_code"]        # the brand's own code wins
    if found.get("retail_price"):
        card["retail_price"] = found["retail_price"]    # full retail from the brand wins
        card["retail_currency"] = found.get("retail_currency") or card.get("retail_currency")
    if found.get("season") and not card.get("season"):
        card["season"] = found["season"]
    name = found.get("official_name")
    if name and name != card.get("product_name"):
        aliases = list(card.get("aliases") or [])
        if card.get("product_name") and card["product_name"] not in aliases:
            aliases.append(card["product_name"])        # keep the old name as an alias
        card["product_name"] = name
        card["aliases"] = aliases
    return card, True
