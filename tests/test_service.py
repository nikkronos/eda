import unittest

from bot import service
from bot.db import Database


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_meal_saved_and_ops_applied(self):
        self.db.apply_ops([{"name": "греча", "op": "add", "qty": 4, "unit": "пакетик"}], "seed")
        parsed = {
            "kind": "meal",
            "meal": {"description": "греча с курицей", "satiety": 4, "taste": 3.5,
                     "notes": "кислинка"},
            "inventory_ops": [{"name": "греча", "op": "subtract", "qty": 1}],
        }
        action = service.apply_parsed(self.db, parsed, "Никита", raw_id=1)
        self.assertEqual(action.kind, "meal")
        self.assertEqual(action.reaction, "✍")
        self.assertIsNotNone(action.undo_batch)
        self.assertIn("греча", action.reply)
        self.assertEqual(self.db.get_item("греча")["qty"], 3)
        self.assertEqual(self.db.last_meals(1)[0]["satiety"], 4)

    def test_meal_undo_removes_diary_entry_too(self):
        self.db.apply_ops([{"name": "спагетти", "op": "add", "qty": 500, "unit": "г"}], "seed")
        parsed = {
            "kind": "meal",
            "meal": {"description": "спагетти с курицей", "satiety": 5, "taste": 3},
            "inventory_ops": [{"name": "спагетти", "op": "subtract"}],
        }
        action = service.apply_parsed(self.db, parsed, "Никита", raw_id=9)
        self.assertIsNotNone(action.meal_id)
        # пользователь жмёт «Отменить»: откатываются и склад, и дневник
        self.assertTrue(self.db.undo_batch(action.undo_batch))
        self.assertTrue(self.db.delete_meal(action.meal_id))
        self.assertEqual(self.db.get_item("спагетти")["qty"], 500)
        self.assertEqual(self.db.last_meals(5), [])

    def test_meal_correction_updates_last_entry_without_ops(self):
        parsed = {"kind": "meal",
                  "meal": {"description": "обед", "satiety": 4, "taste": 3,
                           "notes": "не хватает соуса"}}
        service.apply_parsed(self.db, parsed, "Никита", raw_id=1)
        correction = {"kind": "meal",
                      "meal": {"description": "обед", "satiety": 3, "taste": 3,
                               "is_correction": True}}
        action = service.apply_parsed(self.db, correction, "Никита", raw_id=2)
        self.assertIn("обновлена", action.reply)
        meals = self.db.last_meals(5)
        self.assertEqual(len(meals), 1)          # не дубль, а обновление
        self.assertEqual(meals[0]["satiety"], 3)
        self.assertEqual(meals[0]["notes"], "не хватает соуса")  # заметка не затёрта

    def test_meal_correction_with_empty_diary_falls_back_to_insert(self):
        correction = {"kind": "meal",
                      "meal": {"description": "обед", "satiety": 3,
                               "is_correction": True}}
        action = service.apply_parsed(self.db, correction, "Никита", raw_id=2)
        self.assertEqual(action.kind, "meal")
        self.assertEqual(len(self.db.last_meals(5)), 1)

    def test_meal_without_ops_is_silent_reaction_only(self):
        parsed = {"kind": "meal", "meal": {"description": "банан", "satiety": 3}}
        action = service.apply_parsed(self.db, parsed, "Никита", raw_id=1)
        self.assertEqual(action.reaction, "✍")
        self.assertIsNone(action.reply)

    def test_meal_empty_description_becomes_chatter(self):
        action = service.apply_parsed(self.db, {"kind": "meal", "meal": {}}, "Никита", 1)
        self.assertEqual(action.kind, "chatter")
        self.assertEqual(self.db.last_meals(1), [])

    def test_plan_saved(self):
        parsed = {"kind": "plan", "plan": {"text": "Лепёшки, овощи, йогурт",
                                           "date_for": "2026-07-06"}}
        action = service.apply_parsed(self.db, parsed, "Богдан", raw_id=2)
        self.assertEqual(action.kind, "plan")
        self.assertEqual(action.reaction, "👌")
        plan = self.db.plan_for("2026-07-06")
        self.assertEqual(plan["author"], "Богдан")

    def test_shopping_list_creates_pending_not_inventory(self):
        parsed = {
            "kind": "shopping_list",
            "inventory_ops": [
                {"name": "огурцы", "op": "add", "qty": 3, "unit": "шт"},
                {"name": "творог", "op": "add", "qty": 1, "unit": "уп"},
            ],
        }
        action = service.apply_parsed(self.db, parsed, "Богдан", raw_id=3)
        self.assertEqual(action.kind, "shopping_list")
        self.assertIsNotNone(action.pending_id)
        self.assertIsNone(self.db.get_item("огурцы"))  # до кнопки ничего не добавлено

        confirm = service.confirm_shopping(self.db, action.pending_id)
        self.assertEqual(self.db.get_item("огурцы")["qty"], 3)
        self.assertIsNotNone(confirm.undo_batch)

        repeat = service.confirm_shopping(self.db, action.pending_id)
        self.assertEqual(repeat.kind, "noop")  # повторный клик не задвоит

    def test_inventory_direct_update(self):
        self.db.apply_ops([{"name": "огурцы", "op": "add", "qty": 3, "unit": "шт"}], "seed")
        parsed = {"kind": "inventory",
                  "inventory_ops": [{"name": "огурцы", "op": "deplete"}]}
        action = service.apply_parsed(self.db, parsed, "Никита", raw_id=4)
        self.assertIn("кончилось", action.reply)
        self.assertEqual(self.db.get_item("огурцы")["qty"], 0)

    def test_chatter_is_fully_silent(self):
        action = service.apply_parsed(self.db, {"kind": "chatter"}, "Богдан", raw_id=5)
        self.assertIsNone(action.reply)
        self.assertIsNone(action.reaction)

    def test_format_fridge_sections(self):
        self.db.apply_ops([
            {"name": "яйца", "op": "add", "qty": 10, "unit": "шт"},
            {"name": "сыр", "op": "add"},
            {"name": "огурцы", "op": "add", "qty": 2, "unit": "шт"},
        ], "seed")
        self.db.apply_ops([{"name": "огурцы", "op": "deplete"}], "seed")
        text = service.format_fridge(self.db)
        self.assertIn("яйца — 10 шт", text)
        self.assertIn("сыр — есть", text)
        self.assertIn("Кончилось", text)
        self.assertIn("огурцы", text.split("Кончилось")[1])

    def test_format_fridge_empty(self):
        self.assertIn("пуст", service.format_fridge(self.db))

    def test_format_diary(self):
        self.db.add_meal("овсянка с бананом", 3.5, 2, "кисло местами")
        text = service.format_diary(self.db, 5)
        self.assertIn("овсянка", text)
        self.assertIn("3.5/5", text)
        self.assertIn("2/5", text)
        self.assertIn("кисло местами", text)

    def test_format_today_empty(self):
        self.assertIn("нет", service.format_today(self.db))

    def test_fmt_qty(self):
        self.assertEqual(service.fmt_qty(None, None), "есть")
        self.assertEqual(service.fmt_qty(3.0, "шт"), "3 шт")
        self.assertEqual(service.fmt_qty(2.5, None), "2.5")

    def test_describe_changes_transition(self):
        lines = service.describe_changes([
            {"name": "греча", "op": "subtract", "qty_before": 4, "qty_after": 3,
             "unit": "пакетик", "unit_before": "пакетик"},
            {"name": "огурцы", "op": "deplete", "qty_before": 2, "qty_after": 0,
             "unit": "шт", "unit_before": "шт"},
        ])
        self.assertEqual(lines[0], "греча: 4 пакетик → 3 пакетик")
        self.assertEqual(lines[1], "огурцы: кончилось")

    def test_describe_changes_unit_switch_shows_old_unit(self):
        lines = service.describe_changes([
            {"name": "микс овощей", "op": "set", "qty_before": 1, "qty_after": 400,
             "unit": "г", "unit_before": "уп"},
        ])
        self.assertEqual(lines[0], "микс овощей: 1 уп → 400 г")

    def test_describe_changes_no_change(self):
        lines = service.describe_changes([
            {"name": "приправа", "op": "set", "qty_before": 40, "qty_after": 40,
             "unit": "г", "unit_before": "г"},
        ])
        self.assertEqual(lines[0], "приправа: 40 г — без изменений")


if __name__ == "__main__":
    unittest.main()
