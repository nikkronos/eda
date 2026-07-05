"""Хранилище: SQLite (stdlib). Все времена в БД — UTC ISO."""

import json
import sqlite3
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS raw_messages (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER,
    msg_id  INTEGER,
    user_id INTEGER,
    author  TEXT,
    text    TEXT,
    kind    TEXT,
    ts      TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS meals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT DEFAULT (datetime('now')),
    description TEXT NOT NULL,
    satiety     REAL,
    taste       REAL,
    notes       TEXT,
    raw_id      INTEGER
);
CREATE TABLE IF NOT EXISTS inventory (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    qty        REAL,
    unit       TEXT,
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS inventory_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    item_name     TEXT NOT NULL,
    existed_before INTEGER NOT NULL,
    qty_before    REAL,
    unit_before   TEXT,
    qty_after     REAL,
    unit_after    TEXT,
    reason        TEXT,
    undone        INTEGER NOT NULL DEFAULT 0,
    ts            TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_invlog_batch ON inventory_log(batch_id);
CREATE TABLE IF NOT EXISTS plans (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts TEXT DEFAULT (datetime('now')),
    author     TEXT,
    text       TEXT NOT NULL,
    date_for   TEXT,
    active     INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS pending (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    used       INTEGER NOT NULL DEFAULT 0,
    created_ts TEXT DEFAULT (datetime('now'))
);
"""


def norm_name(name: str) -> str:
    """SQLite NOCASE не сворачивает кириллицу — нормализуем на стороне Python."""
    return " ".join(name.split()).lower()


class Database:
    def __init__(self, path: str = ":memory:"):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # --- settings ---

    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    # --- raw messages ---

    def save_raw(self, chat_id: int, msg_id: int, user_id: int, author: str,
                 text: str, kind: str = "new") -> int:
        cur = self.conn.execute(
            "INSERT INTO raw_messages(chat_id, msg_id, user_id, author, text, kind) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (chat_id, msg_id, user_id, author, text, kind),
        )
        self.conn.commit()
        return cur.lastrowid

    def set_raw_kind(self, raw_id: int, kind: str) -> None:
        self.conn.execute("UPDATE raw_messages SET kind = ? WHERE id = ?", (kind, raw_id))
        self.conn.commit()

    # --- meals ---

    def add_meal(self, description: str, satiety: float | None, taste: float | None,
                 notes: str | None, raw_id: int | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO meals(description, satiety, taste, notes, raw_id) VALUES(?, ?, ?, ?, ?)",
            (description, satiety, taste, notes, raw_id),
        )
        self.conn.commit()
        return cur.lastrowid

    def last_meals(self, n: int = 7) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM meals ORDER BY id DESC LIMIT ?", (n,)
        ).fetchall()

    # --- inventory ---

    def get_item(self, name: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM inventory WHERE name = ?", (norm_name(name),)
        ).fetchone()

    def list_inventory(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM inventory ORDER BY name").fetchall()

    def _write_item(self, name: str, qty: float | None, unit: str | None) -> None:
        self.conn.execute(
            "INSERT INTO inventory(name, qty, unit, updated_at) "
            "VALUES(?, ?, ?, datetime('now')) "
            "ON CONFLICT(name) DO UPDATE SET qty = excluded.qty, unit = excluded.unit, "
            "updated_at = excluded.updated_at",
            (norm_name(name), qty, unit),
        )

    def apply_ops(self, ops: list[dict], reason: str) -> tuple[str, list[dict]]:
        """Применяет операции [{name, op, qty, unit}] одним батчем.

        op: add | subtract | set | deplete. qty None у add/subtract = количество
        неизвестно. Возвращает (batch_id, changes) для текста ответа и Undo.
        """
        batch_id = uuid.uuid4().hex
        changes: list[dict] = []
        for op in ops:
            name = (op.get("name") or "").strip()
            action = op.get("op")
            if not name or action not in ("add", "subtract", "set", "deplete"):
                continue
            qty = op.get("qty")
            unit = op.get("unit")
            item = self.get_item(name)
            existed = item is not None
            qty_before = item["qty"] if item else None
            unit_before = item["unit"] if item else None
            canonical = item["name"] if item else norm_name(name)

            if action == "subtract" and not existed:
                continue  # нечего списывать — не выдумываем позицию
            if action == "deplete" and not existed:
                continue

            if action == "add":
                new_qty = qty if qty_before is None else (
                    qty_before + qty if qty is not None else qty_before
                )
            elif action == "subtract":
                new_qty = None if qty_before is None or qty is None else max(0.0, qty_before - qty)
            elif action == "set":
                new_qty = qty
            else:  # deplete
                new_qty = 0.0

            new_unit = unit or unit_before
            self._write_item(canonical, new_qty, new_unit)
            self.conn.execute(
                "INSERT INTO inventory_log(batch_id, item_name, existed_before, qty_before, "
                "unit_before, qty_after, unit_after, reason) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                (batch_id, canonical, int(existed), qty_before, unit_before,
                 new_qty, new_unit, reason),
            )
            changes.append({
                "name": canonical, "op": action,
                "qty_before": qty_before, "qty_after": new_qty, "unit": new_unit,
            })
        self.conn.commit()
        return batch_id, changes

    def undo_batch(self, batch_id: str) -> bool:
        """Откатывает батч к состоянию до применения. False — нечего откатывать."""
        rows = self.conn.execute(
            "SELECT * FROM inventory_log WHERE batch_id = ? AND undone = 0 ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        if not rows:
            return False
        for row in rows:
            if row["existed_before"]:
                self._write_item(row["item_name"], row["qty_before"], row["unit_before"])
            else:
                self.conn.execute(
                    "DELETE FROM inventory WHERE name = ?", (row["item_name"],)
                )
        self.conn.execute(
            "UPDATE inventory_log SET undone = 1 WHERE batch_id = ?", (batch_id,)
        )
        self.conn.commit()
        return True

    # --- plans ---

    def add_plan(self, author: str, text: str, date_for: str | None) -> int:
        cur = self.conn.execute(
            "INSERT INTO plans(author, text, date_for) VALUES(?, ?, ?)",
            (author, text, date_for),
        )
        self.conn.commit()
        return cur.lastrowid

    def plan_for(self, date_iso: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM plans WHERE active = 1 AND date_for = ? ORDER BY id DESC LIMIT 1",
            (date_iso,),
        ).fetchone()
        if row:
            return row
        return self.conn.execute(
            "SELECT * FROM plans WHERE active = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()

    # --- pending (отложенные действия, напр. «купил всё» по списку закупки) ---

    def add_pending(self, kind: str, payload: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO pending(kind, payload) VALUES(?, ?)",
            (kind, json.dumps(payload, ensure_ascii=False)),
        )
        self.conn.commit()
        return cur.lastrowid

    def take_pending(self, pending_id: int) -> dict | None:
        """Возвращает payload и помечает использованным (одноразово)."""
        row = self.conn.execute(
            "SELECT * FROM pending WHERE id = ? AND used = 0", (pending_id,)
        ).fetchone()
        if not row:
            return None
        self.conn.execute("UPDATE pending SET used = 1 WHERE id = ?", (pending_id,))
        self.conn.commit()
        return json.loads(row["payload"])
