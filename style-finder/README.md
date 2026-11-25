# Style Finder

A personal Telegram bot. Send it a screenshot, a photo or a product name. It fills in a product card for you to check, then searches the open web for exact and same-lineage listings, and after that only messages you about new listings or price drops.

Built from `Style_Finder_MVP_Spec.md`, parts 1 to 4.

## Setup

1. In Telegram, message @BotFather, send `/newbot`, copy the token.
2. Get an Anthropic API key and a Brave Search API key (brave.com/search/api, free tier).
3. `pip install -r requirements.txt`
4. `cp .env.example .env` and fill it in.
5. `python bot.py`, message the bot `/start`, put the chat id it shows you into `ALLOWED_CHAT_IDS`, restart.

## Use

- Send a photo, an image file, a screenshot or text. The bot replies with a card.
- Reply `ok` to save it and search. Anything else you type is treated as a correction ("it's the black colorway, brand is Lemaire").
- `/list`, `/strict <id> exact|lineage|loose`, `/remove <id>`, `/check`, `/cancel`.
- Every saved item is searched again every `RECHECK_HOURS` hours (default 6). The brand map is refined once a day.

## How it works

| File | Job |
|---|---|
| `bot.py` | Telegram handlers: intake, correction, confirm, commands, scheduled jobs |
| `llm.py` | Every Claude call. Uses structured outputs, so replies are always valid JSON |
| `search.py` | Query fan-out (Brave), extraction (Shopify JSON, then schema.org, then the model), checks, ranking |
| `memory.py` | Taste log, brand-neighborhood seeding, nightly refinement |
| `store.py` | SQLite: wishlist, seen list, watched domains, taste, brand map |

Order of checks for each candidate page, cheapest first:
1. Seen list. Skip it if it was already seen and the price hasn't dropped.
2. Style code or SKU found in the listing means EXACT, with no model call.
3. Brand must match. In `lineage` mode a brand from the neighborhood map also passes. In `loose` mode any brand passes.
4. Price-tier floor, so a luxury reference never surfaces a $60 lookalike.
5. The model labels it EXACT, SEASON, LINEAGE, INSPIRED or NO, with a one-line reason.

Pages rejected by the cheap checks are added to the seen list so they aren't fetched again. Any domain that produces a match is added to the watched set and gets its own `site:` query next time.

## Cost notes

The default model is `claude-opus-5-5`. You can change it with `STYLE_FINDER_MODEL`. Each search can make up to about 40 page reads plus one verify call for every page that passes the cheap checks. On a schedule that adds up, so lower `STYLE_FINDER_MAX_URLS` or raise `RECHECK_HOURS` if your bill grows.

## Not built yet

Size filter, target prices, landed cost, image embeddings, Instagram links (send a screenshot instead).
