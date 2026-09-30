import asyncio
import html
import logging
import os
import re

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import Message, MessageEntity
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("custom_emoji_bot")

router = Router()

PACK_LINK_RE = re.compile(r"t\.me/addemoji/([A-Za-z0-9_]+)")
CHUNK_LIMIT = 3500  # Telegram's message limit is 4096 chars; leave headroom for the next entry.

START_TEXT = (
    "👋 <b>Привет! Я — Emoji ID Bot.</b>\n\n"
    "Достаю <code>custom_emoji_id</code> из Telegram custom emoji.\n\n"
    "<b>Как пользоваться:</b>\n"
    "▫️ Пришли сообщение с одним или несколькими custom emoji — верну ID каждого, "
    "с готовыми фрагментами для HTML и MarkdownV2.\n"
    "▫️ Пришли ссылку на emoji-pack вида <code>https://t.me/addemoji/PackName</code> — "
    "верну список ID всех emoji из пака.\n\n"
    "<i>Обычный emoji с клавиатуры не подойдёт — нужен именно custom emoji "
    "из Telegram-пака (Premium).</i>"
)
NOT_FOUND_TEXT = (
    "🤔 <b>Не вижу custom emoji в сообщении.</b>\n\n"
    "Проверь, что ты отправил emoji именно из Telegram emoji-пака, "
    "а не обычный emoji с клавиатуры."
)
BAD_PACK_TEXT = (
    "⚠️ <b>Не смог получить emoji-pack.</b>\n\n"
    "Проверь ссылку — она должна выглядеть так:\n"
    "<code>https://t.me/addemoji/PackName</code>"
)
FOUND_HEADER = "✨ <b>Найдено custom emoji:</b>"
PREMIUM_NOTE = (
    "ℹ️ <i>Анимацию видят только пользователи с Telegram Premium — "
    "у остальных вместо неё показывается обычный символ-заглушка выше. "
    "Это не баг бота, так устроен сам Telegram.</i>"
)


def utf16_slice(text: str, offset: int, length: int) -> str:
    # Telegram entity offset/length are counted in UTF-16 code units, not Python
    # code points, so a plain text[offset:length] slice breaks on emoji/astral chars.
    encoded = text.encode("utf-16-le")
    start = offset * 2
    end = start + length * 2
    return encoded[start:end].decode("utf-16-le")


def extract_custom_emoji(text: str | None, entities: list[MessageEntity] | None) -> list[tuple[str, str]]:
    if not text or not entities:
        return []
    found = []
    for entity in entities:
        if entity.type == "custom_emoji" and entity.custom_emoji_id:
            fallback = utf16_slice(text, entity.offset, entity.length)
            found.append((fallback, entity.custom_emoji_id))
    return found


def emoji_preview(fallback: str, custom_emoji_id: str) -> str:
    # Telegram Premium users see the live custom emoji here; everyone else
    # automatically sees the plain fallback character instead.
    return f'<tg-emoji emoji-id="{custom_emoji_id}">{html.escape(fallback)}</tg-emoji>'


def format_emoji_entry(fallback: str, custom_emoji_id: str, index: int | None = None) -> str:
    prefix = f"{index}. " if index is not None else ""
    html_snippet = html.escape(f'<tg-emoji emoji-id="{custom_emoji_id}">{fallback}</tg-emoji>')
    md_snippet = html.escape(f"![{fallback}](tg://emoji?id={custom_emoji_id})")
    return (
        f"{prefix}{emoji_preview(fallback, custom_emoji_id)}\n"
        f"<b>custom_emoji_id:</b> <code>{custom_emoji_id}</code>\n\n"
        f"<b>HTML:</b>\n<code>{html_snippet}</code>\n\n"
        f"<b>MarkdownV2:</b>\n<code>{md_snippet}</code>"
    )


def format_pack_entry(index: int, fallback: str, custom_emoji_id: str) -> str:
    return f"{index}. {emoji_preview(fallback, custom_emoji_id)} — <code>{custom_emoji_id}</code>"


def strip_html_formatting(text: str) -> str:
    # Fallback for the rare case Telegram rejects the tg-emoji preview tag:
    # drop all markup and send the same content as plain text instead.
    text = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", text, flags=re.S)
    text = re.sub(r"</?(b|i|code)>", "", text)
    return html.unescape(text)


def build_chunks(header: str, items: list[str], continuation_header: str | None = None, limit: int = CHUNK_LIMIT) -> list[str]:
    chunks: list[str] = []
    current = header
    for item in items:
        candidate = f"{current}\n\n{item}" if current else item
        if len(candidate) > limit and current:
            chunks.append(current)
            current = f"{continuation_header}\n\n{item}" if continuation_header else item
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


async def send_chunk(message: Message, chunk: str) -> None:
    try:
        await message.answer(chunk)
    except TelegramBadRequest:
        logger.warning("HTML reply rejected by Telegram, retrying as plain text")
        await message.answer(strip_html_formatting(chunk), parse_mode=None)


@router.message(CommandStart())
async def handle_start(message: Message) -> None:
    await message.answer(START_TEXT)


@router.message()
async def handle_message(message: Message, bot: Bot) -> None:
    text = message.text or message.caption or ""

    pack_match = PACK_LINK_RE.search(text)
    if pack_match:
        await handle_pack_link(message, bot, pack_match.group(1))
        return

    found = extract_custom_emoji(message.text, message.entities) or extract_custom_emoji(
        message.caption, message.caption_entities
    )

    if not found:
        await message.answer(NOT_FOUND_TEXT)
        return

    numbered = len(found) > 1
    entries = [
        format_emoji_entry(fallback, cid, index=i if numbered else None)
        for i, (fallback, cid) in enumerate(found, start=1)
    ]
    entries.append(PREMIUM_NOTE)
    for chunk in build_chunks(FOUND_HEADER, entries):
        await send_chunk(message, chunk)


async def handle_pack_link(message: Message, bot: Bot, pack_name: str) -> None:
    try:
        sticker_set = await bot.get_sticker_set(name=pack_name)
    except TelegramBadRequest:
        await message.answer(BAD_PACK_TEXT)
        return

    custom_emojis = [
        (sticker.emoji or "❓", sticker.custom_emoji_id)
        for sticker in sticker_set.stickers
        if sticker.type == "custom_emoji" and sticker.custom_emoji_id
    ]

    if not custom_emojis:
        await message.answer(BAD_PACK_TEXT)
        return

    safe_name = html.escape(pack_name)
    header = f"📦 <b>Emoji pack:</b> <code>{safe_name}</code>\n<i>Найдено: {len(custom_emojis)}</i>"
    continuation_header = f"📦 <b>Emoji pack:</b> <code>{safe_name}</code> <i>(продолжение)</i>"
    entries = [format_pack_entry(index, fallback, cid) for index, (fallback, cid) in enumerate(custom_emojis, start=1)]
    for chunk in build_chunks(header, entries, continuation_header=continuation_header):
        await send_chunk(message, chunk)


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    bot = Bot(token=token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Starting bot")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
