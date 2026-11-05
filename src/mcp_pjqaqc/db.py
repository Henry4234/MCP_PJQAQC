"""Read-only MariaDB access. Every statement runs in a READ ONLY transaction that is rolled back."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Sequence

import pymysql
import pymysql.cursors

from .config import load_config

STATEMENT_TIMEOUT_SECONDS = 15
CONNECT_TIMEOUT_SECONDS = 5

# MariaDB returns JSON as text and BOOLEAN as TINYINT(1).
JSON_COLUMNS = {"westgard_flags"}
BOOL_COLUMNS = {"active", "product_active", "required", "parallel_test_done", "same_lot_as_previous"}


class DatabaseError(RuntimeError):
    pass


def connect() -> pymysql.connections.Connection:
    cfg = load_config()
    try:
        return pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset="utf8mb4",
            connect_timeout=CONNECT_TIMEOUT_SECONDS,
            read_timeout=STATEMENT_TIMEOUT_SECONDS + 5,
            autocommit=False,
            cursorclass=pymysql.cursors.DictCursor,
        )
    except pymysql.Error as exc:
        raise DatabaseError(f"無法連線到資料庫 {cfg.host}:{cfg.port}：{exc}") from exc


def _convert(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in JSON_COLUMNS and isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    if column in BOOL_COLUMNS and isinstance(value, int):
        return bool(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return value


def fetch_all(
    sql: str,
    params: Sequence[Any] | None = None,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    """Run one query. Leave params as None when the SQL holds literal % characters."""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET SESSION max_statement_time = {STATEMENT_TIMEOUT_SECONDS}")
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("START TRANSACTION READ ONLY")
            cur.execute(sql, params)
            rows = cur.fetchall() if max_rows is None else cur.fetchmany(max_rows)
        return [{k: _convert(k, v) for k, v in row.items()} for row in rows]
    except pymysql.Error as exc:
        raise DatabaseError(f"資料庫查詢失敗：{exc}") from exc
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
