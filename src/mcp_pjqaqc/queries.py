"""Domain queries over the PJQAQC schema. Every value is parameterized; only trusted names are interpolated."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from . import catalog, db, knowledge, snapshot
from .sql_guard import validate_select

# Taiwan has no DST, and a fixed offset avoids needing tzdata on Windows.
LAB_TZ = timezone(timedelta(hours=8))
DEFAULT_LIMIT = 100
MAX_LIMIT = 1000
LOT_DEPLETED = ("EXHAUSTED", "EXPIRED", "RECALLED")

LOT_KINDS: dict[str, dict[str, Any]] = {
    "reagent": {
        "lots": "reagent_lots", "lot_id": "reagent_lot_id",
        "products": "reagent_products", "product_id": "reagent_product_id",
        "requirements": "assay_reagent_requirements",
        "level": False, "parallel": True,
    },
    "qc": {
        "lots": "qc_lots", "lot_id": "qc_lot_id",
        "products": "qc_products", "product_id": "qc_product_id",
        "requirements": "assay_qc_requirements",
        "level": True, "parallel": True,
    },
    "calibrator": {
        "lots": "calibrator_lots", "lot_id": "calibrator_lot_id",
        "products": "calibrator_products", "product_id": "calibrator_product_id",
        "requirements": "assay_calibrator_requirements",
        "level": True, "parallel": False,
    },
}


def lab_today() -> date:
    return datetime.now(LAB_TZ).date()


class Where:
    def __init__(self) -> None:
        self.clauses: list[str] = []
        self.params: list[Any] = []

    def add(self, clause: str, *params: Any) -> None:
        self.clauses.append(clause)
        self.params.extend(params)

    @property
    def sql(self) -> str:
        return f" WHERE {' AND '.join(self.clauses)}" if self.clauses else ""


def _like(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _placeholders(values: Iterable[Any]) -> str:
    return ", ".join(["%s"] * len(list(values)))


def _clamp(limit: int) -> int:
    return max(1, min(int(limit), MAX_LIMIT))


def _filter_instrument(w: Where, value: str | None, column: str) -> None:
    if value:
        like = _like(value)
        w.add(
            f"{column} IN (SELECT instrument_id FROM instruments "
            "WHERE serial_number = %s OR instrument_name LIKE %s OR model_name LIKE %s)",
            value, like, like,
        )


def _filter_section(w: Where, value: str | None, column: str) -> None:
    if value:
        w.add(
            f"{column} IN (SELECT i.instrument_id FROM instruments i "
            "JOIN lab_sections s ON s.section_id = i.section_id "
            "WHERE s.section_code = %s OR s.section_name LIKE %s)",
            knowledge.SECTION_ALIASES.get(value, value), _like(value),
        )


def _filter_item(w: Where, value: str | None, code: str = "item_code", name: str = "item_name") -> None:
    if value:
        item = knowledge.normalize_item(value)
        w.add(f"({code} = %s OR {name} = %s)", item, item)


def _filter_specimen(w: Where, value: str | None, code: str = "specimen_code", name: str = "specimen_name") -> None:
    if value:
        specimen = knowledge.normalize_specimen(value)
        w.add(f"({code} = %s OR {name} = %s)", specimen, specimen)


def _filter_product(w: Where, value: str | None, code: str, name: str) -> None:
    if value:
        w.add(f"({code} = %s OR {name} LIKE %s)", value, _like(value))


def _filter_level(w: Where, value: str | None, column: str) -> None:
    if value:
        digits = re.sub(r"\D", "", value)
        candidates = {value, digits, f"Level {digits}"} if digits else {value}
        w.add(f"{column} IN ({_placeholders(candidates)})", *sorted(candidates))


def _parse_moment(value: str, label: str) -> tuple[datetime, bool]:
    text = value.strip()
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{label} 格式不正確：{value}（請用 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS）") from exc
    return moment.replace(tzinfo=None), len(text) == 10


def _filter_time(w: Where, column: str, date_from: str | None, date_to: str | None) -> None:
    start = _parse_moment(date_from, "date_from")[0] if date_from else None
    end = None
    if date_to:
        end, date_only = _parse_moment(date_to, "date_to")
        # A bare date covers that whole day.
        if date_only:
            end += timedelta(days=1)
            w.add(f"{column} < %s", end)
        else:
            w.add(f"{column} <= %s", end)
    if start:
        if end and start > end:
            raise ValueError("date_from 不可晚於 date_to")
        w.add(f"{column} >= %s", start)


def _rows_result(rows: list[dict[str, Any]], limit: int, source: str, **extra: Any) -> dict[str, Any]:
    truncated = len(rows) > limit
    rows = rows[:limit]
    result: dict[str, Any] = {"row_count": len(rows), "truncated": truncated, "rows": rows, **extra}
    if truncated:
        result["note"] = f"結果超過 {limit} 筆已截斷，請縮小條件或提高 limit。"
    elif not rows:
        result["note"] = _empty_note(source)
    return result


def _empty_note(source: str) -> str:
    if db.fetch_all(f"SELECT 1 AS present FROM {source} LIMIT 1"):
        return "資料表有資料，但沒有符合條件者。名稱不確定時可先用 search_entities 查正確代碼。"
    return f"資料表 {source} 目前完全沒有資料：這代表尚未登錄，不能解讀為結果正常或沒有異常。"


def search_entities(keyword: str, entity_types: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    text = keyword.strip()
    if not text:
        raise ValueError("keyword 不可為空白")
    like = _like(text)
    limit = _clamp(limit)
    sources: dict[str, tuple[str, list[str]]] = {
        "section": ("SELECT section_code, section_name FROM lab_sections", ["section_code", "section_name"]),
        "instrument": (
            "SELECT serial_number, instrument_name, model_name, manufacturer, instrument_role, active FROM instruments",
            ["serial_number", "instrument_name", "model_name", "manufacturer"],
        ),
        "specimen": ("SELECT specimen_code, specimen_name FROM specimen_types", ["specimen_code", "specimen_name"]),
        "item": (
            "SELECT item_code, item_name, default_unit, loinc_code, active FROM test_items",
            ["item_code", "item_name", "loinc_code"],
        ),
    }
    for kind, spec in LOT_KINDS.items():
        sources[f"{kind}_product"] = (
            f"SELECT product_code, product_name, manufacturer, catalog_number, active FROM {spec['products']}",
            ["product_code", "product_name", "manufacturer", "catalog_number"],
        )
        level = "l.level_code, " if spec["level"] else ""
        sources[f"{kind}_lot"] = (
            f"SELECT p.product_code, p.product_name, l.lot_number, {level}l.lot_status, l.expiry_date "
            f"FROM {spec['lots']} l JOIN {spec['products']} p ON p.{spec['product_id']} = l.{spec['product_id']}",
            ["l.lot_number"],
        )

    if entity_types:
        unknown = sorted(set(entity_types) - sources.keys())
        if unknown:
            raise ValueError(f"未知的 entity_types：{unknown}；可用：{sorted(sources)}")
    wanted = entity_types or list(sources)

    # Aliases such as 血糖 → GLU search under the code as well as the literal text.
    extra_terms = {knowledge.ITEM_ALIASES.get(text), knowledge.SPECIMEN_ALIASES.get(text.lower())} - {None}
    matches: dict[str, list[dict[str, Any]]] = {}
    for name in wanted:
        sql, columns = sources[name]
        conditions = [f"{col} LIKE %s" for col in columns]
        params: list[Any] = [like] * len(columns)
        for term in extra_terms:
            conditions.extend(f"{col} = %s" for col in columns)
            params.extend([term] * len(columns))
        rows = db.fetch_all(f"{sql} WHERE {' OR '.join(conditions)} LIMIT %s", [*params, limit])
        if rows:
            matches[name] = rows
    result: dict[str, Any] = {"keyword": text, "matches": matches}
    if not matches:
        result["note"] = "沒有找到符合的主檔或批號，請換關鍵字（代碼、英文名或中文名）。"
    return result


def _materials(assay_ids: list[str]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {
        assay_id: {"reagents": [], "qc_products": [], "calibrators": []} for assay_id in assay_ids
    }
    if not assay_ids:
        return grouped
    marks = _placeholders(assay_ids)
    specs = (
        ("reagents", "reagent", "r.reagent_role, "),
        ("qc_products", "qc", ""),
        ("calibrators", "calibrator", ""),
    )
    for key, kind, extra in specs:
        spec = LOT_KINDS[kind]
        rows = db.fetch_all(
            f"SELECT r.assay_id, {extra}r.required, r.priority, r.note, p.product_code, p.product_name, "
            f"p.manufacturer, p.catalog_number, p.active AS product_active "
            f"FROM {spec['requirements']} r "
            f"JOIN {spec['products']} p ON p.{spec['product_id']} = r.{spec['product_id']} "
            f"WHERE r.assay_id IN ({marks}) ORDER BY r.priority, p.product_code",
            assay_ids,
        )
        for row in rows:
            grouped[row.pop("assay_id")][key].append(row)
    return grouped


def _lot_rows(
    kind: str,
    *,
    product: str | None = None,
    product_codes: list[str] | None = None,
    lot_number: str | None = None,
    status: str | None = None,
    level: str | None = None,
    expiring_within_days: int | None = None,
    parallel_test_done: bool | None = None,
    usable_only: bool = False,
    limit: int = MAX_LIMIT,
) -> list[dict[str, Any]]:
    spec = LOT_KINDS[kind]
    today = lab_today()
    w = Where()
    _filter_product(w, product, "p.product_code", "p.product_name")
    if product_codes is not None:
        w.add(f"p.product_code IN ({_placeholders(product_codes)})", *product_codes)
    if lot_number:
        w.add("l.lot_number = %s", lot_number)
    if status:
        w.add("l.lot_status = %s", status)
    if level and spec["level"]:
        _filter_level(w, level, "l.level_code")
    if expiring_within_days is not None:
        w.add("l.expiry_date IS NOT NULL AND l.expiry_date <= %s", today + timedelta(days=expiring_within_days))
        w.add(f"l.lot_status NOT IN ({_placeholders(LOT_DEPLETED)})", *LOT_DEPLETED)
    if parallel_test_done is not None and spec["parallel"]:
        w.add("l.parallel_test_done = %s", parallel_test_done)
    if usable_only:
        w.add("l.lot_status IN ('AVAILABLE', 'IN_USE')")

    level_col = "l.level_code" if spec["level"] else "NULL"
    parallel = "l.parallel_test_done, l.parallel_test_date" if spec["parallel"] else "NULL AS parallel_test_done, NULL AS parallel_test_date"
    rows = db.fetch_all(
        f"SELECT l.{spec['lot_id']} AS lot_id, p.product_code, p.product_name, p.manufacturer, l.lot_number, "
        f"{level_col} AS level_code, l.lot_status, l.received_at, l.opened_at, l.expiry_date, {parallel}, l.note "
        f"FROM {spec['lots']} l JOIN {spec['products']} p ON p.{spec['product_id']} = l.{spec['product_id']}"
        f"{w.sql} ORDER BY l.expiry_date IS NULL, l.expiry_date, p.product_code, l.lot_number LIMIT %s",
        [*w.params, limit],
    )
    for row in rows:
        row["kind"] = kind
        if row["expiry_date"]:
            days = (date.fromisoformat(row["expiry_date"]) - today).days
            row["days_to_expiry"] = days
            row["expired_by_date"] = days < 0
        else:
            row["days_to_expiry"] = None
            row["expired_by_date"] = None
    return rows


def find_lots(
    kind: str = "all",
    product: str | None = None,
    lot_number: str | None = None,
    status: str | None = None,
    level: str | None = None,
    expiring_within_days: int | None = None,
    parallel_test_done: bool | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = _clamp(limit)
    if expiring_within_days is not None and expiring_within_days < 0:
        raise ValueError("expiring_within_days 不可為負數")
    kinds = list(LOT_KINDS) if kind == "all" else [kind]
    rows: list[dict[str, Any]] = []
    for each in kinds:
        rows.extend(
            _lot_rows(
                each,
                product=product,
                lot_number=lot_number,
                status=status,
                level=level,
                expiring_within_days=expiring_within_days,
                parallel_test_done=parallel_test_done,
                limit=limit + 1,
            )
        )
    rows.sort(key=lambda r: (r["expiry_date"] is None, r["expiry_date"] or "", r["product_code"], r["lot_number"]))
    source = LOT_KINDS[kind]["lots"] if kind != "all" else "reagent_lots"
    result = _rows_result(rows, limit, source, today=lab_today().isoformat())
    if any(r["expired_by_date"] and r["lot_status"] in ("AVAILABLE", "IN_USE") for r in result["rows"]):
        result["warning"] = "部分批號已超過效期，但 lot_status 仍為可用狀態（expired_by_date = true），請確認。"
    return result


def find_assays(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    section: str | None = None,
    active_only: bool = True,
    include_materials: bool = False,
    include_lots: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = _clamp(limit)
    w = Where()
    _filter_item(w, item)
    _filter_specimen(w, specimen)
    _filter_instrument(w, instrument, "instrument_id")
    _filter_section(w, section, "instrument_id")
    if active_only:
        w.add("active = %s", True)
    rows = db.fetch_all(
        "SELECT assay_id, section_code, section_name, serial_number, instrument_name, instrument_role, "
        "item_code, item_name, specimen_code, specimen_name, method_name, result_unit, decimal_places, active "
        f"FROM v_assay_catalog{w.sql} "
        "ORDER BY section_code, instrument_name, item_code, specimen_code LIMIT %s",
        [*w.params, limit + 1],
    )
    result = _rows_result(rows, limit, "instrument_assays")
    if not (include_materials or include_lots):
        return result

    assays = result["rows"]
    materials = _materials([row["assay_id"] for row in assays])
    for row in assays:
        row.update(materials[row["assay_id"]])
    if include_lots:
        keys = {"reagent": "reagents", "qc": "qc_products", "calibrator": "calibrators"}
        lots: dict[str, list[dict[str, Any]]] = {}
        for kind, key in keys.items():
            codes = sorted({m["product_code"] for row in assays for m in row[key]})
            if codes:
                lots[kind] = _lot_rows(kind, product_codes=codes, usable_only=True)
        result["usable_lots"] = lots
        result["today"] = lab_today().isoformat()
    return result


def get_product_usage(
    product: str, kind: str = "all", active_only: bool = True, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    limit = _clamp(limit)
    kinds = list(LOT_KINDS) if kind == "all" else [kind]
    usage: dict[str, list[dict[str, Any]]] = {}
    truncated = False
    for each in kinds:
        spec = LOT_KINDS[each]
        w = Where()
        _filter_product(w, product, "p.product_code", "p.product_name")
        if active_only:
            w.add("c.active = %s", True)
        role = "r.reagent_role, " if each == "reagent" else ""
        rows = db.fetch_all(
            f"SELECT p.product_code, p.product_name, {role}r.required, r.priority, c.assay_id, c.serial_number, "
            "c.instrument_name, c.instrument_role, c.item_code, c.item_name, c.specimen_code, c.result_unit "
            f"FROM {spec['requirements']} r "
            f"JOIN {spec['products']} p ON p.{spec['product_id']} = r.{spec['product_id']} "
            f"JOIN v_assay_catalog c ON c.assay_id = r.assay_id{w.sql} "
            "ORDER BY p.product_code, c.instrument_name, c.item_code, c.specimen_code LIMIT %s",
            [*w.params, limit + 1],
        )
        truncated = truncated or len(rows) > limit
        if rows:
            usage[each] = rows[:limit]
    result: dict[str, Any] = {"product": product, "usage": usage, "truncated": truncated}
    if not usage:
        result["note"] = "沒有找到使用此產品的 assay。名稱不確定時可先用 search_entities。"
    return result


def _qc_where(
    item: str | None,
    specimen: str | None,
    instrument: str | None,
    section: str | None,
    qc_product: str | None,
    qc_lot: str | None,
    level: str | None,
    status: str | None,
    rule: str | None,
    date_from: str | None,
    date_to: str | None,
) -> Where:
    w = Where()
    _filter_item(w, item)
    _filter_specimen(w, specimen)
    _filter_instrument(w, instrument, "instrument_id")
    _filter_section(w, section, "instrument_id")
    _filter_product(w, qc_product, "qc_product_code", "qc_product_name")
    if qc_lot:
        w.add("qc_lot_number = %s", qc_lot)
    _filter_level(w, level, "qc_level")
    if status:
        w.add("result_status = %s", status)
    if rule:
        w.add("JSON_CONTAINS(westgard_flags, JSON_QUOTE(%s))", rule)
    _filter_time(w, "run_at", date_from, date_to)
    return w


def get_qc_results(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    section: str | None = None,
    qc_product: str | None = None,
    qc_lot: str | None = None,
    level: str | None = None,
    status: str | None = None,
    rule: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    order: str = "desc",
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = _clamp(limit)
    w = _qc_where(item, specimen, instrument, section, qc_product, qc_lot, level, status, rule, date_from, date_to)
    direction = "ASC" if order == "asc" else "DESC"
    rows = db.fetch_all(
        "SELECT qc_result_id, qc_run_id, run_at, section_name, serial_number, instrument_name, instrument_role, "
        "assay_id, item_code, item_name, specimen_code, qc_product_code, qc_product_name, qc_lot_number, qc_level, "
        "result_value, unit_snapshot, target_mean_snapshot, target_sd_snapshot, z_score, result_status, "
        "westgard_flags, operator_id, reviewed_by, reviewed_at, note "
        f"FROM v_qc_result_detail{w.sql} ORDER BY run_at {direction} LIMIT %s",
        [*w.params, limit + 1],
    )
    return _rows_result(rows, limit, "qc_results")


def get_qc_summary(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    section: str | None = None,
    qc_product: str | None = None,
    qc_lot: str | None = None,
    level: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = _clamp(limit)
    w = _qc_where(item, specimen, instrument, section, qc_product, qc_lot, level, None, None, date_from, date_to)
    group = (
        "assay_id, serial_number, instrument_name, instrument_role, item_code, item_name, specimen_code, "
        "qc_product_code, qc_lot_number, qc_level"
    )
    valid = "CASE WHEN result_status <> 'INVALID' THEN {} END"
    rows = db.fetch_all(
        f"SELECT {group}, COUNT(*) AS n, "
        "COUNT(CASE WHEN result_status = 'PASS' THEN 1 END) AS pass_count, "
        "COUNT(CASE WHEN result_status = 'WARNING' THEN 1 END) AS warning_count, "
        "COUNT(CASE WHEN result_status = 'FAIL' THEN 1 END) AS fail_count, "
        "COUNT(CASE WHEN result_status = 'INVALID' THEN 1 END) AS invalid_count, "
        f"AVG({valid.format('result_value')}) AS mean_value, "
        f"STDDEV_SAMP({valid.format('result_value')}) AS sd_value, "
        f"AVG({valid.format('z_score')}) AS mean_z, MAX(ABS(z_score)) AS max_abs_z, "
        "MIN(run_at) AS first_run_at, MAX(run_at) AS last_run_at "
        f"FROM v_qc_result_detail{w.sql} GROUP BY {group} "
        "ORDER BY fail_count DESC, instrument_name, item_code, qc_lot_number, qc_level LIMIT %s",
        [*w.params, limit + 1],
    )

    flag_rows = db.fetch_all(
        "SELECT d.assay_id, d.qc_lot_number, d.qc_level, j.rule_name, COUNT(*) AS hits "
        "FROM v_qc_result_detail d "
        "JOIN JSON_TABLE(d.westgard_flags, '$[*]' COLUMNS (rule_name VARCHAR(20) PATH '$')) j"
        f"{w.sql} GROUP BY d.assay_id, d.qc_lot_number, d.qc_level, j.rule_name",
        w.params,
    )
    rule_counts: dict[tuple[str, str, str], dict[str, int]] = {}
    for row in flag_rows:
        key = (row["assay_id"], row["qc_lot_number"], row["qc_level"])
        rule_counts.setdefault(key, {})[row["rule_name"]] = row["hits"]

    for row in rows:
        mean, sd = row["mean_value"], row["sd_value"]
        row["cv_percent"] = round(sd / mean * 100, 3) if mean and sd is not None else None
        row["rule_counts"] = rule_counts.get((row["assay_id"], row["qc_lot_number"], row["qc_level"]), {})
    return _rows_result(
        rows, limit, "qc_results",
        statistics_note="依儀器 × 項目 × 檢體 × 批號 × 濃度分組，主備機不合併；mean、SD、CV 已排除 INVALID 結果。",
    )


def get_qc_targets(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    qc_lot: str | None = None,
    level: str | None = None,
    as_of: str | None = None,
    include_history: bool = False,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = _clamp(limit)
    w = Where()
    _filter_item(w, item, "c.item_code", "c.item_name")
    _filter_specimen(w, specimen, "c.specimen_code", "c.specimen_name")
    _filter_instrument(w, instrument, "c.instrument_id")
    if qc_lot:
        w.add("l.lot_number = %s", qc_lot)
    _filter_level(w, level, "l.level_code")
    if as_of:
        moment = _parse_moment(as_of, "as_of")[0]
        w.add("t.effective_from <= %s AND (t.effective_to IS NULL OR t.effective_to > %s)", moment, moment)
    elif not include_history:
        w.add("t.effective_to IS NULL")
    rows = db.fetch_all(
        "SELECT t.qc_target_id, c.assay_id, c.serial_number, c.instrument_name, c.instrument_role, c.item_code, "
        "c.item_name, c.specimen_code, c.result_unit, p.product_code AS qc_product_code, "
        "p.product_name AS qc_product_name, l.lot_number AS qc_lot_number, l.level_code AS qc_level, "
        "l.lot_status, t.target_source, t.target_mean, t.target_sd, t.acceptable_low, t.acceptable_high, "
        "t.effective_from, t.effective_to, t.approved_by, t.approved_at, t.note "
        "FROM qc_target_values t "
        "JOIN v_assay_catalog c ON c.assay_id = t.assay_id "
        "JOIN qc_lots l ON l.qc_lot_id = t.qc_lot_id "
        f"JOIN qc_products p ON p.qc_product_id = t.qc_product_id{w.sql} "
        "ORDER BY c.instrument_name, c.item_code, l.lot_number, l.level_code, t.effective_from DESC LIMIT %s",
        [*w.params, limit + 1],
    )
    for row in rows:
        mean, sd = row["target_mean"], row["target_sd"]
        row["target_cv_percent"] = round(sd / mean * 100, 3) if mean else None
    return _rows_result(rows, limit, "qc_target_values")


def get_loaded_reagents(
    instrument: str | None = None,
    product: str | None = None,
    lot_number: str | None = None,
    current_only: bool = True,
    at_time: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = _clamp(limit)
    w = Where()
    _filter_instrument(w, instrument, "l.instrument_id")
    _filter_product(w, product, "p.product_code", "p.product_name")
    if lot_number:
        w.add("rl.lot_number = %s", lot_number)
    if at_time:
        moment = _parse_moment(at_time, "at_time")[0]
        w.add("l.loaded_at <= %s AND (l.unloaded_at IS NULL OR l.unloaded_at > %s)", moment, moment)
    elif current_only:
        w.add("l.unloaded_at IS NULL")
    rows = db.fetch_all(
        "SELECT l.reagent_load_id, i.serial_number, i.instrument_name, i.instrument_role, l.position_code, "
        "p.product_code, p.product_name, rl.lot_number, rl.expiry_date, rl.lot_status, l.loaded_at, "
        "l.unloaded_at, l.loaded_by, l.unloaded_by, l.note "
        "FROM instrument_reagent_loads l "
        "JOIN instruments i ON i.instrument_id = l.instrument_id "
        "JOIN reagent_lots rl ON rl.reagent_lot_id = l.reagent_lot_id "
        f"JOIN reagent_products p ON p.reagent_product_id = rl.reagent_product_id{w.sql} "
        "ORDER BY i.instrument_name, l.loaded_at DESC LIMIT %s",
        [*w.params, limit + 1],
    )
    return _rows_result(rows, limit, "instrument_reagent_loads")


def trace_reagent_lot(lot_number: str, product: str | None = None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    limit = _clamp(limit)
    lots = _lot_rows("reagent", product=product, lot_number=lot_number)
    if not lots:
        return {"lot_number": lot_number, "lots": [], "note": "查無此試劑批號。"}
    ids = [lot["lot_id"] for lot in lots]
    marks = _placeholders(ids)

    lineage = db.fetch_all(
        "SELECT l.reagent_lot_id AS lot_id, l.same_lot_as_previous, prev.lot_number AS previous_lot_number "
        "FROM reagent_lots l LEFT JOIN reagent_lots prev ON prev.reagent_lot_id = l.previous_reagent_lot_id "
        f"WHERE l.reagent_lot_id IN ({marks})",
        ids,
    )
    by_id = {row.pop("lot_id"): row for row in lineage}
    for lot in lots:
        lot.update(by_id.get(lot["lot_id"], {}))

    loads = db.fetch_all(
        "SELECT p.product_code, rl.lot_number, i.serial_number, i.instrument_name, i.instrument_role, "
        "l.position_code, l.loaded_at, l.unloaded_at, l.loaded_by, l.unloaded_by "
        "FROM instrument_reagent_loads l "
        "JOIN reagent_lots rl ON rl.reagent_lot_id = l.reagent_lot_id "
        "JOIN reagent_products p ON p.reagent_product_id = rl.reagent_product_id "
        "JOIN instruments i ON i.instrument_id = l.instrument_id "
        f"WHERE l.reagent_lot_id IN ({marks}) ORDER BY l.loaded_at DESC LIMIT %s",
        [*ids, limit],
    )
    qc_usage = db.fetch_all(
        "SELECT p.product_code, rl.lot_number, d.serial_number, d.instrument_name, d.item_code, d.specimen_code, "
        "d.qc_lot_number, d.qc_level, COUNT(*) AS n, "
        "COUNT(CASE WHEN d.result_status = 'FAIL' THEN 1 END) AS fail_count, "
        "COUNT(CASE WHEN d.result_status = 'WARNING' THEN 1 END) AS warning_count, "
        "MIN(d.run_at) AS first_run_at, MAX(d.run_at) AS last_run_at "
        "FROM qc_result_reagents q "
        "JOIN reagent_lots rl ON rl.reagent_lot_id = q.reagent_lot_id "
        "JOIN reagent_products p ON p.reagent_product_id = rl.reagent_product_id "
        "JOIN v_qc_result_detail d ON d.qc_result_id = q.qc_result_id "
        f"WHERE q.reagent_lot_id IN ({marks}) "
        "GROUP BY p.product_code, rl.lot_number, d.serial_number, d.instrument_name, d.item_code, d.specimen_code, "
        "d.qc_lot_number, d.qc_level ORDER BY fail_count DESC, d.instrument_name, d.item_code LIMIT %s",
        [*ids, limit],
    )
    calibrations = db.fetch_all(
        "SELECT p.product_code, rl.lot_number, e.calibration_event_id, e.performed_at, e.event_status, "
        "c.serial_number, c.instrument_name, c.item_code, c.specimen_code, a.assay_status "
        "FROM calibration_event_reagents r "
        "JOIN reagent_lots rl ON rl.reagent_lot_id = r.reagent_lot_id "
        "JOIN reagent_products p ON p.reagent_product_id = rl.reagent_product_id "
        "JOIN calibration_events e ON e.calibration_event_id = r.calibration_event_id "
        "JOIN calibration_event_assays a ON a.calibration_event_id = r.calibration_event_id AND a.assay_id = r.assay_id "
        "JOIN v_assay_catalog c ON c.assay_id = r.assay_id "
        f"WHERE r.reagent_lot_id IN ({marks}) ORDER BY e.performed_at DESC LIMIT %s",
        [*ids, limit],
    )
    result: dict[str, Any] = {
        "lot_number": lot_number,
        "lots": lots,
        "loads": loads,
        "qc_usage": qc_usage,
        "calibration_usage": calibrations,
    }
    notes = []
    if len({lot["product_code"] for lot in lots}) > 1:
        notes.append("此批號同時屬於多個試劑產品，判讀時請區分 product_code，或以 product 參數限定。")
    if not (loads or qc_usage or calibrations):
        notes.append(f"尚無此批號的裝機、品管或校正紀錄。{_empty_note('qc_result_reagents')}")
    if notes:
        result["note"] = " ".join(notes)
    return result


def trace_qc_result(qc_result_id: str) -> dict[str, Any]:
    rows = db.fetch_all("SELECT * FROM v_qc_result_detail WHERE qc_result_id = %s", [qc_result_id])
    if not rows:
        raise ValueError(f"找不到品管結果 {qc_result_id}。{_empty_note('qc_results')}")
    result = rows[0]
    run_at = result["run_at"].replace("T", " ")
    assay_id, instrument_id = result["assay_id"], result["instrument_id"]

    run = db.fetch_all(
        "SELECT run_status, note AS run_note FROM qc_runs WHERE qc_run_id = %s", [result["qc_run_id"]]
    )
    target = db.fetch_all(
        "SELECT t.qc_target_id, t.target_source, t.target_mean, t.target_sd, t.acceptable_low, t.acceptable_high, "
        "t.effective_from, t.effective_to, t.approved_by "
        "FROM qc_results r JOIN qc_target_values t ON t.qc_target_id = r.qc_target_id "
        "WHERE r.qc_result_id = %s",
        [qc_result_id],
    )
    reagents = db.fetch_all(
        "SELECT q.reagent_role, p.product_code, p.product_name, l.lot_number, l.expiry_date, l.lot_status "
        "FROM qc_result_reagents q "
        "JOIN reagent_lots l ON l.reagent_lot_id = q.reagent_lot_id "
        "JOIN reagent_products p ON p.reagent_product_id = q.reagent_product_id "
        "WHERE q.qc_result_id = %s",
        [qc_result_id],
    )
    on_board = db.fetch_all(
        "SELECT l.position_code, p.product_code, p.product_name, rl.lot_number, l.loaded_at, l.unloaded_at "
        "FROM instrument_reagent_loads l "
        "JOIN reagent_lots rl ON rl.reagent_lot_id = l.reagent_lot_id "
        "JOIN reagent_products p ON p.reagent_product_id = rl.reagent_product_id "
        "JOIN assay_reagent_requirements r ON r.reagent_product_id = rl.reagent_product_id AND r.assay_id = %s "
        "WHERE l.instrument_id = %s AND l.loaded_at <= %s AND (l.unloaded_at IS NULL OR l.unloaded_at > %s)",
        [assay_id, instrument_id, run_at, run_at],
    )
    last_calibration = db.fetch_all(
        "SELECT e.calibration_event_id, e.performed_at, e.calibration_reason, e.event_status, a.assay_status, "
        "a.pre_bias, a.post_bias, cl.lot_number AS calibrator_lot, cp.product_code AS calibrator_code "
        "FROM calibration_event_assays a "
        "JOIN calibration_events e ON e.calibration_event_id = a.calibration_event_id "
        "JOIN calibrator_lots cl ON cl.calibrator_lot_id = e.calibrator_lot_id "
        "JOIN calibrator_products cp ON cp.calibrator_product_id = e.calibrator_product_id "
        "WHERE a.assay_id = %s AND e.performed_at <= %s ORDER BY e.performed_at DESC LIMIT 1",
        [assay_id, run_at],
    )
    series = db.fetch_all(
        "SELECT qc_result_id, run_at, result_value, z_score, result_status, westgard_flags "
        "FROM v_qc_result_detail "
        "WHERE assay_id = %s AND qc_lot_number = %s AND qc_level = %s AND run_at <= %s "
        "ORDER BY run_at DESC LIMIT 11",
        [assay_id, result["qc_lot_number"], result["qc_level"], run_at],
    )
    return {
        "result": result,
        "run": run[0] if run else None,
        "target_version": target[0] if target else None,
        "reagents_recorded": reagents,
        "reagents_on_board_at_run": on_board,
        "last_calibration_before_run": last_calibration[0] if last_calibration else None,
        "preceding_series_same_instrument": series,
        "note": "判讀請以 result 內的 snapshot 為準；series 為同儀器、同 assay、同批號濃度的連續點，可用於檢視 Westgard 連續規則。",
    }


def get_calibration_history(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    calibrator_product: str | None = None,
    calibrator_lot: str | None = None,
    event_status: str | None = None,
    assay_status: str | None = None,
    reason: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    limit = _clamp(limit)
    w = Where()
    _filter_item(w, item, "c.item_code", "c.item_name")
    _filter_specimen(w, specimen, "c.specimen_code", "c.specimen_name")
    _filter_instrument(w, instrument, "c.instrument_id")
    _filter_product(w, calibrator_product, "p.product_code", "p.product_name")
    if calibrator_lot:
        w.add("l.lot_number = %s", calibrator_lot)
    if event_status:
        w.add("e.event_status = %s", event_status)
    if assay_status:
        w.add("a.assay_status = %s", assay_status)
    if reason:
        w.add("e.calibration_reason = %s", reason)
    _filter_time(w, "e.performed_at", date_from, date_to)
    rows = db.fetch_all(
        "SELECT e.calibration_event_id, e.performed_at, c.serial_number, c.instrument_name, c.instrument_role, "
        "c.item_code, c.item_name, c.specimen_code, p.product_code AS calibrator_code, "
        "p.product_name AS calibrator_name, l.lot_number AS calibrator_lot, l.level_code AS calibrator_level, "
        "e.calibration_reason, e.event_status, a.assay_status, a.pre_bias, a.post_bias, e.operator_id, "
        "e.instrument_event_id, e.raw_data_uri, e.note AS event_note, a.note AS assay_note "
        "FROM calibration_event_assays a "
        "JOIN calibration_events e ON e.calibration_event_id = a.calibration_event_id "
        "JOIN v_assay_catalog c ON c.assay_id = a.assay_id "
        "JOIN calibrator_lots l ON l.calibrator_lot_id = e.calibrator_lot_id "
        f"JOIN calibrator_products p ON p.calibrator_product_id = e.calibrator_product_id{w.sql} "
        "ORDER BY e.performed_at DESC, c.item_code LIMIT %s",
        [*w.params, limit + 1],
    )
    return _rows_result(rows, limit, "calibration_events")


def run_readonly_query(sql: str, limit: int = 200) -> dict[str, Any]:
    limit = _clamp(limit)
    checked = validate_select(sql, catalog.table_names(), cap=limit + 1)
    rows = db.fetch_all(checked, None, max_rows=limit + 1)
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {"executed_sql": checked, "row_count": len(rows), "truncated": truncated, "rows": rows}


def table_row_counts(tables: Iterable[str]) -> dict[str, int]:
    return {
        table: db.fetch_all(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]
        for table in tables
    }


def check_connection() -> dict[str, Any]:
    info = db.fetch_all(
        "SELECT VERSION() AS version, DATABASE() AS database_name, CURRENT_USER() AS account, "
        "@@tx_read_only AS session_read_only"
    )[0]
    grants = [
        re.sub(r"\s+IDENTIFIED BY.*", "", next(iter(row.values())), flags=re.IGNORECASE)
        for row in db.fetch_all("SHOW GRANTS")
    ]
    can_write = any(re.search(r"\b(ALL PRIVILEGES|INSERT|UPDATE|DELETE|DROP|ALTER|CREATE)\b", g) for g in grants)
    base_tables = [
        name for name, meta in snapshot.load_snapshot()["tables"].items() if meta["type"] == "BASE TABLE"
    ]
    result: dict[str, Any] = {
        **info,
        "session_read_only": bool(info["session_read_only"]),
        "grants": grants,
        "account_can_write": can_write,
        "schema_drift": snapshot.diff_schema(snapshot.load_snapshot(), snapshot.read_live_schema()),
        "row_counts": table_row_counts(base_tables),
    }
    if can_write:
        result["warning"] = (
            "此帳號在資料庫層有寫入權限。MCP 仍以唯讀 transaction 與 SQL 檢查防護，"
            "但建議改用只有 SELECT 權限的專用帳號（見 sql/create_readonly_user.sql）。"
        )
    return result
