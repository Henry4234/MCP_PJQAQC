"""Exercise the server the way an MCP client does, over an in-memory connection."""
import asyncio
import json

from mcp import Client
from mcp.types import TextContent

from mcp_pjqaqc.server import mcp

EXPECTED_TOOLS = {
    "route_question", "get_schema_overview", "describe_table", "find_join_path", "check_connection",
    "search_entities", "find_assays", "get_product_usage", "find_lots", "get_qc_results", "get_qc_summary",
    "get_qc_targets", "get_loaded_reagents", "trace_reagent_lot", "trace_qc_result",
    "get_calibration_history", "run_readonly_query",
}


def run(coro_factory):
    async def runner():
        async with Client(mcp) as client:
            return await coro_factory(client)

    return asyncio.run(runner())


def text_of(result) -> str:
    return next(block.text for block in result.content if isinstance(block, TextContent))


def test_all_tools_are_listed_and_marked_read_only():
    tools = run(lambda c: c.list_tools()).tools
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.description, tool.name


def test_instructions_reach_the_client():
    async def get(client):
        return client.instructions

    assert "主機與備機不可合併" in run(get)


def test_schema_tool_returns_readable_chinese():
    result = run(lambda c: c.call_tool("describe_table", {"table": "qc_results"}))
    assert result.is_error is False
    assert "品管結果" in text_of(result)
    assert json.loads(text_of(result))["table"] == "qc_results"


def test_unknown_table_is_a_tool_error_not_a_crash():
    result = run(lambda c: c.call_tool("describe_table", {"table": "nope_table"}))
    assert result.is_error is True
    assert "找不到資料表" in text_of(result)


def test_invalid_enum_is_rejected_before_any_query():
    result = run(lambda c: c.call_tool("find_lots", {"status": "DROP TABLE"}))
    assert result.is_error is True


def test_write_sql_is_rejected_through_the_tool():
    result = run(lambda c: c.call_tool("run_readonly_query", {"sql": "DROP TABLE qc_results"}))
    assert result.is_error is True
    assert "只允許 SELECT" in text_of(result)


def test_resources_serve_the_schema():
    overview = run(lambda c: c.read_resource("pjqaqc://schema/overview"))
    assert "instrument_assays" in overview.contents[0].text
    table = run(lambda c: c.read_resource("pjqaqc://schema/tables/qc_lots"))
    assert json.loads(table.contents[0].text)["table"] == "qc_lots"
