"""Domain meaning that information_schema cannot provide: layers, purpose, rules and synonyms."""
from __future__ import annotations

from typing import Any

CORE_PRINCIPLE = (
    "模型的中心不是檢驗項目，而是 assay（instrument_assays）：assay = 儀器 × 檢驗項目 × 檢體別。"
    "GLU/血液與 GLU/尿液是兩筆 assay；GLU/血液在主機與備機上也是兩筆 assay。"
    "先定義 assay，再連到可共用的試劑、品管液與校正液產品，最後才是實際批號與結果。"
)

LAYERS: dict[str, dict[str, str]] = {
    "A": {"name": "主檔／框架設定層", "answers": "系統允許什麼"},
    "B": {"name": "批號／庫存實體層", "answers": "現場現在拿到哪一批（架構圖中併入 B 區）"},
    "C": {"name": "交易／稽核層", "answers": "實際發生了什麼（架構圖中併入 B 區）"},
    "V": {"name": "讀取用 View", "answers": "已攤平的常用查詢，優先使用"},
}

RULES: list[str] = [
    "品管判讀一律綁儀器：Westgard 連續規則（2-2s、4-1s、10x）必須分機器各自成序列，主機與備機不可混在一起判讀或統計。",
    "產品與批號分離：*_products 是穩定定義，*_lots 是實體批號；換批只新增 lot，不新增 product。",
    "不要一次展開試劑、品管液、校正液三組 N:M，列數會相乘；請分開查詢（find_assays 的 include_materials 已如此處理）。",
    "歷史判讀以 qc_results 的 target_mean_snapshot / target_sd_snapshot / unit_snapshot 為準，不要用目前的 qc_target_values 回推。",
    "qc_target_values 為版本化資料：effective_to IS NULL 表示目前生效；MANUFACTURER 為平測前原廠值，LAB 為平測後實驗室值。",
    "lot_status 不會隨日期自動更新，判斷是否過期必須比對 expiry_date；狀態為 AVAILABLE 的批號可能已過效期。",
    "批號不是全域唯一：同一 lot_number 可屬於多個產品，查批號時要帶產品一起判讀。",
    "所有 DATETIME 都是實驗室當地時間（Asia/Taipei），不含時區。範圍查詢請用 >= 與 <，不要對 run_at 套 DATE() 等函式，以免索引失效。",
    "westgard_flags 是 JSON 陣列字串，例如 [\"1-3s\",\"2-2s\"]；SQL 請用 JSON_CONTAINS(westgard_flags, '\"1-3s\"')。",
    "空資料不等於正常：若 qc_results 等交易表整張為空，代表尚未登錄資料，不能回答成「沒有失控」。",
    "資料現況：test_items.item_name 部分帶有 (Urine) 字樣但同時用於血液與尿液，檢體請以 specimen_types 為準；GLU-CSF、TP-CSF 仍以項目代碼區分檢體。",
    "資料現況：qc_lots.level_code 寫法不一致（'Level 1' 與 '1' 並存），比對濃度時兩者都要考慮。",
]

WESTGARD: list[dict[str, str]] = [
    {"rule": "1-2s", "meaning": "單點超過 ±2SD", "outcome": "WARNING（警告，非拒收）"},
    {"rule": "1-3s", "meaning": "單點超過 ±3SD", "outcome": "FAIL"},
    {"rule": "2-2s", "meaning": "連續 2 點同方向超過 ±2SD", "outcome": "FAIL"},
    {"rule": "R-4s", "meaning": "兩點差距超過 4SD", "outcome": "FAIL"},
    {"rule": "4-1s", "meaning": "連續 4 點同方向超過 ±1SD", "outcome": "FAIL"},
    {"rule": "10x", "meaning": "連續 10 點落在平均值同側", "outcome": "FAIL"},
]

# Names used in the architecture diagram that differ from the real table names.
TABLE_ALIASES: dict[str, str] = {"event_assays": "calibration_event_assays"}

VIEW_COVERAGE: dict[str, set[str]] = {
    "v_assay_catalog": {"instrument_assays", "instruments", "lab_sections", "test_items", "specimen_types"},
    "v_qc_result_detail": {
        "qc_results",
        "qc_runs",
        "qc_lots",
        "qc_products",
        "instrument_assays",
        "instruments",
        "lab_sections",
        "test_items",
        "specimen_types",
    },
}

