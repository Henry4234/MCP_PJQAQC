import pytest

from mcp_pjqaqc.catalog import table_names
from mcp_pjqaqc.sql_guard import SqlRejected, validate_select

TABLES = table_names()


def check(sql: str, cap: int = 50) -> str:
    return validate_select(sql, TABLES, cap=cap)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT item_code FROM test_items",
        "SELECT item_code FROM test_items WHERE item_code LIKE '%GLU%'",
        "SELECT i.serial_number, COUNT(*) AS n FROM instruments i "
        "JOIN instrument_assays a ON a.instrument_id = i.instrument_id GROUP BY i.serial_number HAVING n > 1",
        "WITH recent AS (SELECT * FROM qc_runs) SELECT COUNT(*) FROM recent",
        "SELECT item_code FROM test_items UNION SELECT specimen_code FROM specimen_types",
        "SELECT * FROM (SELECT item_code FROM test_items) AS t",
        "SELECT DATEDIFF(expiry_date, CURDATE()) FROM reagent_lots",
        "SELECT DATE_FORMAT(run_at, '%Y-%m') FROM qc_runs",
        "SELECT ROW_NUMBER() OVER (PARTITION BY qc_lot_id ORDER BY run_at) FROM qc_runs",
        "SELECT * FROM qc_results WHERE JSON_CONTAINS(westgard_flags, '\"1-3s\"')",
        "SELECT STDDEV_SAMP(result_value), AVG(z_score) FROM qc_results",
        "SELECT j.r FROM qc_results q JOIN JSON_TABLE(q.westgard_flags, '$[*]' COLUMNS (r VARCHAR(20) PATH '$')) j",
        "SELECT '血液組' AS name FROM lab_sections WHERE section_name = '血液組'",
    ],
)
def test_allows_reads(sql):
    assert check(sql).upper().startswith(("SELECT", "WITH", "("))


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO lab_sections (section_code) VALUES ('X')",
        "UPDATE lab_sections SET section_name = 'x'",
        "DELETE FROM qc_results",
        "DROP TABLE qc_results",
        "TRUNCATE TABLE qc_results",
        "ALTER TABLE qc_results ADD COLUMN x INT",
        "CREATE TABLE x (id INT)",
        "REPLACE INTO lab_sections (section_code) VALUES ('X')",
        "CALL anything()",
        "SET GLOBAL read_only = 0",
        "SET SESSION TRANSACTION READ WRITE",
        "COMMIT",
        "GRANT ALL ON *.* TO 'x'",
        "SHOW GRANTS",
        "LOAD DATA INFILE '/etc/passwd' INTO TABLE lab_sections",
        "SELECT 1; DROP TABLE qc_results",
        "SELECT 1; SELECT 2",
        "SELECT * FROM lab_sections INTO OUTFILE '/tmp/x'",
        "SELECT * FROM lab_sections INTO DUMPFILE '/tmp/x'",
        "SELECT section_code INTO @x FROM lab_sections",
        "SELECT * FROM lab_sections FOR UPDATE",
        "SELECT * FROM lab_sections LOCK IN SHARE MODE",
        "SELECT LOAD_FILE('/etc/passwd')",
        "SELECT SLEEP(10)",
        "SELECT * FROM qc_results WHERE 1 AND SLEEP(5)",
        "SELECT BENCHMARK(100000000, MD5('x'))",
        "SELECT GET_LOCK('x', 10)",
        "SELECT @@global.read_only",
        "SELECT @a := 1",
        "SELECT * FROM mysql.user",
        "SELECT * FROM information_schema.tables",
        "SELECT * FROM performance_schema.threads",
        "SELECT * FROM other_db.lab_sections",
        "SELECT * FROM secret_table",
        "WITH user AS (SELECT 1) SELECT * FROM mysql.user",
        "not sql at all",
    ],
)
def test_rejects_everything_else(sql):
    with pytest.raises(SqlRejected):
        check(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1 /*! INTO OUTFILE '/tmp/x' */",
        "SELECT 1 /*M! INTO OUTFILE '/tmp/x' */",
        "SELECT /*!50000 SLEEP(10), */ 1",
        "SELECT 1 -- hidden comment\n",
        "SELECT 1 # hidden comment\n",
    ],
)
def test_executable_comments_never_reach_the_server(sql):
    checked = check(sql)
    assert "/*" not in checked
    assert "OUTFILE" not in checked.upper()
    assert "SLEEP" not in checked.upper()


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT item_code FROM test_items", "LIMIT 50"),
        ("SELECT item_code FROM test_items LIMIT 9999", "LIMIT 50"),
        ("SELECT item_code FROM test_items LIMIT 7", "LIMIT 7"),
        ("SELECT item_code FROM test_items UNION SELECT specimen_code FROM specimen_types", "LIMIT 50"),
    ],
)
def test_row_limit_is_enforced(sql, expected):
    assert check(sql).endswith(expected)
