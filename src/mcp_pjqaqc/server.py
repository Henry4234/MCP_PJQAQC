"""MCP tools and resources. Docstrings are what the connected model reads to choose a tool."""
from __future__ import annotations

import functools
import json
from typing import Any, Callable, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from . import catalog, queries, router
from .config import ConfigError
from .db import DatabaseError

INSTRUCTIONS = """\
這個 server 認識 PJQAQC 檢驗科品管資料庫（MariaDB）的完整架構，並以唯讀方式查詢資料。

建議流程：
1. 不確定該查什麼時，先呼叫 route_question，它會回傳對應的工具、建議參數、已辨識的項目／儀器／批號，以及今天日期。
2. 優先使用專用工具（find_assays、get_qc_results、find_lots⋯），它們已處理好 JOIN 與資料陷阱。
3. 專用工具無法回答時，才用 describe_table、find_join_path 了解結構，再用 run_readonly_query 下 SELECT。

回答時必須遵守：
- 品管判讀與統計必須分儀器呈現，主機與備機不可合併。
- 回傳 note 指出資料表為空時，要告訴使用者「尚未登錄資料」，不可回答成「沒有異常」。
- lot_status 不代表未過期，請看 expired_by_date。
"""

mcp = MCPServer("PJQAQC", instructions=INSTRUCTIONS, version="0.1.0")

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

LotKind = Literal["reagent", "qc", "calibrator"]
LotStatus = Literal["QUARANTINE", "AVAILABLE", "IN_USE", "EXHAUSTED", "EXPIRED", "RECALLED"]
QcStatus = Literal["PASS", "WARNING", "FAIL", "INVALID"]