_PRODUCT_COLUMNS = {
    "product_code": "產品代碼，唯一",
    "product_name": "產品名稱",
    "catalog_number": "目錄編號",
}
_LOT_COLUMNS = {
    "lot_number": "批號（同產品內唯一，不同產品可重複）",
    "received_at": "收貨日",
    "opened_at": "開封時間",
    "expiry_date": "效期；NULL 表示未登錄",
    "lot_status": "批號狀態，不會隨日期自動更新",
    "certificate_uri": "證明文件位置",
}
_PARALLEL_COLUMNS = {
    "parallel_test_done": "入庫後是否已完成平測（平行測試）",
    "parallel_test_date": "平測日期",
}
_REQUIREMENT_COLUMNS = {
    "required": "是否為必要",
    "priority": "優先序，數字小者優先",
}

TABLES: dict[str, dict[str, Any]] = {
    "lab_sections": {
        "layer": "A",
        "zh": "科別／組別",
        "purpose": "檢驗科的工作組別，例如生化免疫組、血液組。",
        "columns": {"section_code": "組別代碼", "section_name": "組別名稱"},
    },
    "instruments": {
        "layer": "A",
        "zh": "儀器",
        "purpose": "儀器實體，以序號唯一識別；主機與備機分開記錄與追蹤。",
        "columns": {
            "serial_number": "儀器序號，全系統識別鍵",
            "model_name": "機型；主備機通常同機型",
            "instrument_role": "主機／備機／其他",
            "backup_of_instrument_id": "備機指向其主機",
            "installed_at": "安裝日",
            "retired_at": "除役日",
        },
    },
    "specimen_types": {
        "layer": "A",
        "zh": "檢體別",
        "purpose": "檢體字典：B(BLOOD)、U(URINE)、CSF、BF(BODY FLUID)、ST(STOOL)。",
        "columns": {"specimen_code": "檢體代碼", "specimen_name": "檢體名稱"},
    },
    "test_items": {
        "layer": "A",
        "zh": "檢驗項目",
        "purpose": "全院共用的項目字典，不綁儀器。",
        "columns": {
            "item_code": "項目代碼，例如 GLU、Na、WBC",
            "item_name": "項目名稱",
            "loinc_code": "LOINC 代碼",
            "default_unit": "預設單位",
        },
    },
    "instrument_assays": {
        "layer": "A",
        "zh": "檢驗設定（中心 Hub）",
        "purpose": "儀器 × 項目 × 檢體的可執行設定，三者組合唯一；所有試劑、品管、校正關聯都掛在這裡。",
        "columns": {
            "instrument_test_code": "儀器端的項目代碼",
            "method_name": "方法學",
            "result_unit": "結果單位；留空時採 test_items.default_unit",
            "decimal_places": "結果小數位數",
            "valid_from": "生效起日",
            "valid_to": "生效迄日",
        },
    },
    "reagent_products": {
        "layer": "A",
        "zh": "試劑產品",
        "purpose": "穩定的試劑產品定義，不含批號、不綁儀器。",
        "columns": {**_PRODUCT_COLUMNS, "storage_condition": "保存條件"},
    },
    "qc_products": {
        "layer": "A",
        "zh": "品管液產品",
        "purpose": "品管液產品定義；同一多項品管液可管理多個項目。濃度層級屬於批號。",
        "columns": {**_PRODUCT_COLUMNS, "matrix_description": "基質描述", "storage_condition": "保存條件"},
    },
    "calibrator_products": {
        "layer": "A",
        "zh": "校正液產品",
        "purpose": "校正液產品定義；同一多項校正液可校正多個項目。",
        "columns": {**_PRODUCT_COLUMNS, "matrix_description": "基質描述", "storage_condition": "保存條件"},
    },
    "assay_reagent_requirements": {
        "layer": "A",
        "zh": "項目↔試劑（N:M）",
        "purpose": "assay 可使用哪些試劑產品及其角色。",
        "columns": {**_REQUIREMENT_COLUMNS, "reagent_role": "主試劑、輔助、稀釋液、清洗液等角色"},
    },
    "assay_qc_requirements": {
        "layer": "A",
        "zh": "項目↔品管液（N:M）",
        "purpose": "assay 可使用哪些品管液產品。",
        "columns": dict(_REQUIREMENT_COLUMNS),
    },
    "assay_calibrator_requirements": {
        "layer": "A",
        "zh": "項目↔校正液（N:M）",
        "purpose": "assay 可使用哪些校正液產品。",
        "columns": dict(_REQUIREMENT_COLUMNS),
    },
    "reagent_lots": {
        "layer": "B",
        "zh": "試劑批號",
        "purpose": "實體試劑批號、效期、狀態與平測紀錄。",
        "columns": {
            **_LOT_COLUMNS,
            **_PARALLEL_COLUMNS,
            "quantity_received": "收貨數量",
            "quantity_unit": "數量單位",
            "same_lot_as_previous": "是否與上一批同批號",
            "previous_reagent_lot_id": "上一批，供批號延續性追溯",
        },
    },
    "qc_lots": {
        "layer": "B",
        "zh": "品管液批號＋Level",
        "purpose": "同一 kit 的 Level 1/2/3 各一列；濃度屬於批號。",
        "columns": {**_LOT_COLUMNS, **_PARALLEL_COLUMNS, "level_code": "濃度層級，寫法不一致（'Level 1' 或 '1'）"},
    },
    "calibrator_lots": {
        "layer": "B",
        "zh": "校正液批號",
        "purpose": "校正液批號、Level、效期與原廠指定值。",
        "columns": {**_LOT_COLUMNS, "level_code": "濃度層級", "assigned_value_text": "原廠指定值"},
    },
    "qc_target_values": {
        "layer": "B",
        "zh": "品管目標值（版本化）",
        "purpose": "每個 assay × QC 批號的 mean/SD、允收區間與生效期間；同一品管液對不同項目或檢體可有不同目標值。",
        "columns": {
            "target_source": "MANUFACTURER 平測前原廠值、LAB 平測後實驗室值、PEER_GROUP 同儕值",
            "target_mean": "目標平均值",
            "target_sd": "目標標準差，必須大於 0",
            "acceptable_low": "允收下限",
            "acceptable_high": "允收上限",
            "effective_from": "生效起始",
            "effective_to": "生效結束；NULL 表示目前生效",
            "current_slot": "產生欄位，保證同一 assay + 批號只有一筆生效中版本",
            "approved_by": "核准者",
        },
    },
    "instrument_reagent_loads": {
        "layer": "B",
        "zh": "試劑裝機歷程",
        "purpose": "哪一批試劑何時裝到哪台儀器的哪個位置；同一位置同時只有一批在線上。",
        "columns": {
            "position_code": "儀器上的位置",
            "loaded_at": "上機時間",
            "unloaded_at": "下機時間；NULL 表示仍在機上",
            "current_slot": "產生欄位，保證同一位置只有一批在線上",
        },
    },
    "qc_runs": {
        "layer": "C",
        "zh": "品管執行",
        "purpose": "一次品管執行的表頭；一瓶多項品管液可產生多個項目結果。",
        "columns": {
            "run_at": "執行時間",
            "operator_id": "操作人員",
            "instrument_run_id": "儀器端的執行編號",
            "run_status": "COMPLETED／PARTIAL／CANCELLED",
        },
    },
    "qc_results": {
        "layer": "C",
        "zh": "品管結果",
        "purpose": "每個 assay 的量測值、mean/SD 快照、Z-score 與 Westgard 判讀。",
        "columns": {
            "result_value": "量測值",
            "unit_snapshot": "當時的單位",
            "target_mean_snapshot": "當時的目標平均值",
            "target_sd_snapshot": "當時的目標標準差",
            "z_score": "產生欄位：(量測值 − mean) / SD",
            "result_status": "PASS／WARNING／FAIL／INVALID",
            "westgard_flags": "違反的規則，JSON 陣列",
            "qc_target_id": "採用的目標值版本",
            "reviewed_by": "複核者",
            "reviewed_at": "複核時間",
        },
    },
    "qc_result_reagents": {
        "layer": "C",
        "zh": "品管結果實際使用的試劑批號",
        "purpose": "每筆品管結果實際用了哪些試劑批號，可為多筆。",
        "columns": {"reagent_role": "該批試劑的角色"},
    },
    "calibration_events": {
        "layer": "C",
        "zh": "校正事件",
        "purpose": "一次校正的表頭；一次校正可涵蓋多個 assay。",
        "columns": {
            "performed_at": "校正時間",
            "calibration_reason": "SCHEDULED／NEW_LOT／QC_FAILURE／MAINTENANCE／OTHER",
            "event_status": "PASS／FAIL／CANCELLED",
            "instrument_event_id": "儀器端的事件編號",
            "raw_data_uri": "原始資料位置",
        },
    },
    "calibration_event_assays": {
        "layer": "C",
        "zh": "校正事件涵蓋的項目",
        "purpose": "此次校正涵蓋哪些 assay 及各項結果。架構圖中的 event_assays。",
        "columns": {
            "assay_status": "PASS／FAIL／NOT_EVALUATED",
            "pre_bias": "校正前偏差",
            "post_bias": "校正後偏差",
        },
    },
    "calibration_event_reagents": {
        "layer": "C",
        "zh": "校正實際使用的試劑批號",
        "purpose": "每個校正項目實際使用的試劑批號。",
        "columns": {"reagent_role": "該批試劑的角色"},
    },
    "v_assay_catalog": {
        "layer": "V",
        "zh": "檢驗設定總覽",
        "purpose": "assay 攤平：組別、儀器、項目、檢體、單位一次查完。",
        "columns": {},
    },
    "v_qc_result_detail": {
        "layer": "V",
        "zh": "品管結果明細",
        "purpose": "品管結果攤平：含儀器、項目、檢體、品管液與批號、Z-score 與判讀。查品管資料優先使用。",
        "columns": {"qc_level": "品管濃度（qc_lots.level_code）", "qc_lot_number": "品管液批號"},
    },
}

