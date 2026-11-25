"""Style Finder Telegram bot: intake -> confirm -> search -> alerts."""
import asyncio
import base64
import json
import logging
import os

from dotenv import load_dotenv

load_dotenv()  # before importing modules that read env at import time

from telegram import Update  # noqa: E402
from telegram.ext import (Application, CommandHandler, ContextTypes,  # noqa: E402
                          MessageHandler, filters)

from llm import CARD_SCHEMA, LLMError, ask_json  # noqa: E402
from memory import learn_from_card, nightly_refine  # noqa: E402
from search import STRICTNESS, format_result, run_search  # noqa: E402
from store import (all_items, items_for_chat, remove_item, save_item,  # noqa: E402
                   set_strictness)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("style-finder")

ALLOWED = {int(x) for x in os.environ.get("ALLOWED_CHAT_IDS", "").split(",") if x.strip()}
RECHECK_HOURS = float(os.environ.get("RECHECK_HOURS", "6"))
CONFIRM_WORDS = {"ok", "okay", "yes", "y", "go", "search", "👍"}

EXTRACT_PROMPT = """You are helping someone find a fashion product online.
From the image and/or text, fill in the product card. Read any visible text (captions,
tags, labels, style codes) in the image. aliases are other names the product is sold or
searched under. style_code is a manufacturer style code or SKU if one is visible.
retail_price is the full-price retail (not sale) price: use the price shown if one is
visible, otherwise the typical full retail price of this model if you know it with
confidence, else null. retail_currency is its ISO code, e.g. USD or EUR.
Use null for anything you cannot determine, and price_tier "unknown" if unclear.
Do not guess brands you cannot see or confidently infer from the design."""


def extract_card(image_bytes, media_type, text):
    content = []
    if image_bytes:
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.standard_b64encode(image_bytes).decode()}})
    content.append({"type": "text",
                    "text": EXTRACT_PROMPT + "\n\nUser text: " + (text or "none")})
    return ask_json(content, CARD_SCHEMA, effort="medium")


def apply_correction(card, text):
    return ask_json("Here is a product card as JSON:\n" + json.dumps(card) +
                    "\n\nThe user says: " + text +
                    "\n\nReturn the corrected card. Change only what the user asked for.",
                    CARD_SCHEMA)


LABELS = [("brand", "Brand"), ("product_name", "Product"), ("aliases", "Aliases"),
          ("style_code", "Style code"), ("category", "Category"),
          ("silhouette", "Silhouette"), ("color", "Color"), ("material", "Material"),
          ("season", "Season"), ("price_tier", "Price tier"), ("retail_price", "Retail"),
          ("notes", "Notes")]


def format_card(card):
    lines = []
    for key, name in LABELS:
        v = card.get(key)
        if isinstance(v, list):
            v = ", ".join(v)
        if key == "retail_price" and v:
            v = f"{v:,.0f} {card.get('retail_currency') or ''}".strip()
        if v and v != "unknown":
            lines.append(f"{name}: {v}")
    return "\n".join(lines) or "(nothing recognised yet)"


def allowed(update: Update):
    return not ALLOWED or update.effective_chat.id in ALLOWED


async def _image_from(msg):
    if msg.photo:
        f = await msg.photo[-1].get_file()
        return bytes(await f.download_as_bytearray()), "image/jpeg"
    doc = msg.document
    if doc and (doc.mime_type or "") in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        f = await doc.get_file()
        return bytes(await f.download_as_bytearray()), doc.mime_type
    return None, None