def tool(fn: Callable[..., dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    """Register a read-only tool. The SDK hides other exceptions, and the model needs these messages."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
        try:
            return fn(*args, **kwargs)
        except (ValueError, ConfigError, DatabaseError) as exc:
            raise ToolError(str(exc)) from exc

    return mcp.tool(annotations=READ_ONLY)(wrapper)


@tool
def route_question(question: str) -> dict[str, Any]:
    """分析自然語言問題，回傳該用哪個工具、建議參數、辨識到的項目／儀器／檢體／批號、今天日期與資料表筆數。

    不確定從哪裡開始時先呼叫它。例如「XN-3000 的 WBC 上週有幾次失控？」。相對日期請用回傳的 today 自行換算。
    """
    return router.route_question(question)


@tool
def get_schema_overview() -> dict[str, Any]:
    """取得資料庫架構總覽：核心原則、分層與各表用途、關聯流向、業務規則、Westgard 規則。不需要連線資料庫。"""
    return catalog.overview()


@tool
def describe_table(table: str) -> dict[str, Any]:
    """取得單一資料表或 View 的欄位、型別、允許值、中文說明、主鍵、唯一鍵與外鍵。不需要連線資料庫。"""
    return catalog.describe(table)


@tool
def find_join_path(from_table: str, to_table: str) -> dict[str, Any]:
    """依外鍵找出兩張表的最短 JOIN 路徑，回傳可直接使用的 FROM ... JOIN ... ON 片段，含複合外鍵。不需要連線資料庫。"""
    return catalog.join_paths(from_table, to_table)


@tool
def check_connection() -> dict[str, Any]:
    """檢查資料庫連線：版本、帳號權限、各表筆數，以及線上結構與本 server 所知結構是否一致。"""
    return queries.check_connection()


@tool
def search_entities(keyword: str, entity_types: list[str] | None = None, limit: int = 20) -> dict[str, Any]:
    """用關鍵字模糊搜尋組別、儀器、檢體、項目、產品與批號，用來找出正確代碼。

    entity_types 可選：section, instrument, specimen, item, reagent_product, qc_product, calibrator_product,
    reagent_lot, qc_lot, calibrator_lot；省略則全部搜尋。支援常用中文名，例如 血糖、白血球。
    """
    return queries.search_entities(keyword, entity_types, limit)


@tool
def find_assays(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    section: str | None = None,
    active_only: bool = True,
    include_materials: bool = False,
    include_lots: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """查檢驗設定（儀器 × 項目 × 檢體）。回答「哪台儀器做什麼」「GLU 要用哪些試劑、品管液、校正液」。

    item 為項目代碼或完整名稱（完全相符）；specimen 為檢體代碼或名稱；instrument 可為序號、機台名或機型片段；
    section 為組別代碼或名稱。include_materials 附上三類產品；include_lots 再附上目前可用批號。
    """
    return queries.find_assays(item, specimen, instrument, section, active_only, include_materials, include_lots, limit)


@tool
def get_product_usage(
    product: str,
    kind: LotKind | Literal["all"] = "all",
    active_only: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """反查某試劑、品管液或校正液產品被哪些 assay 使用。回答「Ci-Trol 用在哪些項目」。product 為代碼或名稱片段。"""
    return queries.get_product_usage(product, kind, active_only, limit)


@tool
def find_lots(
    kind: LotKind | Literal["all"] = "all",
    product: str | None = None,
    lot_number: str | None = None,
    status: LotStatus | None = None,
    level: str | None = None,
    expiring_within_days: int | None = None,
    parallel_test_done: bool | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """查試劑、品管液、校正液批號的效期、狀態與平測進度。

    expiring_within_days=N 回傳 N 天內到期且未用罄的批號，也包含已過期者；0 表示今天以前已到期。
    每筆含 days_to_expiry 與 expired_by_date，因為 lot_status 不會隨日期自動更新。
    """
    return queries.find_lots(kind, product, lot_number, status, level, expiring_within_days, parallel_test_done, limit)


@tool
def get_qc_results(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    section: str | None = None,
    qc_product: str | None = None,
    qc_lot: str | None = None,
    level: str | None = None,
    status: QcStatus | None = None,
    rule: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    order: Literal["desc", "asc"] = "desc",
    limit: int = 100,
) -> dict[str, Any]:
    """查品管結果明細與趨勢：量測值、當時 mean/SD 快照、Z-score、判讀、Westgard 規則。

    rule 例如 1-3s、2-2s。日期為 YYYY-MM-DD（實驗室當地時間），date_to 含當日。畫 L-J 趨勢請限定單一儀器與批號濃度並用 order=asc。
    """
    return queries.get_qc_results(
        item, specimen, instrument, section, qc_product, qc_lot, level, status, rule, date_from, date_to, order, limit
    )


@tool
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
    limit: int = 100,
) -> dict[str, Any]:
    """品管統計彙總：各儀器 × 項目 × 檢體 × 批號 × 濃度的筆數、PASS/WARNING/FAIL 次數、mean、SD、CV%、各 Westgard 規則次數。

    回答「上週失控幾次」「CV 多少」。結果不會跨主備機合併。
    """
    return queries.get_qc_summary(item, specimen, instrument, section, qc_product, qc_lot, level, date_from, date_to, limit)


@tool
def get_qc_targets(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    qc_lot: str | None = None,
    level: str | None = None,
    as_of: str | None = None,
    include_history: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """查品管目標值 mean/SD、允收範圍與來源（原廠或實驗室）。預設只回傳目前生效版本。

    as_of 查某時間點的生效版本；include_history=true 回傳所有版本。
    """
    return queries.get_qc_targets(item, specimen, instrument, qc_lot, level, as_of, include_history, limit)


@tool
def get_loaded_reagents(
    instrument: str | None = None,
    product: str | None = None,
    lot_number: str | None = None,
    current_only: bool = True,
    at_time: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """查試劑裝機歷程：哪一批試劑何時裝在哪台儀器的哪個位置。

    預設只看目前仍在機上者；current_only=false 看完整歷程；at_time 查某時間點的機上試劑。
    """
    return queries.get_loaded_reagents(instrument, product, lot_number, current_only, at_time, limit)


@tool
def trace_reagent_lot(lot_number: str, product: str | None = None, limit: int = 100) -> dict[str, Any]:
    """追溯試劑批號的影響範圍：所屬產品、上一批、裝機紀錄、用過它的品管結果與校正事件。

    批號可能跨多個產品，必要時用 product 限定。
    """
    return queries.trace_reagent_lot(lot_number, product, limit)


@tool
def trace_qc_result(qc_result_id: str) -> dict[str, Any]:
    """稽核單筆品管結果：當時目標值版本、實際試劑批號、當時機上試劑、前一次校正，以及同儀器的連續點。"""
    return queries.trace_qc_result(qc_result_id)


@tool
def get_calibration_history(
    item: str | None = None,
    specimen: str | None = None,
    instrument: str | None = None,
    calibrator_product: str | None = None,
    calibrator_lot: str | None = None,
    event_status: Literal["PASS", "FAIL", "CANCELLED"] | None = None,
    assay_status: Literal["PASS", "FAIL", "NOT_EVALUATED"] | None = None,
    reason: Literal["SCHEDULED", "NEW_LOT", "QC_FAILURE", "MAINTENANCE", "OTHER"] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """查校正歷史：校正時間、原因、校正液批號、事件與各項目結果、校正前後偏差。依時間由新到舊。"""
    return queries.get_calibration_history(
        item, specimen, instrument, calibrator_product, calibrator_lot,
        event_status, assay_status, reason, date_from, date_to, limit,
    )


@tool
def run_readonly_query(sql: str, limit: int = 200) -> dict[str, Any]:
    """執行自訂 SELECT（MariaDB 語法），用於專用工具無法回答的問題。

    只允許單一 SELECT，且只能查 PJQAQC 的資料表與 View；禁止寫入、跨資料庫、@ 變數與 LOAD_FILE、SLEEP 等函式。
    回傳實際執行的 SQL 與結果，超過 limit 會截斷。
    """
    return queries.run_readonly_query(sql, limit)


@mcp.resource("pjqaqc://schema/overview", mime_type="application/json", title="資料庫架構總覽")
def schema_overview_resource() -> str:
    return json.dumps(catalog.overview(), ensure_ascii=False)


@mcp.resource("pjqaqc://schema/tables/{table}", mime_type="application/json", title="資料表定義")
def schema_table_resource(table: str) -> str:
    return json.dumps(catalog.describe(table), ensure_ascii=False)
