"""Turn a natural-language question into intents, recognised entities and suggested tool calls."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from . import db, knowledge, queries
from .config import ConfigError


@dataclass(frozen=True)
class Intent:
    id: str
    label: str
    tool: str
    patterns: tuple[str, ...]
    tables: tuple[str, ...]


INTENTS: tuple[Intent, ...] = (
    Intent(
        "schema", "資料庫架構與關聯", "get_schema_overview",
        (r"架構", r"schema", r"資料表", r"欄位", r"關聯", r"\bjoin\b", r"怎麼串", r"外鍵", r"[a-z]+_[a-z_]+\s*(跟|和|與|to)\s*[a-z]+_[a-z_]+"),
        (),
    ),
    Intent(
        "assay_catalog", "哪些儀器／項目／檢體可以做", "find_assays",
        (r"哪些(項目|儀器|檢體)", r"哪(台|幾台)", r"可以做", r"有做", r"檢驗設定", r"assay", r"有幾台", r"儀器清單"),
        ("instrument_assays",),
    ),
    Intent(
        "assay_materials", "項目需要的試劑、品管液、校正液", "find_assays",
        (r"(需要|使用|用|搭配|對應|配).{0,6}(哪些|什麼|哪支|哪種|哪個)?.{0,4}(試劑|品管液|校正液)", r"(試劑|品管液|校正液).{0,4}(是什麼|有哪些|對應)"),
        ("assay_reagent_requirements", "assay_qc_requirements", "assay_calibrator_requirements"),
    ),
    Intent(
        "product_usage", "某產品被哪些項目使用", "get_product_usage",
        (r"用在", r"用於", r"共用", r"涵蓋", r"用在.{0,4}(哪些|什麼)", r"(哪些|什麼)項目(會)?(使用|用)", r"影響哪些項目"),
        ("assay_reagent_requirements", "assay_qc_requirements", "assay_calibrator_requirements"),
    ),
    Intent(
        "lots", "批號、效期、庫存與平測", "find_lots",
        (r"批號", r"效期", r"過期", r"到期", r"庫存", r"入庫", r"開封", r"平測", r"平行測試", r"\blot\b", r"expir", r"可用的"),
        ("reagent_lots", "qc_lots", "calibrator_lots"),
    ),
    Intent(
        "qc_results", "品管結果明細與趨勢", "get_qc_results",
        (r"品管(?!液)", r"\bqc\b", r"趨勢", r"l-?j", r"levey", r"z[- ]?(score|值)", r"失控", r"westgard", r"\bfail\b", r"不合格", r"違反", r"1-3s|2-2s|r-4s|4-1s|10x|1-2s", r"量測值"),
        ("qc_results", "qc_runs"),
    ),
    Intent(
        "qc_summary", "品管統計彙總", "get_qc_summary",
        (r"統計", r"平均", r"\bcv\b", r"變異", r"幾次", r"幾筆", r"多少次", r"比率", r"通過率", r"失控率", r"彙總", r"總結", r"累積", r"(失控|fail|warning|警告).{0,6}(幾次|多少次)|(幾次|多少次).{0,6}(失控|fail|warning|警告)"),
        ("qc_results",),
    ),
    Intent(
        "qc_targets", "品管目標值 mean/SD", "get_qc_targets",
        (r"目標值", r"\btarget\b", r"\bmean\b", r"\bsd\b", r"允收", r"範圍", r"靶值"),
        ("qc_target_values",),
    ),
    Intent(
        "reagent_loads", "試劑裝機歷程", "get_loaded_reagents",
        (r"上機", r"裝機", r"機上", r"下機", r"裝了", r"位置", r"\bload"),
        ("instrument_reagent_loads",),
    ),
    Intent(
        "reagent_trace", "試劑批號追溯", "trace_reagent_lot",
        (r"追溯", r"追蹤", r"召回", r"recall", r"(這|該|此)?批.{0,6}影響", r"哪些.{0,4}結果", r"批號.{0,10}(影響|用過|用了)", r"用了.{0,6}批"),
        ("reagent_lots", "instrument_reagent_loads", "qc_result_reagents", "calibration_event_reagents"),
    ),
    Intent(
        "result_audit", "單筆品管結果稽核", "trace_qc_result",
        (r"稽核", r"這筆", r"該筆", r"當時", r"(這|該)筆.{0,8}(結果|品管)", r"為什麼.{0,12}(fail|失控|warning)", r"為何", r"qc_result_id"),
        ("qc_results", "qc_target_values", "qc_result_reagents"),
    ),
    Intent(
        "calibration", "校正事件與結果", "get_calibration_history",
        (r"校正(?!液)", r"calibrat", r"\bbias\b", r"偏差"),
        ("calibration_events", "calibration_event_assays"),
    ),
)

_RULES = {"1-2S": "1-2s", "1-3S": "1-3s", "2-2S": "2-2s", "R-4S": "R-4s", "4-1S": "4-1s", "10X": "10x"}
_STATUS_PATTERNS = (
    ("FAIL", r"\bfail\b|失控|不合格|未通過|不通過"),
    ("WARNING", r"\bwarning\b|警告"),
    ("INVALID", r"\binvalid\b|無效"),
    ("PASS", r"\bpass\b|(?<!不)合格|(?<![未不])通過(?!率)"),
)
_LOT_STATUS_PATTERNS = (
    ("RECALLED", r"召回"), ("QUARANTINE", r"隔離"), ("IN_USE", r"使用中"), ("EXHAUSTED", r"用完|用罄"),
)
_ALNUM_LEFT = r"(?<![A-Za-z0-9])"
_ALNUM_RIGHT = r"(?![A-Za-z0-9])"
# ASCII mode makes \b work next to Chinese, which Unicode mode treats as word characters.
_FLAGS = re.IGNORECASE | re.ASCII


def _code_pattern(code: str) -> re.Pattern[str]:
    # Short codes stay case-aware so that "mg" in mg/dL never reads as MG (magnesium).
    body = re.escape(code)
    if len(code) <= 2:
        alternatives = {body, re.escape(code.upper())}
        return re.compile(_ALNUM_LEFT + "(?:" + "|".join(sorted(alternatives)) + ")" + _ALNUM_RIGHT)
    return re.compile(_ALNUM_LEFT + body + _ALNUM_RIGHT, re.IGNORECASE)


def _text_pattern(text: str) -> re.Pattern[str]:
    if text.isascii():
        return _code_pattern(text)
    return re.compile(re.escape(text), re.IGNORECASE)


def _name_variants(name: str | None) -> set[str]:
    if not name:
        return set()
    parts = {name.strip()}
    parts.update(p.strip() for p in re.split(r"[(),]", name) if len(p.strip()) >= 4)
    # "Creatinine (Urine)" must not turn the word Urine into an item.
    return {p for p in parts if p.lower() not in {"urine", "blood"}}


def _instrument_variants(row: dict[str, Any]) -> set[str]:
    variants = {row["serial_number"]}
    for text in (row["instrument_name"], row["model_name"]):
        if text:
            words = text.split()
            # "Sysmex XN-3000-A" also answers to "XN-3000-A".
            variants.update(" ".join(words[i:]) for i in range(len(words)))
    return variants


def _dictionary_terms(dictionary: dict[str, list[dict[str, Any]]]) -> list[tuple[str, str, dict[str, Any]]]:
    terms: list[tuple[str, str, dict[str, Any]]] = []
    for row in dictionary["section"]:
        record = {"section_code": row["section_code"], "section_name": row["section_name"]}
        terms.extend((t, "section", record) for t in (row["section_code"], row["section_name"]))
    sections = {row["section_code"]: row for row in dictionary["section"]}
    for alias, code in knowledge.SECTION_ALIASES.items():
        if code in sections:
            row = sections[code]
            terms.append((alias, "section", {"section_code": code, "section_name": row["section_name"]}))

    specimens = {row["specimen_code"]: row for row in dictionary["specimen"]}
    for row in dictionary["specimen"]:
        record = {"specimen_code": row["specimen_code"], "specimen_name": row["specimen_name"]}
        # B and U are too short to spot in free text, so they come from the aliases below.
        if len(row["specimen_code"]) > 2:
            terms.append((row["specimen_code"], "specimen", record))
        terms.append((row["specimen_name"], "specimen", record))
    for alias, code in knowledge.SPECIMEN_ALIASES.items():
        if code in specimens:
            row = specimens[code]
            terms.append((alias, "specimen", {"specimen_code": code, "specimen_name": row["specimen_name"]}))

    items = {row["item_code"]: row for row in dictionary["item"]}
    for row in dictionary["item"]:
        record = {"item_code": row["item_code"], "item_name": row["item_name"]}
        terms.append((row["item_code"], "item", record))
        terms.extend((t, "item", record) for t in _name_variants(row["item_name"]))
    for alias, code in knowledge.ITEM_ALIASES.items():
        if code in items:
            terms.append((alias, "item", {"item_code": code, "item_name": items[code]["item_name"]}))

    for row in dictionary["instrument"]:
        record = {
            "serial_number": row["serial_number"],
            "instrument_name": row["instrument_name"],
            "instrument_role": row["instrument_role"],
        }
        terms.extend((t, "instrument", record) for t in _instrument_variants(row))

    for kind in queries.LOT_KINDS:
        for row in dictionary[f"{kind}_product"]:
            record = {"kind": kind, "product_code": row["product_code"], "product_name": row["product_name"]}
            terms.extend((t, "product", record) for t in (row["product_code"], row["product_name"]))
    return terms


def match_entities(question: str, dictionary: dict[str, list[dict[str, Any]]]) -> tuple[dict[str, list[dict[str, Any]]], str]:
    """Longest term wins, so 血液組 is a section (not 血液 the specimen) and CA 19-9 is not CA."""
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for term, kind, record in _dictionary_terms(dictionary):
        if term:
            groups.setdefault(term, []).append((kind, record))

    text = question
    found: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, tuple]] = set()
    for term in sorted(groups, key=len, reverse=True):
        pattern = _text_pattern(term)
        matched = pattern.search(text)
        if not matched:
            continue
        # One term can name several records: a model name covers every unit of that model.
        for kind, record in groups[term]:
            key = (kind, tuple(sorted(record.items())))
            if key not in seen:
                seen.add(key)
                found.setdefault(kind, []).append({**record, "matched_text": matched.group(0)})
        text = pattern.sub(lambda m: " " * len(m.group(0)), text)
    return found, text


def _load_dictionary() -> dict[str, list[dict[str, Any]]]:
    dictionary = {
        "section": db.fetch_all("SELECT section_code, section_name FROM lab_sections"),
        "specimen": db.fetch_all("SELECT specimen_code, specimen_name FROM specimen_types"),
        # Sorted so a bare name resolves to GLU before GLU-CSF.
        "item": db.fetch_all("SELECT item_code, item_name FROM test_items ORDER BY CHAR_LENGTH(item_code), item_code"),
        "instrument": db.fetch_all(
            "SELECT serial_number, instrument_name, model_name, instrument_role FROM instruments"
        ),
    }
    for kind, spec in queries.LOT_KINDS.items():
        dictionary[f"{kind}_product"] = db.fetch_all(f"SELECT product_code, product_name FROM {spec['products']}")
    return dictionary


def _match_lots(masked_question: str) -> list[dict[str, Any]]:
    tokens = sorted(set(re.findall(r"[A-Za-z0-9][A-Za-z0-9._-]{2,}", masked_question)))
    if not tokens:
        return []
    marks = ", ".join(["%s"] * len(tokens))
    lots: list[dict[str, Any]] = []
    for kind, spec in queries.LOT_KINDS.items():
        rows = db.fetch_all(
            f"SELECT l.lot_number, p.product_code, p.product_name, l.lot_status, l.expiry_date "
            f"FROM {spec['lots']} l JOIN {spec['products']} p ON p.{spec['product_id']} = l.{spec['product_id']} "
            f"WHERE l.lot_number IN ({marks})",
            tokens,
        )
        lots.extend({"kind": kind, **row} for row in rows)
    return lots


def _filters_from_text(question: str) -> dict[str, Any]:
    filters: dict[str, Any] = {}
    for status, pattern in _STATUS_PATTERNS:
        if re.search(pattern, question, _FLAGS):
            filters["status"] = status
            break
    for status, pattern in _LOT_STATUS_PATTERNS:
        if re.search(pattern, question):
            filters["lot_status"] = status
            break
    rule = re.search(r"(?<![0-9A-Za-z])(1-2s|1-3s|2-2s|r-4s|4-1s|10x)(?![0-9A-Za-z])", question, re.IGNORECASE)
    if rule:
        filters["rule"] = _RULES[rule.group(1).upper()]
    level = re.search(r"(?<![A-Za-z0-9])(?:level|lv|l|濃度)\s*([1-3])(?![0-9])", question, re.IGNORECASE)
    if level:
        filters["level"] = level.group(1)
    if re.search(r"已過期", question):
        filters["expiring_within_days"] = 0
    elif re.search(r"過期|到期|效期|expir", question, re.IGNORECASE):
        days = re.search(r"([0-9]+)\s*(?:天|日|days?)", question, re.IGNORECASE)
        filters["expiring_within_days"] = int(days.group(1)) if days else 30
    if re.search(r"試劑", question):
        filters["kind"] = "reagent"
    elif re.search(r"品管液", question):
        filters["kind"] = "qc"
    elif re.search(r"校正液", question):
        filters["kind"] = "calibrator"
    return filters


def _first(entities: dict[str, list[dict[str, Any]]], kind: str, key: str) -> Any:
    rows = entities.get(kind)
    return rows[0][key] if rows else None


def _instrument_argument(entities: dict[str, list[dict[str, Any]]]) -> str | None:
    rows = entities.get("instrument")
    if not rows:
        return None
    # A model mentioned by name covers every unit; the server's LIKE filter keeps them all.
    return rows[0]["serial_number"] if len(rows) == 1 else rows[0]["matched_text"]


def _suggest_arguments(
    intent: Intent,
    entities: dict[str, list[dict[str, Any]]],
    lots: list[dict[str, Any]],
    filters: dict[str, Any],
) -> dict[str, Any]:
    item = _first(entities, "item", "item_code")
    specimen = _first(entities, "specimen", "specimen_code")
    section = _first(entities, "section", "section_code")
    instrument = _instrument_argument(entities)
    product = _first(entities, "product", "product_code")
    qc_lot = next((lot["lot_number"] for lot in lots if lot["kind"] == "qc"), None)
    reagent_lot = next((lot["lot_number"] for lot in lots if lot["kind"] == "reagent"), None)
    any_lot = lots[0]["lot_number"] if lots else None

    args: dict[str, Any] = {}
    if intent.tool == "find_assays":
        args = {"item": item, "specimen": specimen, "instrument": instrument, "section": section}
        if intent.id == "assay_materials":
            args["include_materials"] = True
    elif intent.tool == "get_product_usage":
        args = {"product": product, "kind": filters.get("kind")}
    elif intent.tool == "find_lots":
        args = {
            "kind": filters.get("kind"),
            "product": product,
            "lot_number": any_lot,
            "level": filters.get("level"),
            "status": filters.get("lot_status"),
            "expiring_within_days": filters.get("expiring_within_days"),
        }
    elif intent.tool in ("get_qc_results", "get_qc_summary"):
        args = {
            "item": item, "specimen": specimen, "instrument": instrument, "section": section,
            "qc_product": product if _first(entities, "product", "kind") == "qc" else None,
            "qc_lot": qc_lot, "level": filters.get("level"),
        }
        if intent.tool == "get_qc_results":
            args.update(status=filters.get("status"), rule=filters.get("rule"))
    elif intent.tool == "get_qc_targets":
        args = {"item": item, "specimen": specimen, "instrument": instrument, "qc_lot": qc_lot, "level": filters.get("level")}
    elif intent.tool == "get_loaded_reagents":
        args = {"instrument": instrument, "product": product, "lot_number": reagent_lot}
    elif intent.tool == "trace_reagent_lot":
        args = {"lot_number": reagent_lot or any_lot, "product": product}
    elif intent.tool == "get_calibration_history":
        args = {"item": item, "specimen": specimen, "instrument": instrument, "event_status": filters.get("status")}
    return {key: value for key, value in args.items() if value is not None}


def route_question(question: str) -> dict[str, Any]:
    text = question.strip()
    if not text:
        raise ValueError("question 不可為空白")

    scored = []
    for intent in INTENTS:
        hits = [p for p in intent.patterns if re.search(p, text, _FLAGS)]
        if hits:
            scored.append((len(hits), intent))
    scored.sort(key=lambda pair: -pair[0])
    top = [intent for _, intent in scored[:3]]

    filters = _filters_from_text(text)
    entities: dict[str, list[dict[str, Any]]] = {}
    lots: list[dict[str, Any]] = []
    counts: dict[str, int] = {}
    notes: list[str] = []
    try:
        entities, masked = match_entities(text, _load_dictionary())
        lots = _match_lots(masked)
        tables = sorted({table for intent in top for table in intent.tables})
        counts = queries.table_row_counts(tables)
    except (ConfigError, db.DatabaseError) as exc:
        notes.append(f"無法連線資料庫，以下建議未比對實際主檔：{exc}")

    empty = [table for table, n in counts.items() if n == 0]
    if empty:
        notes.append(f"以下資料表目前完全沒有資料：{', '.join(empty)}。查無結果代表尚未登錄，不能解讀為正常。")
    for kind in ("item", "product"):
        if len(entities.get(kind, [])) > 1:
            notes.append(f"問題提到多個 {kind}，工具一次查一個，請依 matched_entities 分次呼叫。")
    if {"qc_results", "qc_summary"} & {intent.id for intent in top} and len(entities.get("instrument", [])) > 1:
        notes.append("問題涵蓋多台儀器；品管判讀必須分機器呈現，不可把主機與備機合併。")
    if not top:
        notes.append("沒有對應到明確意圖：可先用 search_entities 找主檔，或用 get_schema_overview 看架構。")

    return {
        "question": text,
        "today": queries.lab_today().isoformat(),
        "timezone": "Asia/Taipei",
        "date_hint": "若問題含「上週」「本月」等相對時間，請以 today 換算成 date_from / date_to。",
        "recommended": [
            {
                "intent": intent.id,
                "label": intent.label,
                "tool": intent.tool,
                "suggested_arguments": _suggest_arguments(intent, entities, lots, filters),
                "tables": list(intent.tables),
            }
            for intent in top
        ],
        "matched_entities": entities,
        "matched_lots": lots,
        "detected_filters": filters,
        "table_row_counts": counts,
        "notes": notes,
    }
