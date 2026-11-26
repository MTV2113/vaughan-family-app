"""One place for every model call: structured JSON out, refusal-safe."""
import json
import os

import anthropic

client = anthropic.Anthropic()
MODEL = os.environ.get("STYLE_FINDER_MODEL", "claude-opus-5-5")


class LLMError(Exception):
    pass


def _status_error(e, what):
    if "credit balance" in str(e.message).lower():
        return LLMError("your Anthropic account is out of credit. Top up at "
                        "console.anthropic.com → Billing, then send /check")
    return LLMError(f"{what} ({e.status_code}): {e.message}")


def ask_json(content, schema, max_tokens=4000, effort="low", tools=None):
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
            **({"tools": tools} if tools else {}),
        )
    except anthropic.APIConnectionError as e:
        raise LLMError(f"network error talking to the model: {e}") from e
    except anthropic.RateLimitError as e:
        raise LLMError("rate limited by the model API, try again shortly") from e
    except anthropic.APIStatusError as e:
        raise _status_error(e, "model API error") from e

    if resp.stop_reason == "refusal":
        raise LLMError("the model declined this request")
    if resp.stop_reason == "pause_turn":
        raise LLMError("model paused mid tool use")
    if resp.stop_reason == "max_tokens":
        raise LLMError("model reply was cut off")
    text = next((b.text for b in resp.content if b.type == "text"), None)
    if text is None:
        raise LLMError("model returned no text")
    return json.loads(text)


def web_search_urls(queries, blocked_domains=None, max_continuations=3):
    """Run search queries through Claude's built-in web search tool; return result URLs.

    One request covers every query. Billed per search plus tokens, no extra key.
    """
    # basic variant on purpose: the 20260209 one filters results inside a code sandbox,
    # so most result URLs never come back to us
    tools = [{"type": "web_search_20250305", "name": "web_search",
              "max_uses": len(queries) + 2}]
    if blocked_domains:
        tools[0]["blocked_domains"] = list(blocked_domains)[:60]
    messages = [{"role": "user", "content":
                 "Run a separate web search for each query below, exactly as written, "
                 "one after another. Do not summarise; just run the searches.\n\n"
                 + "\n".join(f"- {q}" for q in queries)}]
    urls = []
    try:
        for _ in range(max_continuations + 1):
            resp = client.messages.create(model=MODEL, max_tokens=4000, tools=tools,
                                          output_config={"effort": "low"},
                                          messages=messages)
            for block in resp.content:
                # success: content is a list of results; error: a single error object
                if block.type == "web_search_tool_result" and isinstance(block.content, list):
                    urls += [r.url for r in block.content if getattr(r, "url", None)]
            if resp.stop_reason != "pause_turn":
                break
            messages = messages[:1] + [{"role": "assistant", "content": resp.content}]
    except anthropic.APIStatusError as e:
        raise _status_error(e, "web search failed") from e
    except anthropic.APIConnectionError as e:
        raise LLMError(f"network error during web search: {e}") from e
    return list(dict.fromkeys(urls))


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
    "official_url": _nullable("string"),
    "retail_price": _nullable("number"),
    "retail_currency": _nullable("string"),
    "notes": _nullable("string"),
}

CARD_SCHEMA = {
    "type": "object",
    "properties": CARD_FIELDS,
    "required": list(CARD_FIELDS),
    "additionalProperties": False,
}
