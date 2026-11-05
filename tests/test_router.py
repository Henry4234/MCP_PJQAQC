import pytest

from mcp_pjqaqc import queries, router

DICTIONARY = {
    "section": [
        {"section_code": "BIO", "section_name": "生化免疫組"},
        {"section_code": "HEM", "section_name": "血液組"},
        {"section_code": "URN", "section_name": "鏡檢組"},
        {"section_code": "BB", "section_name": "血庫組"},
    ],
    "specimen": [
        {"specimen_code": "B", "specimen_name": "BLOOD"},
        {"specimen_code": "U", "specimen_name": "URINE"},
        {"specimen_code": "CSF", "specimen_name": "CSF"},
        {"specimen_code": "BF", "specimen_name": "BODY FLUID"},
        {"specimen_code": "ST", "specimen_name": "STOOL"},
    ],
    "item": [
        {"item_code": "K", "item_name": "Potassium"},
        {"item_code": "CA", "item_name": "Calcium"},
        {"item_code": "MG", "item_name": "Magnesium"},
        {"item_code": "Na", "item_name": "Sodium (Urine)"},
        {"item_code": "GLU", "item_name": "Glucose"},
        {"item_code": "WBC", "item_name": "White Blood Cell Count"},
        {"item_code": "CRE", "item_name": "Creatinine (Urine)"},
        {"item_code": "CA 19-9", "item_name": "CA 19-9"},
        {"item_code": "GLU-CSF", "item_name": "Glucose (CSF)"},
    ],
    "instrument": [
        {"serial_number": "43056", "instrument_name": "Sysmex XN-3000-A", "model_name": "Sysmex XN-3000", "instrument_role": "主機"},
        {"serial_number": "43052", "instrument_name": "Sysmex XN-3000-B", "model_name": "Sysmex XN-3000", "instrument_role": "備機"},
        {"serial_number": "33110", "instrument_name": "Roche Cobas c503-Line1", "model_name": "Roche Cobas c503", "instrument_role": "主機"},
    ],
    "reagent_product": [{"product_code": "THROMBOREL-S", "product_name": "Thromborel S"}],
    "qc_product": [{"product_code": "CI-TROL", "product_name": "Ci-Trol"}],
    "calibrator_product": [],
}


def entities(question):
    return router.match_entities(question, DICTIONARY)[0]


def codes(found, kind, key):
    return [row[key] for row in found.get(kind, [])]


def test_longest_term_wins_for_sections():
    found = entities("血液組有哪些儀器")
    assert codes(found, "section", "section_code") == ["HEM"]
    assert "specimen" not in found


def test_longest_term_wins_for_item_codes():
    assert codes(entities("CA 19-9 的目標值"), "item", "item_code") == ["CA 19-9"]


def test_model_name_matches_every_unit():
    found = entities("XN-3000 的 WBC")
    assert sorted(codes(found, "instrument", "serial_number")) == ["43052", "43056"]


def test_full_instrument_name_matches_one_unit():
    found = entities("XN-3000-A 的 WBC")
    assert codes(found, "instrument", "serial_number") == ["43056"]


def test_short_codes_ignore_units_and_english_words():
    found = entities("GLU 的結果是 120 mg/dL 嗎？okay")
    assert codes(found, "item", "item_code") == ["GLU"]


def test_short_codes_accept_uppercase_form():
    assert codes(entities("NA 的目標值"), "item", "item_code") == ["Na"]
    assert codes(entities("Na 的目標值"), "item", "item_code") == ["Na"]


def test_specimen_word_in_item_name_is_not_an_item():
    found = entities("urine 的項目")
    assert codes(found, "specimen", "specimen_code") == ["U"]
    assert "item" not in found


def test_chinese_aliases_resolve_to_codes():
    found = entities("尿液的鈉和血糖")
    assert codes(found, "specimen", "specimen_code") == ["U"]
    assert sorted(codes(found, "item", "item_code")) == ["GLU", "Na"]


