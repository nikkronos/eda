"""LLM-разбор свободных сообщений в структуру (Claude Haiku, structured outputs)."""

import json
import logging

import anthropic

log = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 3000

SYSTEM_PROMPT = """\
Ты — парсер сообщений кулинарного телеграм-чата. В чате двое: Никита (учится \
готовить, отчитывается о еде) и Богдан (повар, даёт планы готовки и списки закупок).

Разбери одно сообщение и верни JSON по схеме.

kind:
- "meal" — автор поел / приготовил и описывает еду или оценки (сытость, вкус).
- "plan" — инструкция или план готовки («на завтра…», рецепт с шагами, что приготовить).
- "shopping_list" — список продуктов к покупке.
- "inventory" — изменение запаса еды: купил(а), кончилось, «добавь…», «осталось N»,
  «есть/лежит/стоит N …», перечисление продуктов, которые есть дома.
- "chatter" — всё остальное: болтовня, вопросы, обсуждение, реакции.

Правила:
- «Холодильник» — условное имя ВСЕГО запаса еды дома: полки, шкаф, стол, кладовка,
  морозилка. Уточнения вида «не в холодильнике, а рядом / на столе / в шкафу» НЕ
  отменяют учёт — это всё равно "inventory".
- Числа прописью («два», «пара», «десяток») переводи в цифры.
- Оценки вида «4/5», «4,5/5» → числа 0–5. «сытость X» → meal.satiety, «вкус Y» → meal.taste.
  Заполняй satiety/taste ТОЛЬКО если число явно написано в сообщении. Слово
  «сытость»/«вкус» без числа рядом — поле НЕ заполняй, никогда не выдумывай оценку.
- Поправка УЖЕ отправленного отчёта о еде («поправка», «ещё поправка», «исправляю»,
  повтор того же приёма с новыми оценками) — это "meal" с meal.is_correction=true
  и БЕЗ inventory_ops: продукты уже списаны первой версией отчёта, не списывай их
  повторно.
- Если автор пишет, что продукт ещё остался («курица ещё осталась») — это НЕ
  повод списывать; остаток не обнуляй.
- Для "meal" заполни inventory_ops — что списать из холодильника исходя из съеденного.
  Сопоставляй с переданным списком холодильника и используй ТОЧНЫЕ имена из него.
  Если подходящей позиции в холодильнике нет — операцию не создавай.
- Списывай консервативно. Штучное, съедаемое целиком (яйца, огурцы, бананы,
  яблоки) — op="subtract" с числом штук.
- Позиции в упаковках/банках/пачках (йогурт, творог, сыр, клубника, крупы, соусы)
  при порции — op="subtract" БЕЗ qty: количество станет «есть, точно неизвестно».
  subtract с qty или "deplete" по таким позициям — ТОЛЬКО если автор явно сказал,
  что употребил упаковку целиком или что это было последнее («доел», «кончилось»,
  «пустая упаковка»). Одна порция НИКОГДА не обнуляет упаковку.
- Автор может поправлять учёт: «нет, X не кончился», «X ещё есть», «верни X» —
  это "inventory", op="set" (с числом, если названо, иначе без qty — станет «есть»).
  Позиции с пометкой «кончилось» в списке холодильника можно так возвращать.
- «Было X г» у ВСКРЫТОЙ упаковки — исходный размер упаковки, НЕ остаток. Остаток
  бери только из «сейчас/осталось Y». Если у вскрытой упаковки остаток не назван,
  а у позиции в холодильнике уже стоит число — не перезаписывай его размером
  упаковки (op не создавай или set без qty). Для ЗАКРЫТОЙ или новой позиции
  размер упаковки = текущее количество (add/set с X).
- Для "shopping_list": inventory_ops = позиции списка, op="add", количество из текста
  («огурцы 3/4 штуки» → qty 3). Эти операции применятся ПОСЛЕ покупки, по кнопке.
- При ДОБАВЛЕНИИ (закупка, «купил X») сопоставляй покупку с существующими позициями
  холодильника, включая помеченные «кончилось»: один и тот же продукт под другим
  названием («филе грудки цыплят» = «курица», «помидорки» = «черри-помидоры») —
  используй СУЩЕСТВУЮЩЕЕ имя из списка, не создавай дубль. Новое имя — только для
  действительно новых продуктов.
- Для "inventory": кончилось → "deplete", купил/принёс → "add", осталось N → "set".
- Для "plan": plan.text — суть плана без потерь (шаги сохраняй), plan.date_for —
  дата ISO (YYYY-MM-DD), если однозначна из текста и текущей даты, иначе null.
- Единицы: шт, уп, г, кг, мл, л, пакетик, порция, банка. Не уверен — null.
- Обрывочные телеграфные реплики без явного факта («Овощи ещё», «и сыр», «ага»,
  «норм», «понял») — всегда "chatter". Для "inventory" в сообщении должен быть
  явный сигнал: глагол изменения или наличия (есть, купил, принёс, кончилось,
  осталось, добавь, вскрыл) либо количество. Такой обрывок может продолжать
  соседнее сообщение, которого ты не видишь, — не догадывайся за автора.
- Сомневаешься в kind — выбирай "chatter": сообщение всё равно сохранится.
"""

RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["meal", "plan", "shopping_list", "inventory", "chatter"],
        },
        "meal": {
            "type": "object",
            "additionalProperties": False,
            "required": ["description"],
            "properties": {
                "description": {"type": "string"},
                "satiety": {"type": "number"},
                "taste": {"type": "number"},
                "notes": {"type": "string"},
                "is_correction": {"type": "boolean"},
            },
        },
        "inventory_ops": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "op"],
                "properties": {
                    "name": {"type": "string"},
                    "op": {"type": "string", "enum": ["add", "subtract", "set", "deplete"]},
                    "qty": {"type": "number"},
                    "unit": {"type": "string"},
                },
            },
        },
        "plan": {
            "type": "object",
            "additionalProperties": False,
            "required": ["text"],
            "properties": {
                "text": {"type": "string"},
                "date_for": {"type": "string"},
            },
        },
    },
}


def build_user_prompt(text: str, author: str, now_local: str,
                      inventory_lines: list[str]) -> str:
    inventory = "\n".join(inventory_lines) if inventory_lines else "(пусто)"
    return (
        f"Дата и время (локальные): {now_local}\n"
        f"Автор сообщения: {author}\n\n"
        f"Холодильник сейчас:\n{inventory}\n\n"
        f"Сообщение:\n{text[:MAX_MESSAGE_CHARS]}"
    )


class Parser:
    def __init__(self, api_key: str, model: str = "claude-haiku-4-5"):
        self.client = anthropic.AsyncAnthropic(api_key=api_key)
        self.model = model

    async def parse(self, text: str, author: str, now_local: str,
                    inventory_lines: list[str]) -> dict | None:
        """Возвращает dict по RESULT_SCHEMA или None при ошибке (сообщение не теряется)."""
        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=SYSTEM_PROMPT,
                output_config={"format": {"type": "json_schema", "schema": RESULT_SCHEMA}},
                messages=[{
                    "role": "user",
                    "content": build_user_prompt(text, author, now_local, inventory_lines),
                }],
            )
        except anthropic.RateLimitError:
            log.warning("LLM rate limit")
            return None
        except anthropic.APIStatusError as exc:
            log.error("LLM API error %s: %s", exc.status_code, exc.message)
            return None
        except anthropic.APIConnectionError:
            log.error("LLM connection error")
            return None

        raw = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            log.error("LLM вернул не-JSON: %.200s", raw)
            return None
