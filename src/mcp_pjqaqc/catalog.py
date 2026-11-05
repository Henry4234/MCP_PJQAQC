"""Schema knowledge: the structure snapshot merged with domain meaning, plus the FK join graph."""
from __future__ import annotations

import difflib
from collections import defaultdict
from functools import lru_cache
from typing import Any, NamedTuple

from . import knowledge
from .snapshot import load_snapshot

MAX_PATHS = 5


class Edge(NamedTuple):
    left: str
    right: str
    on: str
    fk: str


@lru_cache(maxsize=1)
def _schema() -> dict[str, dict[str, Any]]:
    return load_snapshot()["tables"]


def table_names() -> list[str]:
    return sorted(_schema())


def resolve_table(name: str) -> str:
    key = name.strip().lower()
    key = knowledge.TABLE_ALIASES.get(key, key)
    if key in _schema():
        return key
    suggestions = difflib.get_close_matches(key, table_names(), n=3, cutoff=0.4)
    hint = f"，您是否要找：{', '.join(suggestions)}" if suggestions else ""
    raise ValueError(f"找不到資料表 {name}{hint}")


def overview() -> dict[str, Any]:
    schema = _schema()
    layers = []
    for code, layer in knowledge.LAYERS.items():
        tables = [
            {"table": name, "zh": meta["zh"], "purpose": meta["purpose"]}
            for name, meta in knowledge.TABLES.items()
            if meta["layer"] == code and name in schema
        ]
        layers.append({"layer": code, **layer, "tables": tables})
    return {
        "database": "PJQAQC（MariaDB 10.11）",
        "core_principle": knowledge.CORE_PRINCIPLE,
        "layers": layers,
        "relationship_flow": [
            "lab_sections → instruments → instrument_assays ← test_items, specimen_types",
            "instrument_assays → assay_reagent_requirements → reagent_products → reagent_lots → instrument_reagent_loads",
            "instrument_assays → assay_qc_requirements → qc_products → qc_lots → qc_target_values",
            "qc_runs → qc_results → qc_result_reagents → reagent_lots",
            "instrument_assays → assay_calibrator_requirements → calibrator_products → calibrator_lots",
            "calibration_events → calibration_event_assays → calibration_event_reagents → reagent_lots",
        ],
        "rules": knowledge.RULES,
        "westgard_rules": knowledge.WESTGARD,
    }


def _referenced_by(table: str) -> list[dict[str, Any]]:
    return [
        {"table": other, "columns": fk["columns"], "ref_columns": fk["ref_columns"]}
        for other, meta in sorted(_schema().items())
        for fk in meta["foreign_keys"]
        if fk["ref_table"] == table
    ]


def describe(table: str) -> dict[str, Any]:
    name = resolve_table(table)
    meta = _schema()[name]
    # A table added by a snapshot refresh may not have curated notes yet.
    info = knowledge.TABLES.get(name, {})
    descriptions = info.get("columns", {})
    columns = [{**col, "description": descriptions.get(col["name"], "")} for col in meta["columns"]]
    return {
        "table": name,
        "type": meta["type"],
        "zh": info.get("zh", ""),
        "layer": knowledge.LAYERS[info["layer"]]["name"] if info else "未分類",
        "purpose": info.get("purpose", ""),
        "columns": columns,
        "primary_key": meta["primary_key"],
        "unique_keys": meta["unique_keys"],
        "foreign_keys": meta["foreign_keys"],
        "referenced_by": _referenced_by(name),
    }


@lru_cache(maxsize=1)
def _graph() -> dict[str, list[Edge]]:
    graph: dict[str, list[Edge]] = defaultdict(list)
    for table, meta in _schema().items():
        for fk in meta["foreign_keys"]:
            ref = fk["ref_table"]
            if ref == table:
                continue
            on = " AND ".join(
                f"{table}.{col} = {ref}.{ref_col}" for col, ref_col in zip(fk["columns"], fk["ref_columns"])
            )
            graph[table].append(Edge(table, ref, on, fk["name"]))
            graph[ref].append(Edge(ref, table, on, fk["name"]))
    return graph


def _shortest_paths(start: str, goal: str) -> list[list[Edge]]:
    graph = _graph()
    frontier: list[tuple[str, list[Edge], set[str]]] = [(start, [], {start})]
    while frontier:
        found: list[list[Edge]] = []
        next_frontier: list[tuple[str, list[Edge], set[str]]] = []
        for table, path, seen in frontier:
            for edge in graph.get(table, []):
                if edge.right in seen:
                    continue
                new_path = [*path, edge]
                if edge.right == goal:
                    found.append(new_path)
                else:
                    next_frontier.append((edge.right, new_path, seen | {edge.right}))
        if found:
            return found
        frontier = next_frontier
    return []


def path_sql(start: str, path: list[Edge]) -> str:
    joins = [f"FROM {start}"]
    joins += [f"JOIN {edge.right} ON {edge.on}" for edge in path]
    return " ".join(joins)


def join_paths(from_table: str, to_table: str) -> dict[str, Any]:
    start, goal = resolve_table(from_table), resolve_table(to_table)
    if _schema()[start]["type"] == "VIEW" or _schema()[goal]["type"] == "VIEW":
        raise ValueError("View 沒有外鍵，請改以基底資料表查路徑，或直接查詢 View。")
    if start == goal:
        return {"from": start, "to": goal, "paths": [], "note": "兩者是同一張表，不需要 JOIN。"}

    paths = _shortest_paths(start, goal)[:MAX_PATHS]
    result: dict[str, Any] = {
        "from": start,
        "to": goal,
        "paths": [
            {
                "tables": [start, *(edge.right for edge in path)],
                "sql": path_sql(start, path),
                "foreign_keys": [edge.fk for edge in path],
            }
            for path in paths
        ],
    }
    views = [v for v, covered in knowledge.VIEW_COVERAGE.items() if {start, goal} <= covered]
    if views:
        result["tip"] = f"這兩張表已被 {views[0]} 攤平，可直接查 View。"
    if len(paths) > 1:
        result["note"] = "有多條同長度路徑，語意可能不同：經 *_requirements 是「設定允許」，經 *_lots 是「實際使用」。"
    return result