def test_masked_text_leaves_only_unknown_tokens():
    assert "11223" in router.match_entities("XN-3000 批號 11223", DICTIONARY)[1]
    assert "XN-3000" not in router.match_entities("XN-3000 批號 11223", DICTIONARY)[1]


@pytest.fixture
def offline(monkeypatch):
    monkeypatch.setattr(router, "_load_dictionary", lambda: DICTIONARY)
    monkeypatch.setattr(queries, "table_row_counts", lambda tables: {t: 5 for t in tables})
    monkeypatch.setattr(
        router,
        "_match_lots",
        lambda text: [{"kind": "reagent", "lot_number": "11223"}] if "11223" in text else [],
    )


def route(question):
    return router.route_question(question)


@pytest.mark.parametrize(
    ("question", "tool", "arguments"),
    [
        ("XN-3000 的 WBC 上週有幾次 FAIL？", "get_qc_summary", {"item": "WBC", "instrument": "XN-3000"}),
        ("c503 的 GLU 有FAIL嗎", "get_qc_results", {"item": "GLU", "instrument": "33110", "status": "FAIL"}),
        ("WBC 違反 1-3s 的品管結果", "get_qc_results", {"item": "WBC", "rule": "1-3s"}),
        ("哪些試劑快過期？", "find_lots", {"kind": "reagent", "expiring_within_days": 30}),
        ("14 天內到期的品管液", "find_lots", {"kind": "qc", "expiring_within_days": 14}),
        ("GLU 需要用哪些試劑和品管液", "find_assays", {"item": "GLU", "include_materials": True}),
        ("Ci-Trol 用在哪些項目", "get_product_usage", {"product": "CI-TROL", "kind": None}),
        ("批號 11223 影響哪些品管結果", "trace_reagent_lot", {"lot_number": "11223"}),
        ("c503 目前機上裝了哪批試劑", "get_loaded_reagents", {"instrument": "33110"}),
        ("WBC 上次校正是什麼時候？", "get_calibration_history", {"item": "WBC"}),
        ("尿液的鈉目標值是多少 level 2", "get_qc_targets", {"item": "Na", "specimen": "U", "level": "2"}),
        ("血液組有哪些儀器", "find_assays", {"section": "HEM"}),
        ("qc_results 跟 reagent_products 怎麼 join", "get_schema_overview", {}),
        ("為什麼這筆品管結果 FAIL", "trace_qc_result", {}),
    ],
)
def test_question_routes_to_the_right_tool(offline, question, tool, arguments):
    top = route(question)["recommended"][0]
    assert top["tool"] == tool
    for key, value in arguments.items():
        assert top["suggested_arguments"].get(key) == value


def test_multiple_instruments_get_a_warning(offline):
    notes = " ".join(route("XN-3000 的 WBC 品管結果")["notes"])
    assert "主機與備機" in notes


def test_empty_tables_are_called_out(offline, monkeypatch):
    monkeypatch.setattr(queries, "table_row_counts", lambda tables: {t: 0 for t in tables})
    notes = " ".join(route("WBC 的品管結果")["notes"])
    assert "尚未登錄" in notes


def test_unmatched_question_offers_a_next_step(offline):
    result = route("今天天氣好嗎")
    assert result["recommended"] == []
    assert "search_entities" in " ".join(result["notes"])


def test_works_without_a_database(monkeypatch):
    from mcp_pjqaqc.config import ConfigError

    def unavailable():
        raise ConfigError("缺少資料庫設定")

    monkeypatch.setattr(router, "_load_dictionary", unavailable)
    result = route("哪些試劑快過期")
    assert result["recommended"][0]["tool"] == "find_lots"
    assert "無法連線" in result["notes"][0]


def test_blank_question_is_rejected():
    with pytest.raises(ValueError):
        route("   ")
