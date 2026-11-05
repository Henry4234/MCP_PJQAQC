"""Static checks for ad-hoc SQL. The rewritten AST is what runs, so comments cannot smuggle code."""
from __future__ import annotations

from typing import Collection

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError

# sqlglot types common functions itself; anything it leaves as Anonymous must be listed here.
ALLOWED_ANONYMOUS_FUNCTIONS = frozenset(
    """
    abs adddate avg ceil ceiling char_length character_length coalesce concat concat_ws convert count
    cume_dist curdate curtime date_add date_format date_sub datediff day dayname dayofmonth dayofweek
    dayofyear dense_rank field find_in_set first_value floor format from_days from_unixtime greatest
    group_concat hour if ifnull instr isnull json_array json_contains json_extract json_length
    json_object json_query json_quote json_search json_type json_unquote json_valid json_value lag
    last_day last_value lcase lead least left length ln locate log log10 lower lpad ltrim max median min
    minute mod month monthname now nth_value ntile nullif percent_rank percentile_cont percentile_disc
    position pow power quarter rank replace reverse right round row_number rpad rtrim second sign sqrt
    std stddev stddev_pop stddev_samp str_to_date subdate substr substring sum timestampdiff to_days
    trim truncate ucase unix_timestamp upper utc_date utc_timestamp var_pop var_samp variance week
    weekofyear year yearweek
    """.split()
)

FORBIDDEN_NODES: tuple[tuple[type[exp.Expression], str], ...] = (
    (exp.Into, "不允許 SELECT ... INTO"),
    (exp.Lock, "不允許 FOR UPDATE / LOCK IN SHARE MODE"),
    (exp.Parameter, "不允許使用 @ 變數"),
    (exp.SessionParameter, "不允許讀寫 @@ 系統變數"),
    (exp.Insert, "只允許 SELECT"),
    (exp.Update, "只允許 SELECT"),
    (exp.Delete, "只允許 SELECT"),
    (exp.Command, "只允許 SELECT"),
)


class SqlRejected(ValueError):
    pass


def _check_tables(root: exp.Expression, allowed_tables: Collection[str]) -> None:
    cte_names = {cte.alias.lower() for cte in root.find_all(exp.CTE)}
    allowed = {name.lower() for name in allowed_tables}
    for table in root.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            if isinstance(table.this, exp.JSONTable):
                continue
            raise SqlRejected(f"不允許以函式作為資料來源：{table.sql(dialect='mysql')}")
        if table.db or table.catalog:
            raise SqlRejected(f"不允許跨資料庫查詢：{table.sql(dialect='mysql')}")
        if table.name.lower() not in allowed | cte_names:
            raise SqlRejected(f"資料表不在 PJQAQC 結構內：{table.name}")


def _check_functions(root: exp.Expression) -> None:
    for func in root.find_all(exp.Anonymous):
        if func.name.lower() not in ALLOWED_ANONYMOUS_FUNCTIONS:
            raise SqlRejected(f"不允許使用函式：{func.name}")


def _cap_limit(root: exp.Query, cap: int) -> exp.Query:
    limit = root.args.get("limit")
    if isinstance(limit, exp.Limit):
        value = limit.expression
        if isinstance(value, exp.Literal) and value.is_int and int(value.this) <= cap:
            return root
    return root.limit(cap, copy=False)


def validate_select(sql: str, allowed_tables: Collection[str], cap: int) -> str:
    """Return the SQL to execute: one SELECT, allow-listed tables and functions, at most cap rows."""
    try:
        statements = [s for s in sqlglot.parse(sql, dialect="mysql") if s is not None]
    except SqlglotError as exc:
        raise SqlRejected(f"無法解析 SQL：{str(exc)[:300]}") from exc

    if len(statements) != 1:
        raise SqlRejected("一次只能執行一條 SQL")
    root = statements[0]
    if not isinstance(root, exp.Query):
        raise SqlRejected("只允許 SELECT")

    for node_type, message in FORBIDDEN_NODES:
        if root.find(node_type) is not None:
            raise SqlRejected(message)
    _check_tables(root, allowed_tables)
    _check_functions(root)

    return _cap_limit(root, cap).sql(dialect="mysql", comments=False)
