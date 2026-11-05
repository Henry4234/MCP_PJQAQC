"""Run the real SQL against the local test database. Read-only: nothing here changes any row."""
import pytest

from mcp_pjqaqc import catalog, db, queries, router, snapshot
from mcp_pjqaqc.config import ConfigError, load_config

pytestmark = pytest.mark.database


@pytest.fixture(scope="module", autouse=True)
def require_database():
    try:
        load_config()
    except ConfigError as exc:
        pytest.skip(str(exc))


def one(sql, params=None):
    rows = db.fetch_all(sql, params)
    return rows[0] if rows else None


def test_session_rejects_writes():
    # WHERE 1 = 0 keeps this harmless even if the read-only protection were ever broken.
    with pytest.raises(db.DatabaseError, match="1792|READ ONLY"):
        db.fetch_all("UPDATE lab_sections SET section_name = section_name WHERE 1 = 0")


def test_schema_snapshot_matches_the_database():
    assert snapshot.diff_schema(snapshot.load_snapshot(), snapshot.read_live_schema())["in_sync"] is True


def test_check_connection_reports_state_without_secrets():
    info = queries.check_connection()
    assert info["session_read_only"] is True
    assert info["schema_drift"]["in_sync"] is True
    assert "qc_results" in info["row_counts"]
    assert not any("IDENTIFIED" in grant.upper() for grant in info["grants"])


def test_every_join_path_is_valid_sql():
    bases = [t for t in catalog.table_names() if not t.startswith("v_")]
    pairs = [(a, b) for i, a in enumerate(bases) for b in bases[i + 1:]]
    for start, goal in pairs:
        for path in catalog.join_paths(start, goal)["paths"]:
            db.fetch_all(f"SELECT 1 {path['sql']} LIMIT 0")


def test_mariadb_json_functions_used_by_the_queries():
    row = one(
        "SELECT JSON_CONTAINS('[\"1-3s\",\"2-2s\"]', JSON_QUOTE(%s)) AS hit, "
        "JSON_CONTAINS('[\"1-3s\"]', JSON_QUOTE(%s)) AS miss",
        ["2-2s", "10x"],
    )
    assert row == {"hit": 1, "miss": 0}
    rows = db.fetch_all(
        "SELECT j.rule_name FROM JSON_TABLE('[\"1-3s\",\"2-2s\"]', '$[*]' "
        "COLUMNS (rule_name VARCHAR(20) PATH '$')) j"
    )
    assert [r["rule_name"] for r in rows] == ["1-3s", "2-2s"]


def test_find_assays_with_materials_and_lots():
    assay = one("SELECT a.assay_id, t.item_code FROM assay_qc_requirements r "
                "JOIN instrument_assays a ON a.assay_id = r.assay_id "
                "JOIN test_items t ON t.test_item_id = a.test_item_id LIMIT 1")
    if not assay:
        pytest.skip("no requirement rows")
    result = queries.find_assays(item=assay["item_code"], include_materials=True, include_lots=True)
    assert result["row_count"] >= 1
    row = next(r for r in result["rows"] if r["assay_id"] == assay["assay_id"])
    assert row["qc_products"]
    assert isinstance(row["active"], bool)
    assert isinstance(row["qc_products"][0]["required"], bool)
    assert "usable_lots" in result


def test_codes_with_special_characters_match_exactly():
    item = one("SELECT item_code FROM test_items WHERE item_code LIKE %s LIMIT 1", ["%\\%%"])
    if item:
        rows = queries.find_assays(item=item["item_code"], active_only=False)["rows"]
        assert {r["item_code"] for r in rows} == {item["item_code"]}


def test_matching_ignores_case():
    item = one("SELECT item_code FROM test_items WHERE CHAR_LENGTH(item_code) > 2 LIMIT 1")
    upper = queries.find_assays(item=item["item_code"].upper(), active_only=False)["row_count"]
    lower = queries.find_assays(item=item["item_code"].lower(), active_only=False)["row_count"]
    assert upper == lower


def test_instrument_filter_accepts_model_fragment():
    model = one("SELECT model_name, COUNT(*) AS n FROM instruments GROUP BY model_name HAVING n > 1 LIMIT 1")
    if not model:
        pytest.skip("no model with more than one unit")
    fragment = model["model_name"].split()[-1]
    rows = queries.find_assays(instrument=fragment, active_only=False, limit=1000)["rows"]
    assert len({r["serial_number"] for r in rows}) == model["n"]


def test_search_entities_across_types():
    item = one("SELECT item_code FROM test_items LIMIT 1")
    assert "item" in queries.search_entities(item["item_code"])["matches"]
    assert queries.search_entities("no-such-thing-xyz")["matches"] == {}


def test_search_entities_rejects_unknown_type():
    with pytest.raises(ValueError):
        queries.search_entities("x", ["nope"])


@pytest.mark.parametrize("kind", ["reagent", "qc", "calibrator", "all"])
def test_find_lots_every_kind(kind):
    result = queries.find_lots(kind=kind)
    assert "rows" in result
    for row in result["rows"]:
        assert row["kind"] in queries.LOT_KINDS
        assert "expired_by_date" in row


def test_find_lots_with_every_filter():
    queries.find_lots(
        product="x", lot_number="x", status="AVAILABLE", level="1",
        expiring_within_days=30, parallel_test_done=False,
    )


def test_level_spellings_find_the_same_lots():
    level = one("SELECT level_code FROM qc_lots WHERE level_code LIKE 'Level %' LIMIT 1")
    if level:
        digit = level["level_code"].split()[-1]
        by_word = {r["lot_id"] for r in queries.find_lots(kind="qc", level=level["level_code"])["rows"]}
        by_digit = {r["lot_id"] for r in queries.find_lots(kind="qc", level=digit)["rows"]}
        assert by_word == by_digit


