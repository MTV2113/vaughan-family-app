"""One place for every model call: structured JSON out, refusal-safe."""
import json
import os

import anthropic

client = anthropic.Anthropic()
MODEL = os.environ.get("STYLE_FINDER_MODEL", "claude-opus-5-5")


class LLMError(Exception):
    pass


def ask_json(content, schema, max_tokens=4000, effort="low"):
    """Send a user turn and get back a dict matching `schema`.

    Uses structured outputs, so there is no fence-stripping or JSON repair.
    Server-side fallbacks re-run a policy-declined request on another model.
    """
    try:
        resp = client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": effort,
                           "format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": content}],
        )
    except anthropic.APIConnectionError as e:
        raise LLMError(f"network error talking to the model: {e}") from e
    except anthropic.RateLimitError as e:
        raise LLMError("rate limited by the model API, try again shortly") from e
    except anthropic.APIStatusError as e:
        raise LLMError(f"model API error {e.status_code}: {e.message}") from e

    if resp.stop_reason == "refusal":
        raise LLMError("the model declined this request")
    if resp.stop_reason == "max_tokens":
        raise LLMError("model reply was cut off")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMError("model returned no text")
    return json.loads(text)


def _nullable(t):
    return {"type": [t, "null"]}


CARD_FIELDS = {
    "brand": _nullable("string"),
    "product_name": _nullable("string"),
    "aliases": {"type": "array", "items": {"type": "string"}},
    "style_code": _nullable("string"),
    "category": _nullable("string"),
    "silhouette": _nullable("string"),
    "color": _nullable("string"),
    "material": _nullable("string"),
    "season": _nullable("string"),
    "price_tier": {"type": "string",
                   "enum": ["budget", "mid", "premium", "luxury", "unknown"]},
    "notes": _nullable("string"),
}

CARD_SCHEMA = {
    "type": "object",
    "properties": CARD_FIELDS,
    "required": list(CARD_FIELDS),
    "additionalProperties": False,
}
