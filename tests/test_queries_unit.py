"""Query-building and result-shaping logic, with the database replaced by canned rows."""
from datetime import datetime, timedelta

import pytest

from mcp_pjqaqc import db, queries


class FakeDb:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, sql, params=None, max_rows=None):
        self.calls.append((sql, params))
        return self.responses.pop(0) if self.responses else []


@pytest.fixture
def fake(monkeypatch):
    def install(*responses):
        stub = FakeDb(*responses)
        monkeypatch.setattr(db, "fetch_all", stub)
        return stub

    return install


def test_like_escapes_wildcards():
    assert queries._like("BASO%") == r"%BASO\%%"
    assert queries._like("a_b") == r"%a\_b%"


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", ["1", "Level 1"]), ("Level 1", ["1", "Level 1"]), ("L2", ["2", "L2", "Level 2"])],
)
def test_level_matches_both_spellings(value, expected):
    w = queries.Where()
    queries._filter_level(w, value, "qc_level")
    assert w.params == expected


def test_date_only_range_covers_the_whole_last_day():
    w = queries.Where()
    queries._filter_time(w, "run_at", "2026-11-01", "2026-11-05")
    assert "run_at < %s" in w.clauses
    assert "run_at >= %s" in w.clauses
    assert w.params == [datetime(2026, 11, 6), datetime(2026, 11, 1)]


def test_exact_datetime_range_is_inclusive():
    w = queries.Where()
    queries._filter_time(w, "run_at", None, "2026-11-05T08:30:00")
    assert w.clauses == ["run_at <= %s"]


def test_bad_dates_are_explained():
    with pytest.raises(ValueError, match="date_from"):
        queries._filter_time(queries.Where(), "run_at", "上週", None)
    with pytest.raises(ValueError, match="不可晚於"):
        queries._filter_time(queries.Where(), "run_at", "2026-11-05", "2026-11-01")


def test_empty_table_is_not_reported_as_no_problem(fake):
    stub = fake([], [])
    result = queries.get_qc_results(item="WBC", status="FAIL")
    assert "完全沒有資料" in result["note"]
    assert "不能解讀為結果正常" in result["note"]
    assert "qc_results" in stub.calls[1][0]


def test_no_match_in_populated_table_points_at_search(fake):
    fake([], [{"present": 1}])
    assert "search_entities" in queries.get_qc_results(item="WBC")["note"]


def test_results_are_truncated_at_the_limit(fake):
    fake([{"run_at": str(i)} for i in range(3)])
    result = queries.get_qc_results(limit=2)
    assert result["row_count"] == 2
    assert result["truncated"] is True


def test_qc_filters_are_parameterized(fake):
    stub = fake([{"x": 1}])
    hostile = "x' OR '1'='1"
    queries.get_qc_results(item=hostile, instrument=hostile, qc_lot=hostile, rule=hostile)
    sql, params = stub.calls[0]
    assert hostile not in sql
    assert params.count(hostile) >= 4


def test_item_aliases_are_resolved(fake):
    stub = fake([{"x": 1}])
    queries.find_assays(item="血糖", specimen="尿液")
    assert stub.calls[0][1][:4] == ["GLU", "GLU", "U", "U"]


def _lot(days_from_today, status="AVAILABLE"):
    expiry = queries.lab_today() + timedelta(days=days_from_today)
    return {
        "lot_id": "1", "product_code": "A", "product_name": "A", "manufacturer": None, "lot_number": "L",
        "level_code": None, "lot_status": status, "received_at": None, "opened_at": None,
        "expiry_date": expiry.isoformat(), "parallel_test_done": False, "parallel_test_date": None, "note": None,
    }


def test_expiry_is_judged_by_date_not_status(fake):
    fake([_lot(-3)])
    result = queries.find_lots(kind="reagent")
    row = result["rows"][0]
    assert row["expired_by_date"] is True
    assert row["days_to_expiry"] == -3
    assert "lot_status" in result["warning"]


def test_future_expiry_raises_no_warning(fake):
    fake([_lot(30)])
    result = queries.find_lots(kind="reagent")
    assert result["rows"][0]["expired_by_date"] is False
    assert "warning" not in result


def test_missing_expiry_stays_unknown(fake):
    lot = _lot(1)
    lot["expiry_date"] = None
    fake([lot])
    row = queries.find_lots(kind="reagent")["rows"][0]
    assert row["expired_by_date"] is None
    assert row["days_to_expiry"] is None