def test_product_usage():
    product = one("SELECT p.product_code FROM qc_products p "
                  "JOIN assay_qc_requirements r ON r.qc_product_id = p.qc_product_id LIMIT 1")
    if not product:
        pytest.skip("no QC product in use")
    usage = queries.get_product_usage(product["product_code"], kind="qc")["usage"]
    assert usage["qc"][0]["product_code"] == product["product_code"]


def test_qc_targets():
    result = queries.get_qc_targets(include_history=True)
    assert "rows" in result
    for row in result["rows"]:
        assert row["target_source"] in {"MANUFACTURER", "LAB", "PEER_GROUP"}
    queries.get_qc_targets(item="GLU", specimen="B", instrument="x", qc_lot="x", level="1", as_of="2026-01-01")


def test_qc_results_with_every_filter():
    result = queries.get_qc_results(
        item="GLU", specimen="B", instrument="x", section="BIO", qc_product="x", qc_lot="x", level="1",
        status="FAIL", rule="1-3s", date_from="2026-01-01", date_to="2026-12-31", order="asc",
    )
    assert "rows" in result


def test_qc_summary_with_every_filter():
    result = queries.get_qc_summary(
        item="GLU", specimen="B", instrument="x", section="BIO", qc_product="x", qc_lot="x", level="1",
        date_from="2026-01-01", date_to="2026-12-31",
    )
    assert "statistics_note" in result


def test_qc_queries_tell_empty_table_from_no_match():
    if one("SELECT 1 AS present FROM qc_results LIMIT 1") is None:
        assert "完全沒有資料" in queries.get_qc_results()["note"]


def test_loaded_reagents_every_mode():
    queries.get_loaded_reagents()
    queries.get_loaded_reagents(current_only=False)
    queries.get_loaded_reagents(instrument="x", product="x", lot_number="x", at_time="2026-01-01T08:00:00")


def test_trace_reagent_lot():
    lot = one("SELECT lot_number FROM reagent_lots GROUP BY lot_number ORDER BY COUNT(*) DESC LIMIT 1")
    if not lot:
        pytest.skip("no reagent lots")
    result = queries.trace_reagent_lot(lot["lot_number"])
    assert result["lots"]
    for key in ("loads", "qc_usage", "calibration_usage"):
        assert key in result
    assert "previous_lot_number" in result["lots"][0]


def test_trace_qc_result_on_real_schema():
    row = one("SELECT qc_result_id FROM qc_results LIMIT 1")
    if row:
        assert queries.trace_qc_result(row["qc_result_id"])["result"]["qc_result_id"] == row["qc_result_id"]
    else:
        with pytest.raises(ValueError, match="找不到"):
            queries.trace_qc_result("00000000-0000-0000-0000-000000000000")


def test_calibration_history_with_every_filter():
    result = queries.get_calibration_history(
        item="GLU", specimen="B", instrument="x", calibrator_product="x", calibrator_lot="x",
        event_status="PASS", assay_status="PASS", reason="SCHEDULED",
        date_from="2026-01-01", date_to="2026-12-31",
    )
    assert "rows" in result


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM v_assay_catalog WHERE item_code LIKE '%GLU%'",
        "SELECT section_code, COUNT(*) AS n FROM v_assay_catalog GROUP BY section_code ORDER BY n DESC",
        "WITH q AS (SELECT item_code FROM test_items) SELECT COUNT(*) AS n FROM q",
        "SELECT DATEDIFF(expiry_date, CURDATE()) AS d, DATE_FORMAT(expiry_date, '%Y-%m') AS m FROM reagent_lots",
        "SELECT ROW_NUMBER() OVER (ORDER BY item_code) AS rn, item_code FROM test_items",
        "SELECT JSON_CONTAINS(westgard_flags, '\"1-3s\"') AS hit FROM qc_results",
        "SELECT STDDEV_SAMP(result_value) AS sd, AVG(z_score) AS z FROM qc_results",
        "SELECT j.r FROM qc_results q JOIN JSON_TABLE(q.westgard_flags, '$[*]' COLUMNS (r VARCHAR(20) PATH '$')) j",
        "SELECT i.serial_number, a.item_code FROM instruments i "
        "LEFT JOIN v_assay_catalog a ON a.instrument_id = i.instrument_id WHERE i.active = 1",
        "SELECT '血液組' AS name, section_code FROM lab_sections WHERE section_name = '血液組'",
    ],
)
def test_checked_sql_stays_valid_on_the_server(sql):
    result = queries.run_readonly_query(sql, limit=5)
    assert result["row_count"] <= 5


def test_ad_hoc_query_reports_truncation():
    assert queries.run_readonly_query("SELECT item_code FROM test_items", limit=2)["truncated"] is True


def test_route_question_end_to_end():
    instrument = one("SELECT model_name FROM instruments WHERE model_name IS NOT NULL LIMIT 1")
    fragment = instrument["model_name"].split()[-1]
    result = router.route_question(f"{fragment} 的品管結果有失控嗎")
    assert result["matched_entities"]["instrument"]
    assert result["recommended"][0]["tool"] == "get_qc_results"
    assert "qc_results" in result["table_row_counts"]


def test_route_question_finds_lot_numbers():
    lot = one("SELECT lot_number FROM reagent_lots LIMIT 1")
    if not lot:
        pytest.skip("no reagent lots")
    result = router.route_question(f"批號 {lot['lot_number']} 影響哪些品管結果")
    assert any(m["lot_number"] == lot["lot_number"] for m in result["matched_lots"])
    top = result["recommended"][0]
    assert top["tool"] == "trace_reagent_lot"
    assert top["suggested_arguments"]["lot_number"] == lot["lot_number"]
