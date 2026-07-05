"""Telegram-хендлеры (aiogram 3). Ответы — plain text, без parse_mode."""

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReactionTypeEmoji,
)

from . import service
from .config import Config
from .db import Database
from .llm import Parser

log = logging.getLogger(__name__)
router = Router()

HELP_TEXT = """\
Это общая кулинарная память на двоих.

Пишите как обычно — бот сам разберёт и запишет:
• отчёт о еде с оценками («съел гречу с курицей, сытость 4/5, вкус 3/5»)
  — попадёт в дневник, продукты спишутся из холодильника;
• план готовки («на завтра: …») — сохранится, смотрится через /today;
• список закупки — бот предложит кнопку «Куплено», продукты добавятся;
• изменения холодильника («кончились огурцы», «купил творог»).

Команды:
/fridge — что в холодильнике
/diary — последние приёмы еды с оценками
/today — актуальный план готовки
/bind — привязать этот групповой чат к боту
/help — эта справка

Если бот ошибся со списанием — под его ответом есть кнопка «Отменить».
Болтовня и обсуждения игнорируются, на них бот не реагирует."""


def undo_keyboard(batch_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="↩️ Отменить", callback_data=f"undo:{batch_id}")
    ]])


def buy_keyboard(pending_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Куплено — в холодильник", callback_data=f"buy:{pending_id}")
    ]])


async def react(message: Message, emoji: str) -> None:
    """Реакция не должна ломать основной поток."""
    try:
        await message.react([ReactionTypeEmoji(emoji=emoji)])
    except Exception:  # noqa: BLE001 — реакции опциональны
        log.debug("Не удалось поставить реакцию %s", emoji)


class Context:
    """Связывает хендлеры с конфигом, БД и парсером (заполняется в main)."""
    config: Config
    database: Database
    parser: Parser


ctx = Context()


async def has_access(message: Message) -> bool:
    """Доступ: только два пользователя; группа — только привязанная (автопривязка)."""
    if not message.from_user or message.from_user.id not in ctx.config.allowed_ids:
        return False
    if message.chat.type == "private":
        return True
    bound = ctx.database.get_setting("group_chat_id")
    if bound is None:
        ctx.database.set_setting("group_chat_id", str(message.chat.id))
        await message.answer("Чат привязан. Здесь бот ведёт дневник, холодильник и планы. /help")
        return True
    return bound == str(message.chat.id)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    if not await has_access(message):
        return
    await message.answer(HELP_TEXT)


@router.message(Command("fridge"))
async def cmd_fridge(message: Message) -> None:
    if not await has_access(message):
        return
    await message.answer(service.format_fridge(ctx.database, ctx.config.tz_offset))


@router.message(Command("diary"))
async def cmd_diary(message: Message, command: CommandObject) -> None:
    if not await has_access(message):
        return
    try:
        n = max(1, min(30, int(command.args.strip()))) if command.args else 7
    except ValueError:
        n = 7
    await message.answer(service.format_diary(ctx.database, n, ctx.config.tz_offset))


@router.message(Command("today"))
async def cmd_today(message: Message) -> None:
    if not await has_access(message):
        return
    await message.answer(service.format_today(ctx.database, ctx.config.tz_offset))


@router.message(Command("bind"))
async def cmd_bind(message: Message) -> None:
    if not message.from_user or message.from_user.id not in ctx.config.allowed_ids:
        return
    if message.chat.type == "private":
        await message.answer("Привязывать нужно групповой чат — вызови /bind в группе.")
        return
    ctx.database.set_setting("group_chat_id", str(message.chat.id))
    await message.answer("Чат привязан. /help — как пользоваться.")


@router.message(F.text | F.caption)
async def on_text(message: Message) -> None:
    if not await has_access(message):
        return
    text = (message.text or message.caption or "").strip()
    if not text or text.startswith("/"):
        return

    author = ctx.config.users.get(message.from_user.id, "участник")
    raw_id = ctx.database.save_raw(
        chat_id=message.chat.id, msg_id=message.message_id,
        user_id=message.from_user.id, author=author, text=text,
    )

    inventory_lines = [
        f"- {item['name']}: {service.fmt_qty(item['qty'], item['unit'])}"
        for item in ctx.database.list_inventory()
        if item["qty"] is None or item["qty"] > 0
    ]
    parsed = await ctx.parser.parse(
        text=text, author=author,
        now_local=service.local_stamp(ctx.config.tz_offset),
        inventory_lines=inventory_lines,
    )
    if parsed is None:
        ctx.database.set_raw_kind(raw_id, "unparsed")
        await react(message, "🤔")
        return

    action = service.apply_parsed(ctx.database, parsed, author, raw_id)
    ctx.database.set_raw_kind(raw_id, action.kind)

    if action.reaction:
        await react(message, action.reaction)
    if action.reply:
        keyboard = None
        if action.undo_batch:
            keyboard = undo_keyboard(action.undo_batch)
        elif action.pending_id:
            keyboard = buy_keyboard(action.pending_id)
        await message.reply(action.reply, reply_markup=keyboard)


@router.callback_query(F.data.startswith("undo:"))
async def on_undo(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ctx.config.allowed_ids:
        await callback.answer()
        return
    batch_id = callback.data.split(":", 1)[1]
    if ctx.database.undo_batch(batch_id):
        await callback.answer("Отменено")
        if callback.message:
            await callback.message.edit_text("↩️ Изменения холодильника отменены.")
    else:
        await callback.answer("Уже отменено", show_alert=False)


@router.callback_query(F.data.startswith("buy:"))
async def on_buy(callback: CallbackQuery) -> None:
    if callback.from_user.id not in ctx.config.allowed_ids:
        await callback.answer()
        return
    pending_id = int(callback.data.split(":", 1)[1])
    action = service.confirm_shopping(ctx.database, pending_id)
    await callback.answer()
    if callback.message and action.reply:
        keyboard = undo_keyboard(action.undo_batch) if action.undo_batch else None
        await callback.message.edit_text(action.reply, reply_markup=keyboard)
