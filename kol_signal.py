"""Durable signal queue shared by the market monitors and Square publisher."""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping, Optional


logger = logging.getLogger(__name__)
DEFAULT_DB_PATH = "kol_signals.db"
VALID_DIRECTIONS = {"LONG", "SHORT", "WATCH"}


def normalize_symbol(symbol: str) -> str:
    value = re.sub(r"[^A-Z0-9]", "", str(symbol).upper().strip())
    if value.endswith("USDT") and len(value) > 4:
        value = value[:-4]
    if not value:
        raise ValueError("signal symbol must not be empty")
    return value


def _database_path(path: Optional[str | Path] = None) -> Path:
    return Path(path or os.getenv("KOL_SIGNAL_DB", DEFAULT_DB_PATH))


def connect(path: Optional[str | Path] = None) -> sqlite3.Connection:
    db_path = _database_path(path)
    if db_path.parent != Path("."):
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS signals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at REAL NOT NULL,
            symbol TEXT NOT NULL,
            source TEXT NOT NULL,
            indicator TEXT NOT NULL,
            direction TEXT NOT NULL,
            price REAL,
            summary TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            fingerprint TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at REAL NOT NULL DEFAULT 0,
            claimed_at REAL,
            generated_text TEXT,
            published_at REAL,
            post_id TEXT,
            post_url TEXT,
            error TEXT
        )
        """
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_signals_pending "
        "ON signals(status, next_attempt_at, created_at)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_signals_cooldown "
        "ON signals(symbol, indicator, published_at)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_fingerprint "
        "ON signals(source, indicator, symbol, fingerprint) WHERE fingerprint IS NOT NULL"
    )
    connection.commit()
    return connection


def emit_signal(
    *,
    symbol: str,
    source: str,
    indicator: str,
    direction: str,
    summary: str,
    price: Optional[float] = None,
    details: Optional[Mapping[str, Any]] = None,
    fingerprint: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> Optional[int]:
    """Append one structured signal without allowing queue failures to stop a monitor."""

    try:
        clean_symbol = normalize_symbol(symbol)
        clean_direction = str(direction).upper().strip()
        if clean_direction not in VALID_DIRECTIONS:
            raise ValueError(f"unsupported signal direction: {direction}")
        if not source.strip() or not indicator.strip() or not summary.strip():
            raise ValueError("source, indicator, and summary are required")
        serialized_details = json.dumps(details or {}, ensure_ascii=False, separators=(",", ":"))
        with connect(db_path) as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO signals (
                    created_at, symbol, source, indicator, direction, price,
                    summary, details_json, fingerprint
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    time.time(),
                    clean_symbol,
                    source.strip(),
                    indicator.strip(),
                    clean_direction,
                    float(price) if price is not None else None,
                    summary.strip(),
                    serialized_details,
                    fingerprint.strip() if fingerprint else None,
                ),
            )
            if cursor.rowcount == 0:
                return None
            return int(cursor.lastrowid)
    except Exception:
        logger.exception("Unable to enqueue KOL signal for %s/%s", source, symbol)
        return None
