"""Taste log, brand-neighborhood seeding, nightly refinement."""
import json

from llm import LLMError, ask_json
from store import all_items, known_brand, log_taste, set_neighbors, taste_profile

NEIGHBORS_SCHEMA = {
    "type": "object",
    "properties": {"neighbors": {"type": "array", "items": {
        "type": "object",
        "properties": {"brand": {"type": "string"}, "score": {"type": "number"}},
        "required": ["brand", "score"], "additionalProperties": False}}},
    "required": ["neighbors"], "additionalProperties": False,
}

LINKS_SCHEMA = {
    "type": "object",
    "properties": {"links": {"type": "array", "items": {
        "type": "object",
        "properties": {"brand_a": {"type": "string"}, "brand_b": {"type": "string"},
                       "score": {"type": "number"}},
        "required": ["brand_a", "brand_b", "score"], "additionalProperties": False}}},
    "required": ["links"], "additionalProperties": False,
}


def _clamp(s):
    return max(0.0, min(1.0, float(s)))


def learn_from_card(card):
    """Call once when a card is confirmed."""
    log_taste(card)
    brand = card.get("brand")
    if not brand or known_brand(brand):
        return
    try:
        d = ask_json(
            f"List up to 8 designers or brands that share design language and price tier "
            f"with {brand}. Exclude brands that merely make the same product category "
            f"cheaply. Score each 0-1 for closeness.", NEIGHBORS_SCHEMA, effort="medium")
    except LLMError:
        return
    set_neighbors(brand, [(n["brand"], _clamp(n["score"])) for n in d["neighbors"]])


def nightly_refine():
    """Review recent saves and add neighbor links the taste implies."""
    items = [c for _, _, c, _ in all_items()][-40:]
    if len(items) < 5:
        return
    try:
        d = ask_json(
            "Here are a collector's recent saved products and a count profile of their "
            "taste.\n" + json.dumps([{k: v for k, v in c.items() if not k.startswith("_")}
                                     for c in items])
            + "\n" + json.dumps(taste_profile(), default=str)
            + "\n\nSuggest brand neighbor links this taste implies that are worth adding, "
              "scored 0-1.", LINKS_SCHEMA, effort="medium")
    except LLMError:
        return
    for link in d["links"]:
        set_neighbors(link["brand_a"], [(link["brand_b"], _clamp(link["score"]))])
