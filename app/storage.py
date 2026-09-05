"""结果持久化：SQLite 保存每次实验运行结果。"""
from __future__ import annotations

import json
import os
import sqlite3

from .config import DATA_DIR

DB_FILE = os.path.join(DATA_DIR, "results.db")


class Storage:
    def __init__(self, db_file: str = DB_FILE):
        os.makedirs(os.path.dirname(db_file), exist_ok=True)
        self.conn = sqlite3.connect(db_file, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def _init(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT,
                payload TEXT
            )""")
        self.conn.commit()

    def save(self, result: dict) -> int:
        cur = self.conn.execute(
            "INSERT INTO results (ts, payload) VALUES (?, ?)",
            (result.get("ts", ""), json.dumps(result, ensure_ascii=False)),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_all(self) -> list[dict]:
        rows = self.conn.execute("SELECT id, ts, payload FROM results ORDER BY id").fetchall()
        out = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except Exception:
                payload = {}
            payload["id"] = r["id"]
            payload["_ts"] = r["ts"]
            out.append(payload)
        return out

    def get(self, rid: int) -> dict | None:
        row = self.conn.execute("SELECT payload FROM results WHERE id=?", (rid,)).fetchone()
        if not row:
            return None
        try:
            d = json.loads(row["payload"])
        except Exception:
            d = {}
        d["id"] = rid
        return d

    def delete_all(self) -> int:
        cur = self.conn.execute("DELETE FROM results")
        self.conn.commit()
        return cur.rowcount

    def delete(self, rid: int) -> int:
        cur = self.conn.execute("DELETE FROM results WHERE id=?", (rid,))
        self.conn.commit()
        return cur.rowcount
