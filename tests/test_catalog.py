import pytest

from mcp_pjqaqc import catalog, knowledge
from mcp_pjqaqc.snapshot import load_snapshot

SCHEMA = load_snapshot()["tables"]


def test_every_table_is_documented():
    assert set(knowledge.TABLES) == set(SCHEMA)


def test_every_documented_column_exists():
    for table, meta in knowledge.TABLES.items():
        real = {col["name"] for col in SCHEMA[table]["columns"]}
        assert set(meta["columns"]) <= real, f"{table}: {set(meta['columns']) - real}"


def test_view_coverage_names_real_tables():
    for view, tables in knowledge.VIEW_COVERAGE.items():
        assert view in SCHEMA
        assert tables <= set(SCHEMA)


def test_overview_covers_all_layers_and_tables():
    overview = catalog.overview()
    listed = {t["table"] for layer in overview["layers"] for t in layer["tables"]}
    assert listed == set(SCHEMA)
    assert "assay" in overview["core_principle"]


def test_describe_merges_structure_and_meaning():
    info = catalog.describe("qc_results")
    columns = {c["name"]: c for c in info["columns"]}
    assert info["zh"] == "品管結果"
    assert columns["result_status"]["allowed_values"] == ["PASS", "WARNING", "FAIL", "INVALID"]
    assert "generated" in columns["z_score"]
    assert columns["z_score"]["description"]
    assert info["primary_key"] == ["qc_result_id"]


def test_describe_accepts_the_diagram_name():
    assert catalog.describe("event_assays")["table"] == "calibration_event_assays"


def test_describe_suggests_close_names():
    with pytest.raises(ValueError, match="qc_results"):
        catalog.describe("qc_result")


def test_join_path_keeps_composite_keys():
    result = catalog.join_paths("qc_results", "qc_runs")
    assert result["paths"][0]["tables"] == ["qc_results", "qc_runs"]
    sql = result["paths"][0]["sql"]
    for column in ("qc_run_id", "instrument_id", "qc_lot_id", "qc_product_id"):
        assert f"qc_results.{column} = qc_runs.{column}" in sql


def test_join_path_through_the_hub():
    paths = catalog.join_paths("test_items", "reagent_products")["paths"]
    assert paths[0]["tables"] == [
        "test_items", "instrument_assays", "assay_reagent_requirements", "reagent_products",
    ]


def test_join_path_reports_alternatives_with_different_meaning():
    result = catalog.join_paths("qc_result_reagents", "reagent_products")
    tables = [tuple(path["tables"]) for path in result["paths"]]
    assert ("qc_result_reagents", "reagent_lots", "reagent_products") in tables
    assert ("qc_result_reagents", "assay_reagent_requirements", "reagent_products") in tables
    assert "note" in result


def test_join_path_points_at_views():
    assert "v_assay_catalog" in catalog.join_paths("lab_sections", "test_items")["tip"]


def test_join_path_rejects_views():
    with pytest.raises(ValueError):
        catalog.join_paths("v_assay_catalog", "test_items")