# Everyday names for what the question router and tool filters should resolve to a code.
ITEM_ALIASES: dict[str, str] = {
    "血糖": "GLU", "葡萄糖": "GLU", "白血球": "WBC", "紅血球": "RBC", "血紅素": "HGB", "血色素": "HGB",
    "血比容": "HCT", "血小板": "PLT", "鈉": "Na", "鉀": "K", "氯": "CL", "鈣": "CA", "磷": "IP", "鎂": "MG",
    "鐵": "Fe", "鐵蛋白": "Ferritin", "肌酸酐": "CRE", "尿素氮": "BUN", "尿酸": "UA", "總蛋白": "TP",
    "白蛋白": "ALB", "膽固醇": "CHOL", "總膽固醇": "CHOL", "三酸甘油酯": "TG", "三酸甘油脂": "TG",
    "總膽紅素": "T-BI", "直接膽紅素": "D-BI", "澱粉酶": "AMY", "脂解酶": "LIP", "乳酸": "LAC",
    "乳酸脫氫酶": "LDH", "氨": "NH3", "糖化血色素": "HbA1c", "凝血酶原時間": "P.T", "PT": "P.T",
    "纖維蛋白原": "FIB", "甲胎蛋白": "AFP", "癌胚抗原": "CEA", "肌酸激酶": "CK", "肌鈣蛋白": "Troponin-T",
    "鹼性磷酸酶": "ALP", "GOT": "AST", "GPT": "ALT", "GGT": "γ-GT", "葉酸": "Folate", "維生素D": "Vitamin D Total",
    "維生素B12": "Vitamin B12", "甲狀腺刺激素": "TSH", "C反應蛋白": "CRP",
}

SPECIMEN_ALIASES: dict[str, str] = {
    "血液": "B", "全血": "B", "血清": "B", "血漿": "B", "blood": "B",
    "尿液": "U", "尿": "U", "urine": "U",
    "腦脊髓液": "CSF", "腦脊液": "CSF",
    "體液": "BF", "body fluid": "BF",
    "糞便": "ST", "stool": "ST",
}

SECTION_ALIASES: dict[str, str] = {"生化": "BIO", "免疫": "BIO", "血液組": "HEM", "鏡檢": "URN", "血庫": "BB"}


def normalize_item(value: str) -> str:
    return ITEM_ALIASES.get(value.strip(), value.strip())


def normalize_specimen(value: str) -> str:
    return SPECIMEN_ALIASES.get(value.strip().lower(), value.strip())
