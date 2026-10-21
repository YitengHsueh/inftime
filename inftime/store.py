"""
本地存储层 —— 全部落在用户自己机器的 SQLite 文件里，默认不上云。

这是 Inftime 与豆包 / 飞书最本质的差别：日程和消息是一个人最私密的数据之一，
所以「不出本机」不是妥协，是产品主张。
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional

from .schema import Event

DEFAULT_DB = Path(os.environ.get("INFTIME_HOME", Path.home() / ".inftime")) / "inftime.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    fingerprint     TEXT UNIQUE,
    title           TEXT NOT NULL,
    start           TEXT,
    end             TEXT,
    all_day         INTEGER DEFAULT 0,
    location        TEXT,
    participants    TEXT,
    source_channel  TEXT,
    source_ref      TEXT,
    raw_text        TEXT,
    is_deadline     INTEGER DEFAULT 0,
    time_granularity TEXT,
    priority        INTEGER DEFAULT 3,
    confidence      REAL DEFAULT 0,
    needs_clarification INTEGER DEFAULT 0,
    clarify_question TEXT,
    status          TEXT DEFAULT 'pending',
    extractor       TEXT,
    notes           TEXT,
    verified        TEXT,
    created_at      TEXT,
    updated_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_status ON events(status);
CREATE INDEX IF NOT EXISTS idx_events_start  ON events(start);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# 老库补列。SQLite 没有「ADD COLUMN IF NOT EXISTS」，
# 所以先读 PRAGMA 再决定加不加 —— 重复执行是安全的（幂等）。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (表, 列, 类型)
    ("events", "verified", "TEXT"),
    # 通过快捷指令写进日历的时间。**必须记**：快捷指令是直接写日历，
    # 没有任何去重（ICS 靠 UID 去重，这条路没有）。不记的话，
    # 每小时轮询会把同一条事件重复写 N 遍。
    ("events", "pushed_at", "TEXT"),
)


class Store:
    def __init__(self, path: Optional[Path | str] = None):
        self.path = Path(path) if path else DEFAULT_DB
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        """给老库补上后来新增的列。

        为什么必须做：用户已经有一条真实数据（2026-10-20 年级大会）。
        不能因为加了 `verified` 列就让老库打不开。
        `ALTER TABLE ... ADD COLUMN` 对已有行填 NULL，正好是我们要的语义
        （「还没核实过」）。
        """
        for table, column, coltype in _MIGRATIONS:
            cols = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if not cols:
                continue                      # 表还没建出来，跳过
            if column not in cols:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    # ---------- 写 ----------

    def add(self, ev: Event, *, dedupe: bool = True) -> tuple[Event, bool]:
        """写入事件。返回 (事件, 是否新建)。dedupe=True 时同 fingerprint 视为重复。"""
        if dedupe:
            row = self.conn.execute(
                "SELECT * FROM events WHERE fingerprint = ?", (ev.fingerprint,)
            ).fetchone()
            if row:
                return _row_to_event(row), False

        self.conn.execute(
            """INSERT OR REPLACE INTO events
               (id, fingerprint, title, start, end, all_day, location, participants,
                source_channel, source_ref, raw_text, is_deadline, time_granularity,
                priority, confidence, needs_clarification, clarify_question,
                status, extractor, notes, verified, pushed_at, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                ev.id, ev.fingerprint, ev.title, ev.start, ev.end, int(ev.all_day),
                ev.location, json.dumps(ev.participants, ensure_ascii=False),
                ev.source_channel, ev.source_ref, ev.raw_text, int(ev.is_deadline),
                ev.time_granularity, ev.priority, ev.confidence,
                int(ev.needs_clarification), ev.clarify_question,
                ev.status, ev.extractor, ev.notes,
                json.dumps(ev.verified, ensure_ascii=False) if ev.verified else None,
                ev.pushed_at,
                ev.created_at,
                datetime.now().astimezone().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        return ev, True

    def add_many(self, events: Iterable[Event]) -> list[tuple[Event, bool]]:
        return [self.add(e) for e in events]

    def set_status(self, event_id: str, status: str) -> Optional[Event]:
        self.conn.execute(
            "UPDATE events SET status = ?, updated_at = ? WHERE id = ?",
            (status, datetime.now().astimezone().isoformat(timespec="seconds"), event_id),
        )
        self.conn.commit()
        return self.get(event_id)

    def patch(self, event_id: str, **fields) -> Optional[Event]:
        allowed = {"title", "start", "end", "location", "priority", "notes", "status"}
        sets = {k: v for k, v in fields.items() if k in allowed}
        if not sets:
            return self.get(event_id)
        sets["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        clause = ", ".join(f"{k} = ?" for k in sets)
        self.conn.execute(f"UPDATE events SET {clause} WHERE id = ?", (*sets.values(), event_id))
        self.conn.commit()
        return self.get(event_id)

    def set_verified(self, event_id: str, data: Optional[dict]) -> Optional[Event]:
        """写入（或清除）外部核实结果。

        ⚠️ 刻意**不碰 `start` / `location` / `fingerprint`**。
        核实是「另外知道的事实」，不是「原来读错了」——
        混在一起会让去重键随核实结果漂移。详见 `Event.verified`。
        """
        self.conn.execute(
            "UPDATE events SET verified = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False) if data else None,
             datetime.now().astimezone().isoformat(timespec="seconds"), event_id),
        )
        self.conn.commit()
        return self.get(event_id)

    def set_pushed(self, event_id: str, when: Optional[str] = None) -> Optional[Event]:
        """记录「这条已经通过快捷指令写进日历了」。

        这是快捷指令路线的**防重复**机制：那条路没有任何去重
        （ICS 导入靠 UID，快捷指令是裸写），所以必须由我们自己记。
        """
        stamp = when or datetime.now().astimezone().isoformat(timespec="seconds")
        self.conn.execute(
            "UPDATE events SET pushed_at = ?, updated_at = ? WHERE id = ?",
            (stamp, stamp, event_id),
        )
        self.conn.commit()
        return self.get(event_id)

    # ---------- 读 ----------

    def get(self, event_id: str) -> Optional[Event]:
        row = self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return _row_to_event(row) if row else None

    def by_id_prefix(self, prefix: str) -> Optional[Event]:
        row = self.conn.execute(
            "SELECT * FROM events WHERE id LIKE ? ORDER BY created_at DESC LIMIT 1", (prefix + "%",)
        ).fetchone()
        return _row_to_event(row) if row else None

    def list(
        self,
        *,
        status: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 200,
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE 1=1"
        args: list = []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if since:
            sql += " AND (start IS NULL OR start >= ?)"
            args.append(since)
        if until:
            sql += " AND (start IS NULL OR start <= ?)"
            args.append(until)
        sql += " ORDER BY (start IS NULL), start ASC, created_at DESC LIMIT ?"
        args.append(limit)
        return [_row_to_event(r) for r in self.conn.execute(sql, args).fetchall()]

    def today(self) -> list[Event]:
        now = datetime.now().astimezone()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        return self.list(since=start.isoformat(timespec="seconds"),
                         until=end.isoformat(timespec="seconds"))

    def upcoming(self, days: int = 7) -> list[Event]:
        now = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        return self.list(status="pending", since=now.isoformat(timespec="seconds"),
                         until=(now + timedelta(days=days)).isoformat(timespec="seconds"))

    def stats(self) -> dict:
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM events GROUP BY status"
        ).fetchall()
        by_status = {r["status"]: r["n"] for r in rows}
        total = sum(by_status.values())
        no_time = self.conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE time_granularity = 'unknown'"
        ).fetchone()["n"]
        return {"total": total, "by_status": by_status, "no_time": no_time}

    def close(self) -> None:
        self.conn.close()


def _row_to_event(row: sqlite3.Row) -> Event:
    d = dict(row)
    d["participants"] = json.loads(d.get("participants") or "[]")
    d["all_day"] = bool(d.get("all_day"))
    d["is_deadline"] = bool(d.get("is_deadline"))
    d["needs_clarification"] = bool(d.get("needs_clarification"))
    # verified 存的是 JSON 文本；坏数据当作「没核实过」处理，不要让整行读不出来
    raw_v = d.get("verified")
    if raw_v:
        try:
            parsed = json.loads(raw_v)
            d["verified"] = parsed if isinstance(parsed, dict) else None
        except (TypeError, ValueError):
            d["verified"] = None
    else:
        d["verified"] = None
    return Event.from_dict(d)
