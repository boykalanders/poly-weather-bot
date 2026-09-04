"""SQLite persistence: trades, positions, daily risk counters, and bot state."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    mode          TEXT NOT NULL,
    strategy      TEXT NOT NULL,
    event_slug    TEXT,
    condition_id  TEXT,
    token_id      TEXT,
    market        TEXT,
    side          TEXT,
    price         REAL,
    size          REAL,
    notional      REAL,
    model_prob    REAL,
    edge          REAL,
    order_id      TEXT,
    status        TEXT,
    note          TEXT
);
CREATE INDEX IF NOT EXISTS idx_trades_ts ON trades(ts);

CREATE TABLE IF NOT EXISTS positions (
    token_id      TEXT PRIMARY KEY,
    condition_id  TEXT,
    event_slug    TEXT,
    market        TEXT,
    shares        REAL NOT NULL DEFAULT 0,
    cost          REAL NOT NULL DEFAULT 0,
    opened_ts     INTEGER,
    updated_ts    INTEGER,
    resolved      INTEGER NOT NULL DEFAULT 0,
    payout        REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily (
    day           TEXT PRIMARY KEY,
    notional      REAL NOT NULL DEFAULT 0,
    realized_pnl  REAL NOT NULL DEFAULT 0,
    n_trades      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS state (
    k             TEXT PRIMARY KEY,
    v             TEXT
);

CREATE TABLE IF NOT EXISTS seen_leader_trades (
    key           TEXT PRIMARY KEY,
    ts            INTEGER
);
"""


def utc_day(ts: int | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), tz=timezone.utc).strftime("%Y-%m-%d")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._c = sqlite3.connect(str(path), check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)
        self._c.commit()

    # ------------------------------------------------------------------ state
    def get_state(self, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._c.execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except (TypeError, ValueError):
            return row["v"]

    def set_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._c.execute(
                "INSERT INTO state(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value)),
            )
            self._c.commit()

    # ----------------------------------------------------------------- trades
    def record_trade(self, **kw: Any) -> int:
        kw.setdefault("ts", int(time.time()))
        cols = ",".join(kw)
        qs = ",".join("?" * len(kw))
        with self._lock:
            cur = self._c.execute(f"INSERT INTO trades({cols}) VALUES({qs})", tuple(kw.values()))
            self._c.execute(
                "INSERT INTO daily(day,notional,n_trades) VALUES(?,?,1) "
                "ON CONFLICT(day) DO UPDATE SET notional=notional+excluded.notional, "
                "n_trades=n_trades+1",
                (utc_day(kw["ts"]), float(kw.get("notional") or 0)),
            )
            self._c.commit()
            return cur.lastrowid

    def recent_trades(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self._c.execute(
                "SELECT * FROM trades ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()

    # -------------------------------------------------------------- positions
    def upsert_position(
        self, token_id: str, shares_delta: float, cost_delta: float, **meta: Any
    ) -> None:
        now = int(time.time())
        with self._lock:
            self._c.execute(
                "INSERT INTO positions"
                "(token_id,condition_id,event_slug,market,shares,cost,opened_ts,updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(token_id) DO UPDATE SET "
                "  shares=shares+excluded.shares, "
                "  cost=cost+excluded.cost, "
                "  updated_ts=excluded.updated_ts",
                (token_id, meta.get("condition_id"), meta.get("event_slug"),
                 meta.get("market"), shares_delta, cost_delta, now, now),
            )
            self._c.commit()

    def open_positions(self) -> list[sqlite3.Row]:
        with self._lock:
            return self._c.execute(
                "SELECT * FROM positions WHERE resolved=0 AND shares > 0.01 "
                "ORDER BY updated_ts DESC"
            ).fetchall()

    def position_cost(self, condition_id: str) -> float:
        with self._lock:
            row = self._c.execute(
                "SELECT COALESCE(SUM(cost),0) AS c FROM positions "
                "WHERE condition_id=? AND resolved=0",
                (condition_id,),
            ).fetchone()
        return float(row["c"] or 0)

    def resolve_position(self, token_id: str, payout: float) -> float:
        """Mark a position settled and book the realized PnL. Returns the PnL."""
        with self._lock:
            row = self._c.execute(
                "SELECT cost FROM positions WHERE token_id=? AND resolved=0", (token_id,)
            ).fetchone()
            if not row:
                return 0.0
            pnl = payout - float(row["cost"])
            self._c.execute(
                "UPDATE positions SET resolved=1, payout=?, updated_ts=? WHERE token_id=?",
                (payout, int(time.time()), token_id),
            )
            self._c.execute(
                "INSERT INTO daily(day,realized_pnl) VALUES(?,?) "
                "ON CONFLICT(day) DO UPDATE SET realized_pnl=realized_pnl+excluded.realized_pnl",
                (utc_day(), pnl),
            )
            self._c.commit()
            return pnl

    # ------------------------------------------------------------------ daily
    def today(self) -> dict:
        day = utc_day()
        with self._lock:
            row = self._c.execute("SELECT * FROM daily WHERE day=?", (day,)).fetchone()
        if not row:
            return {"day": day, "notional": 0.0, "realized_pnl": 0.0, "n_trades": 0}
        return dict(row)

    def totals(self) -> dict:
        with self._lock:
            r = self._c.execute(
                "SELECT COALESCE(SUM(realized_pnl),0) p, COALESCE(SUM(n_trades),0) n FROM daily"
            ).fetchone()
        return {"realized_pnl": float(r["p"]), "n_trades": int(r["n"])}

    # -------------------------------------------------------- copy-trade dedup
    def seen_leader_trade(self, key: str) -> bool:
        """True if this leader trade was already mirrored; records it otherwise."""
        with self._lock:
            row = self._c.execute(
                "SELECT 1 FROM seen_leader_trades WHERE key=?", (key,)
            ).fetchone()
            if row:
                return True
            now = int(time.time())
            self._c.execute(
                "INSERT OR IGNORE INTO seen_leader_trades(key,ts) VALUES(?,?)", (key, now)
            )
            self._c.execute("DELETE FROM seen_leader_trades WHERE ts < ?", (now - 7 * 86400,))
            self._c.commit()
            return False

    def close(self) -> None:
        with self._lock:
            self._c.close()
