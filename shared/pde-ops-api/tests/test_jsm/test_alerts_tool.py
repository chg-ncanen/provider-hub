import unittest
from unittest.mock import Mock

from api.jsm.alerts_tool import JSMOpsAlertsTool


class TestJSMOpsAlertsTool(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = JSMOpsAlertsTool(mock_mode=True)

    def test_fetch_open_alerts_returns_expected_shape(self) -> None:
        result = self.tool.fetch_open_alerts()
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "fetch_open_alerts")
        self.assertGreaterEqual(result["count"], 1)
        self.assertIsInstance(result["alerts"], list)

    def test_fetch_closed_alerts_returns_expected_shape(self) -> None:
        result = self.tool.fetch_closed_alerts()
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "fetch_closed_alerts")
        self.assertIsInstance(result["alerts"], list)
        self.assertEqual(result["query"], 'responders:"PDE" AND status:closed')

    def test_fetch_closed_alerts_default_query_falls_back_when_no_status_token(self) -> None:
        tool = JSMOpsAlertsTool(mock_mode=True, filter_query='responders:"PDE"')

        result = tool.fetch_closed_alerts()

        self.assertEqual(result["query"], 'responders:"PDE" AND status:closed')

    def test_get_alert_detail_returns_alert(self) -> None:
        result = self.tool.get_alert_detail("alert-1001")
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "get_alert_detail")
        self.assertEqual(result["alert"]["id"], "alert-1001")

    def test_add_ack_close_operations(self) -> None:
        note_result = self.tool.add_alert_note("alert-1001", "Investigating")
        ack_result = self.tool.acknowledge_alert("alert-1001", note="Owner assigned")
        close_result = self.tool.close_alert("alert-1001", note="Recovered")

        self.assertTrue(note_result["success"])
        self.assertTrue(ack_result["success"])
        self.assertTrue(close_result["success"])
        self.assertEqual(close_result["result"]["status"], "closed")

    def test_tool_definitions_include_expected_tools(self) -> None:
        definitions = self.tool.get_tool_definitions()
        names = {entry["name"] for entry in definitions}

        expected = {
            "fetch_open_alerts",
            "fetch_closed_alerts",
            "get_alert_detail",
            "add_alert_note",
            "acknowledge_alert",
            "close_alert",
        }
        self.assertEqual(names, expected)

    def test_execute_tool_dispatch_and_unknown(self) -> None:
        ok = self.tool.execute_tool("fetch_open_alerts", {})
        unknown = self.tool.execute_tool("does_not_exist", {})

        self.assertTrue(ok["success"])
        self.assertFalse(unknown["success"])
        self.assertIn("Unknown tool", unknown["error"])

    def test_real_mode_never_uses_mock_response(self) -> None:
        tool = JSMOpsAlertsTool(
            mock_mode=False,
            email="user@example.com",
            api_token="token",
        )

        class _FakeResponse:
            status_code = 200
            text = "{}"

            @staticmethod
            def json():
                return {"values": []}

        tool._mock_response = Mock(side_effect=AssertionError("_mock_response must not be called in real mode"))  # type: ignore[method-assign]
        tool.session = Mock()
        tool.session.request.return_value = _FakeResponse()

        result = tool.fetch_open_alerts(query='responders:"PDE"')

        self.assertTrue(result["success"])
        tool._mock_response.assert_not_called()  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
