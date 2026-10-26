import json
import unittest
from pathlib import Path

import host


class Tool:
    def __init__(self):
        self.name = "query"
        self.description = "Run a read-only SQL query"
        self.input_schema = {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
        }


class Result:
    def __init__(self, text, is_error=False):
        self.is_error = is_error
        self.content = [type("Block", (), {"text": text})()]


class HostTests(unittest.TestCase):
    def test_chat_tools_keep_query_schema(self):
        tools = host.chat_tools([Tool()])
        self.assertEqual(tools[0]["type"], "function")
        self.assertEqual(tools[0]["function"]["name"], "query")
        self.assertIn("sql", tools[0]["function"]["parameters"]["properties"])

    def test_parse_arguments_accepts_json_string_and_dict(self):
        self.assertEqual(host.parse_arguments('{"sql": "select 1"}'), {"sql": "select 1"})
        self.assertEqual(host.parse_arguments({"sql": "select 1"}), {"sql": "select 1"})

    def test_pending_tool_calls_use_chat_completions_shape(self):
        assistant, pending = host.pending_tool_calls(
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "query", "arguments": '{"sql": "select 1"}'},
                    }
                ],
            }
        )
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        self.assertEqual(pending, [("call_1", "query", '{"sql": "select 1"}')])

    def test_system_prompt_contains_catalog_sql(self):
        catalog = json.loads(Path("catalog.json").read_text(encoding="utf-8"))
        prompt = host.system_prompt(catalog)
        self.assertIn("select * from orders", prompt)
        self.assertIn("Sum(products.UnitsInStock)", prompt)
        self.assertNotIn("ConnectionString", prompt)

    def test_render_truncates_and_marks_errors(self):
        self.assertTrue(host.render_tool_result(Result("x" * (host.MAX_TOOL_CHARS + 10))).endswith("...truncated"))
        self.assertTrue(host.render_tool_result(Result("boom", is_error=True)).startswith("SQL error:"))


if __name__ == "__main__":
    unittest.main()