async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    msg = update.message
    pending = ctx.user_data.get("pending_card")
    text = (msg.caption or msg.text or "").strip()
    image_bytes, media_type = await _image_from(msg)

    try:
        # Confirm
        if pending and not image_bytes and text.lower() in CONFIRM_WORDS:
            ctx.user_data["pending_card"] = None
            item_id = save_item(update.effective_chat.id, pending)
            await msg.reply_text(f"Saved as #{item_id}. Searching now; I'll message you "
                                 f"with matches and keep watching every {RECHECK_HOURS:g}h.")
            asyncio.create_task(search_and_report(ctx, update.effective_chat.id,
                                                  item_id, pending, "lineage"))
            return

        # Correction
        if pending and not image_bytes:
            await msg.chat.send_action("typing")
            fixed = await asyncio.to_thread(apply_correction, pending, text)
            ctx.user_data["pending_card"] = fixed
            await msg.reply_text("Updated:\n\n" + format_card(fixed) + "\n\nReply 'ok' to search.")
            return

        # New intake
        if not image_bytes and not text:
            return
        await msg.chat.send_action("typing")
        card = await asyncio.to_thread(extract_card, image_bytes, media_type, text)
        ctx.user_data["pending_card"] = card
        await msg.reply_text("Here's what I see:\n\n" + format_card(card) +
                             "\n\nReply 'ok' to search, tell me what to fix, or /cancel.")
    except LLMError as e:
        await msg.reply_text(f"Sorry, that failed: {e}")


async def search_and_report(ctx, chat_id, item_id, card, strictness):
    try:
        await asyncio.to_thread(learn_from_card, card)  # seeds brand map before verify
        results = await asyncio.to_thread(run_search, item_id, card, strictness)
    except Exception as e:
        log.exception("search failed for item %s", item_id)
        await ctx.bot.send_message(chat_id, f"Search for #{item_id} failed: {e}")
        return
    if not results:
        await ctx.bot.send_message(chat_id, f"Nothing verified yet for #{item_id}. "
                                            "I'll keep watching.")
        return
    for r in results[:8]:
        await ctx.bot.send_message(chat_id, format_result(r), disable_web_page_preview=False)


async def recheck(ctx: ContextTypes.DEFAULT_TYPE):
    for item_id, chat_id, card, strictness in all_items():
        try:
            results = await asyncio.to_thread(run_search, item_id, card, strictness)
        except Exception:
            log.exception("recheck failed for item %s", item_id)
            continue
        for r in results[:5]:
            await ctx.bot.send_message(chat_id, format_result(r, prefix=f"New for #{item_id}: "))


async def nightly(ctx: ContextTypes.DEFAULT_TYPE):
    await asyncio.to_thread(nightly_refine)


# --- commands ---------------------------------------------------------------

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Your chat id is {update.effective_chat.id}.\n\n"
        "Send a screenshot, a photo, or a product name.\n\n"
        "/list  your wishlist\n"
        "/strict <id> exact|lineage|loose  how close matches must be\n"
        "/remove <id>  stop watching an item\n"
        "/check  re-run every search now\n"
        "/cancel  drop the card you're editing")


async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    items = items_for_chat(update.effective_chat.id)
    if not items:
        await update.message.reply_text("Wishlist is empty.")
        return
    await update.message.reply_text("\n".join(
        f"#{i}  {c.get('brand') or '?'} {c.get('product_name') or c.get('silhouette') or ''}"
        f"  ({s})" for i, _, c, s in items))


async def cmd_strict(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        item_id, level = int(ctx.args[0].lstrip("#")), ctx.args[1].lower()
        assert level in STRICTNESS
    except (IndexError, ValueError, AssertionError):
        await update.message.reply_text("Usage: /strict <id> exact|lineage|loose")
        return
    ok = set_strictness(item_id, update.effective_chat.id, level)
    await update.message.reply_text(f"#{item_id} set to {level}." if ok else "No such item.")


async def cmd_remove(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    try:
        item_id = int(ctx.args[0].lstrip("#"))
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /remove <id>")
        return
    ok = remove_item(item_id, update.effective_chat.id)
    await update.message.reply_text(f"Stopped watching #{item_id}." if ok else "No such item.")


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text("Re-checking everything. Only new listings and price drops will show up.")
    ctx.job_queue.run_once(recheck, 0)


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["pending_card"] = None
    await update.message.reply_text("Dropped.")


def main():
    app = Application.builder().token(os.environ["TELEGRAM_TOKEN"]).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("strict", cmd_strict))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("check", cmd_check))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(MessageHandler(
        filters.PHOTO | filters.Document.IMAGE | (filters.TEXT & ~filters.COMMAND), handle))
    app.job_queue.run_repeating(recheck, interval=RECHECK_HOURS * 3600, first=300)
    app.job_queue.run_repeating(nightly, interval=24 * 3600, first=600)
    app.run_polling()


if __name__ == "__main__":
    main()