def test_negative_expiry_window_is_rejected():
    with pytest.raises(ValueError):
        queries.find_lots(expiring_within_days=-1)


def test_materials_are_queried_separately_to_avoid_row_explosion(fake):
    assay = {"assay_id": "a1", "item_code": "GLU"}
    stub = fake(
        [assay],
        [{"assay_id": "a1", "product_code": "R1"}, {"assay_id": "a1", "product_code": "R2"}],
        [{"assay_id": "a1", "product_code": "Q1"}],
        [],
    )
    row = queries.find_assays(item="GLU", include_materials=True)["rows"][0]
    assert [m["product_code"] for m in row["reagents"]] == ["R1", "R2"]
    assert [m["product_code"] for m in row["qc_products"]] == ["Q1"]
    assert row["calibrators"] == []
    joined = [sql for sql, _ in stub.calls]
    assert sum("assay_reagent_requirements" in s and "assay_qc_requirements" in s for s in joined) == 0


def test_summary_adds_cv_and_rule_counts_per_group(fake):
    group = {"assay_id": "a1", "qc_lot_number": "L1", "qc_level": "1", "mean_value": 100.0, "sd_value": 2.5, "n": 10}
    other = {"assay_id": "a2", "qc_lot_number": "L1", "qc_level": "1", "mean_value": 50.0, "sd_value": None, "n": 1}
    fake(
        [group, other],
        [
            {"assay_id": "a1", "qc_lot_number": "L1", "qc_level": "1", "rule_name": "1-3s", "hits": 2},
            {"assay_id": "a1", "qc_lot_number": "L1", "qc_level": "1", "rule_name": "2-2s", "hits": 1},
        ],
    )
    rows = queries.get_qc_summary(item="GLU")["rows"]
    assert rows[0]["cv_percent"] == 2.5
    assert rows[0]["rule_counts"] == {"1-3s": 2, "2-2s": 1}
    assert rows[1]["cv_percent"] is None
    assert rows[1]["rule_counts"] == {}


def test_summary_groups_by_instrument(fake):
    stub = fake([], [], [])
    queries.get_qc_summary(item="GLU")
    assert "assay_id" in stub.calls[0][0].split("GROUP BY")[1]


def test_lot_shared_by_products_is_flagged(fake):
    a, b = _lot(10), _lot(10)
    a.update(lot_id="1", product_code="A")
    b.update(lot_id="2", product_code="B")
    fake([a, b], [], [], [], [], [{"present": 1}])
    result = queries.trace_reagent_lot("L")
    assert len(result["lots"]) == 2
    assert "多個試劑產品" in result["note"]


def test_unknown_lot_is_explained(fake):
    fake([])
    assert queries.trace_reagent_lot("NOPE")["lots"] == []


def test_unknown_result_is_an_error(fake):
    fake([], [])
    with pytest.raises(ValueError, match="找不到"):
        queries.trace_qc_result("nope")


def test_trace_result_passes_a_space_separated_timestamp(fake):
    row = {
        "qc_result_id": "r1", "qc_run_id": "run1", "run_at": "2026-11-05T08:30:00", "assay_id": "a1",
        "instrument_id": "i1", "qc_lot_number": "L1", "qc_level": "1",
    }
    stub = fake([row])
    result = queries.trace_qc_result("r1")
    assert result["result"]["qc_result_id"] == "r1"
    assert result["target_version"] is None
    assert result["last_calibration_before_run"] is None
    assert any("2026-11-05 08:30:00" in (params or []) for _, params in stub.calls)


def test_limit_is_clamped():
    assert queries._clamp(0) == 1
    assert queries._clamp(10**6) == queries.MAX_LIMIT


def test_ad_hoc_sql_runs_the_checked_statement(fake):
    stub = fake([{"n": 1}])
    result = queries.run_readonly_query("SELECT COUNT(*) AS n FROM qc_runs /*! INTO OUTFILE 'x' */")
    assert "OUTFILE" not in stub.calls[0][0]
    assert stub.calls[0][1] is None
    assert result["executed_sql"] == stub.calls[0][0]


def test_ad_hoc_writes_never_reach_the_database(fake):
    stub = fake()
    with pytest.raises(ValueError):
        queries.run_readonly_query("DELETE FROM qc_results")
    assert stub.calls == []
