"""Read the schema from information_schema. Run `python -m mcp_pjqaqc.snapshot` to refresh the JSON."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import db

SNAPSHOT_PATH = Path(__file__).with_name("schema_snapshot.json")
_IN_LIST = re.compile(r"^`(\w+)` in \((.*)\)$", re.IGNORECASE)


def _key_columns() -> dict[tuple[str, str], list[dict[str, Any]]]:
    rows = db.fetch_all(
        """
        SELECT k.TABLE_NAME AS table_name, k.CONSTRAINT_NAME AS name, c.CONSTRAINT_TYPE AS kind,
               k.COLUMN_NAME AS column_name, k.REFERENCED_TABLE_NAME AS ref_table,
               k.REFERENCED_COLUMN_NAME AS ref_column
        FROM information_schema.KEY_COLUMN_USAGE k
        JOIN information_schema.TABLE_CONSTRAINTS c
          ON c.CONSTRAINT_SCHEMA = k.CONSTRAINT_SCHEMA
         AND c.TABLE_NAME = k.TABLE_NAME
         AND c.CONSTRAINT_NAME = k.CONSTRAINT_NAME
        WHERE k.CONSTRAINT_SCHEMA = DATABASE()
        ORDER BY k.TABLE_NAME, k.CONSTRAINT_NAME, k.ORDINAL_POSITION
        """
    )
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault((row["table_name"], row["name"]), []).append(row)
    return grouped


def _allowed_values() -> dict[tuple[str, str], list[str]]:
    rows = db.fetch_all(
        """
        SELECT TABLE_NAME AS table_name, CHECK_CLAUSE AS clause
        FROM information_schema.CHECK_CONSTRAINTS
        WHERE CONSTRAINT_SCHEMA = DATABASE()
        """
    )
    allowed: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        match = _IN_LIST.match(row["clause"].strip())
        if match:
            values = re.findall(r"'([^']*)'", match.group(2))
            allowed[(row["table_name"], match.group(1))] = values
    return allowed


def read_live_schema() -> dict[str, Any]:
    tables: dict[str, Any] = {
        row["name"]: {
            "type": "VIEW" if row["kind"] == "VIEW" else "BASE TABLE",
            "columns": [],
            "primary_key": [],
            "unique_keys": {},
            "foreign_keys": [],
        }
        for row in db.fetch_all(
            """
            SELECT TABLE_NAME AS name, TABLE_TYPE AS kind FROM information_schema.TABLES
            WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME
            """
        )
    }

    allowed = _allowed_values()
    for row in db.fetch_all(
        """
        SELECT TABLE_NAME AS table_name, COLUMN_NAME AS name, COLUMN_TYPE AS type,
               IS_NULLABLE AS nullable, COLUMN_DEFAULT AS default_value,
               GENERATION_EXPRESSION AS generated
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE()
        ORDER BY TABLE_NAME, ORDINAL_POSITION
        """
    ):
        column: dict[str, Any] = {
            "name": row["name"],
            "type": row["type"],
            "nullable": row["nullable"] == "YES",
        }
        if row["default_value"] not in (None, "NULL"):
            column["default"] = row["default_value"]
        if row["generated"]:
            column["generated"] = row["generated"]
        values = allowed.get((row["table_name"], row["name"]))
        if values:
            column["allowed_values"] = values
        tables[row["table_name"]]["columns"].append(column)

    for (table_name, name), parts in _key_columns().items():
        table = tables[table_name]
        columns = [part["column_name"] for part in parts]
        kind = parts[0]["kind"]
        if kind == "PRIMARY KEY":
            table["primary_key"] = columns
        elif kind == "UNIQUE":
            table["unique_keys"][name] = columns
        elif kind == "FOREIGN KEY":
            table["foreign_keys"].append(
                {
                    "name": name,
                    "columns": columns,
                    "ref_table": parts[0]["ref_table"],
                    "ref_columns": [part["ref_column"] for part in parts],
                }
            )
    return {"tables": tables}


def load_snapshot() -> dict[str, Any]:
    return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))


def diff_schema(expected: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Compare what this server believes with the live database."""
    want, have = expected["tables"], live["tables"]
    differences: list[str] = []
    for name in sorted(want.keys() & have.keys()):
        want_cols = {c["name"]: (c["type"], c["nullable"]) for c in want[name]["columns"]}
        have_cols = {c["name"]: (c["type"], c["nullable"]) for c in have[name]["columns"]}
        for col in sorted(want_cols.keys() - have_cols.keys()):
            differences.append(f"{name}.{col}：線上已不存在")
        for col in sorted(have_cols.keys() - want_cols.keys()):
            differences.append(f"{name}.{col}：線上新增，快照未收錄")
        for col in sorted(want_cols.keys() & have_cols.keys()):
            if want_cols[col] != have_cols[col]:
                differences.append(f"{name}.{col}：快照 {want_cols[col]}，線上 {have_cols[col]}")
        fk_key = lambda fk: (tuple(fk["columns"]), fk["ref_table"], tuple(fk["ref_columns"]))
        if {fk_key(f) for f in want[name]["foreign_keys"]} != {fk_key(f) for f in have[name]["foreign_keys"]}:
            differences.append(f"{name}：外鍵與快照不同")
    missing = sorted(want.keys() - have.keys())
    extra = sorted(have.keys() - want.keys())
    return {
        "in_sync": not (missing or extra or differences),
        "missing_tables": missing,
        "extra_tables": extra,
        "differences": differences,
    }


def main() -> None:
    schema = read_live_schema()
    SNAPSHOT_PATH.write_text(json.dumps(schema, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"已更新 {SNAPSHOT_PATH.name}：{len(schema['tables'])} 個物件")


if __name__ == "__main__":
    main()
