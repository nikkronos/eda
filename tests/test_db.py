import unittest

from bot.db import Database


class DbTest(unittest.TestCase):
    def setUp(self):
        self.db = Database(":memory:")

    def tearDown(self):
        self.db.close()

    def test_settings_roundtrip(self):
        self.assertIsNone(self.db.get_setting("group_chat_id"))
        self.db.set_setting("group_chat_id", "-100123")
        self.assertEqual(self.db.get_setting("group_chat_id"), "-100123")
        self.db.set_setting("group_chat_id", "-100456")
        self.assertEqual(self.db.get_setting("group_chat_id"), "-100456")

    def test_add_creates_item(self):
        _, changes = self.db.apply_ops(
            [{"name": "Огурцы", "op": "add", "qty": 3, "unit": "шт"}], "test"
        )
        self.assertEqual(len(changes), 1)
        item = self.db.get_item("огурцы")  # регистронезависимо
        self.assertEqual(item["qty"], 3)
        self.assertEqual(item["unit"], "шт")

    def test_add_accumulates(self):
        self.db.apply_ops([{"name": "Яйца", "op": "add", "qty": 10, "unit": "шт"}], "t")
        self.db.apply_ops([{"name": "яйца", "op": "add", "qty": 10}], "t")
        self.assertEqual(self.db.get_item("Яйца")["qty"], 20)

    def test_subtract_and_clamp(self):
        self.db.apply_ops([{"name": "Греча", "op": "add", "qty": 4, "unit": "пакетик"}], "t")
        self.db.apply_ops([{"name": "греча", "op": "subtract", "qty": 1}], "t")
        self.assertEqual(self.db.get_item("греча")["qty"], 3)
        self.db.apply_ops([{"name": "греча", "op": "subtract", "qty": 99}], "t")
        self.assertEqual(self.db.get_item("греча")["qty"], 0)

    def test_subtract_missing_item_skipped(self):
        _, changes = self.db.apply_ops([{"name": "лобстер", "op": "subtract", "qty": 1}], "t")
        self.assertEqual(changes, [])
        self.assertIsNone(self.db.get_item("лобстер"))

    def test_subtract_mismatched_unit_degrades_to_portion(self):
        self.db.apply_ops([{"name": "курица", "op": "add", "qty": 400, "unit": "г"}], "t")
        self.db.apply_ops([{"name": "курица", "op": "subtract", "qty": 1, "unit": "шт"}], "t")
        item = self.db.get_item("курица")
        self.assertIsNone(item["qty"])       # не 399: единицы несовместимы → «есть»
        self.assertEqual(item["unit"], "г")  # единица хранения не перезаписана

    def test_subtract_unknown_qty_stays_unknown(self):
        self.db.apply_ops([{"name": "Сыр", "op": "add"}], "t")  # qty неизвестно
        self.assertIsNone(self.db.get_item("сыр")["qty"])
        self.db.apply_ops([{"name": "сыр", "op": "subtract", "qty": 1}], "t")
        self.assertIsNone(self.db.get_item("сыр")["qty"])

    def test_set_and_deplete(self):
        self.db.apply_ops([{"name": "Творог", "op": "add", "qty": 2, "unit": "уп"}], "t")
        self.db.apply_ops([{"name": "творог", "op": "set", "qty": 1}], "t")
        self.assertEqual(self.db.get_item("творог")["qty"], 1)
        self.db.apply_ops([{"name": "творог", "op": "deplete"}], "t")
        self.assertEqual(self.db.get_item("творог")["qty"], 0)

    def test_undo_restores_previous_state(self):
        self.db.apply_ops([{"name": "Молоко", "op": "add", "qty": 2, "unit": "л"}], "t")
        batch, _ = self.db.apply_ops([
            {"name": "молоко", "op": "subtract", "qty": 1},
            {"name": "Хлеб", "op": "add", "qty": 1, "unit": "шт"},
        ], "t")
        self.assertTrue(self.db.undo_batch(batch))
        self.assertEqual(self.db.get_item("молоко")["qty"], 2)
        self.assertIsNone(self.db.get_item("Хлеб"))  # не существовал — удалён

    def test_undo_twice_is_noop(self):
        batch, _ = self.db.apply_ops([{"name": "Кефир", "op": "add", "qty": 1}], "t")
        self.assertTrue(self.db.undo_batch(batch))
        self.assertFalse(self.db.undo_batch(batch))

    def test_meals(self):
        self.db.add_meal("греча с курицей", 4, 3.5, "кислинка", raw_id=1)
        self.db.add_meal("овсянка", 5, 2, None)
        meals = self.db.last_meals(5)
        self.assertEqual(len(meals), 2)
        self.assertEqual(meals[0]["description"], "овсянка")  # свежие первыми
        self.assertEqual(meals[1]["satiety"], 4)
        self.assertEqual(meals[1]["notes"], "кислинка")

    def test_delete_meal(self):
        meal_id = self.db.add_meal("обед", 4, 3, None)
        self.assertTrue(self.db.delete_meal(meal_id))
        self.assertEqual(self.db.last_meals(5), [])
        self.assertFalse(self.db.delete_meal(meal_id))  # повторно — нечего удалять

    def test_plans_pick_by_date_then_latest(self):
        self.db.add_plan("Богдан", "план на завтра", "2026-07-06")
        self.db.add_plan("Богдан", "план на сегодня", "2026-07-05")
        plan = self.db.plan_for("2026-07-05")
        self.assertEqual(plan["text"], "план на сегодня")
        plan = self.db.plan_for("2026-07-09")  # нет точного — последний
        self.assertEqual(plan["text"], "план на сегодня")

    def test_pending_single_use(self):
        pid = self.db.add_pending("shopping", {"ops": [{"name": "х", "op": "add"}]})
        payload = self.db.take_pending(pid)
        self.assertIsNotNone(payload)
        self.assertIsNone(self.db.take_pending(pid))  # одноразово

    def test_raw_messages(self):
        raw_id = self.db.save_raw(1, 2, 3, "Никита", "текст")
        self.db.set_raw_kind(raw_id, "meal")
        row = self.db.conn.execute(
            "SELECT * FROM raw_messages WHERE id = ?", (raw_id,)
        ).fetchone()
        self.assertEqual(row["kind"], "meal")
        self.assertEqual(row["author"], "Никита")


if __name__ == "__main__":
    unittest.main()
