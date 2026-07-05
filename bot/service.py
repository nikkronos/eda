"""Бизнес-логика: применение разобранных сообщений, форматирование ответов.

Только stdlib — тестируется локально без aiogram/anthropic.
Тексты ответов безличные (без родовых форм).
"""

import datetime as dt
from dataclasses import dataclass, field

from . import db as db_module

MSK_MISSING = "—"


def fmt_qty(qty: float | None, unit: str | None) -> str:
    if qty is None:
        return "есть"
    num = int(qty) if float(qty).is_integer() else round(qty, 1)
    return f"{num} {unit}" if unit else str(num)


def fmt_score(value: float | None) -> str:
    if value is None:
        return MSK_MISSING
    num = int(value) if float(value).is_integer() else value
    return f"{num}/5"


def local_date(tz_offset: int) -> dt.date:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=tz_offset)).date()


def local_stamp(tz_offset: int) -> str:
    now = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=tz_offset)
    return now.strftime("%Y-%m-%d %H:%M")


def utc_to_local(ts_utc: str, tz_offset: int) -> str:
    """'2026-07-05 10:00:00' (UTC из SQLite) -> 'дд.мм чч:мм' локально."""
    try:
        parsed = dt.datetime.strptime(ts_utc, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return ts_utc or MSK_MISSING
    local = parsed + dt.timedelta(hours=tz_offset)
    return local.strftime("%d.%m %H:%M")


@dataclass
class Action:
    """Что боту сделать в ответ на сообщение."""
    kind: str
    reaction: str | None = None        # эмодзи-реакция на сообщение
    reply: str | None = None           # текст ответа (None — молчать)
    undo_batch: str | None = None      # batch_id для кнопки «Отменить»
    pending_id: int | None = None      # id для кнопки «Купил всё»
    lines: list[str] = field(default_factory=list)


def describe_changes(changes: list[dict]) -> list[str]:
    lines = []
    for ch in changes:
        name, unit = ch["name"], ch["unit"]
        before, after = ch["qty_before"], ch["qty_after"]
        if ch["op"] == "deplete" or after == 0:
            lines.append(f"{name}: кончилось")
        elif ch["op"] == "add" and before is None and after is not None:
            lines.append(f"{name}: +{fmt_qty(after, unit)}")
        elif before is not None and after is not None:
            before_str = fmt_qty(before, ch.get("unit_before") or unit)
            after_str = fmt_qty(after, unit)
            if before_str == after_str:
                lines.append(f"{name}: {after_str} — без изменений")
            else:
                lines.append(f"{name}: {before_str} → {after_str}")
        else:
            lines.append(f"{name}: {fmt_qty(after, unit)}")
    return lines


def apply_parsed(database: db_module.Database, parsed: dict, author: str,
                 raw_id: int) -> Action:
    """Применяет результат LLM-разбора к базе и решает, как ответить."""
    kind = parsed.get("kind", "chatter")
    ops = parsed.get("inventory_ops") or []

    if kind == "meal":
        meal = parsed.get("meal") or {}
        description = (meal.get("description") or "").strip()
        if not description:
            return Action(kind="chatter")
        database.add_meal(
            description=description,
            satiety=meal.get("satiety"),
            taste=meal.get("taste"),
            notes=meal.get("notes"),
            raw_id=raw_id,
        )
        action = Action(kind="meal", reaction="✍")
        if ops:
            batch_id, changes = database.apply_ops(ops, reason=f"meal:{raw_id}")
            if changes:
                action.undo_batch = batch_id
                action.lines = describe_changes(changes)
                action.reply = "📦 Списано:\n" + "\n".join(
                    f"• {line}" for line in action.lines
                )
        return action

    if kind == "plan":
        plan = parsed.get("plan") or {}
        text = (plan.get("text") or "").strip()
        if not text:
            return Action(kind="chatter")
        database.add_plan(author=author, text=text, date_for=plan.get("date_for"))
        return Action(kind="plan", reaction="👌")

    if kind == "shopping_list":
        add_ops = [op for op in ops if op.get("op") == "add" and op.get("name")]
        if not add_ops:
            return Action(kind="chatter")
        pending_id = database.add_pending("shopping", {"ops": add_ops, "raw_id": raw_id})
        names = "\n".join(
            f"• {op['name']}" + (f" — {fmt_qty(op.get('qty'), op.get('unit'))}"
                                 if op.get("qty") is not None else "")
            for op in add_ops
        )
        return Action(
            kind="shopping_list",
            reply=f"🛒 Список закупки ({len(add_ops)}):\n{names}\n\n"
                  "Как будет куплено — жми кнопку, всё попадёт в холодильник.",
            pending_id=pending_id,
        )

    if kind == "inventory":
        if not ops:
            return Action(kind="chatter")
        batch_id, changes = database.apply_ops(ops, reason=f"inventory:{raw_id}")
        if not changes:
            return Action(kind="chatter")
        lines = describe_changes(changes)
        return Action(
            kind="inventory",
            reply="📦 Холодильник обновлён:\n" + "\n".join(f"• {line}" for line in lines),
            undo_batch=batch_id,
            lines=lines,
        )

    return Action(kind="chatter")


def confirm_shopping(database: db_module.Database, pending_id: int) -> Action:
    payload = database.take_pending(pending_id)
    if not payload:
        return Action(kind="noop", reply="Уже добавлено раньше.")
    batch_id, changes = database.apply_ops(payload["ops"], reason=f"shopping:{pending_id}")
    lines = describe_changes(changes)
    return Action(
        kind="inventory",
        reply="✅ Закупка в холодильнике:\n" + "\n".join(f"• {line}" for line in lines),
        undo_batch=batch_id,
        lines=lines,
    )


# --- форматирование команд ---

def format_fridge(database: db_module.Database, tz_offset: int = 3) -> str:
    items = database.list_inventory()
    in_stock = [i for i in items if i["qty"] is None or i["qty"] > 0]
    out_of_stock = [i for i in items if i["qty"] is not None and i["qty"] <= 0]
    if not in_stock and not out_of_stock:
        return "Холодильник пуст — записей нет."
    parts = []
    if in_stock:
        parts.append("🧊 В холодильнике:")
        parts += [f"• {i['name']} — {fmt_qty(i['qty'], i['unit'])}" for i in in_stock]
    if out_of_stock:
        recent = sorted(out_of_stock, key=lambda i: i["updated_at"], reverse=True)[:10]
        parts.append("\n🚫 Кончилось:")
        parts += [f"• {i['name']}" for i in recent]
    return "\n".join(parts)


def format_diary(database: db_module.Database, n: int = 7, tz_offset: int = 3) -> str:
    meals = database.last_meals(n)
    if not meals:
        return "Дневник пуст — записей о еде нет."
    parts = [f"🍽 Последние приёмы ({len(meals)}):"]
    for meal in meals:
        when = utc_to_local(meal["ts"], tz_offset)
        line = (f"\n{when} — {meal['description']}\n"
                f"   сытость {fmt_score(meal['satiety'])} · вкус {fmt_score(meal['taste'])}")
        if meal["notes"]:
            line += f"\n   💬 {meal['notes']}"
        parts.append(line)
    return "\n".join(parts)


def format_today(database: db_module.Database, tz_offset: int = 3) -> str:
    today = local_date(tz_offset).isoformat()
    plan = database.plan_for(today)
    if not plan:
        return "Плана пока нет."
    header = "📋 План на сегодня:" if plan["date_for"] == today else (
        f"📋 Последний план (на {plan['date_for']}):" if plan["date_for"]
        else "📋 Последний план:"
    )
    author = f" — от {plan['author']}" if plan["author"] else ""
    return f"{header}{author}\n\n{plan['text']}"
