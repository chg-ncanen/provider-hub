import unittest

from api.jsm.schedules_tool import JSMOpsSchedulesTool


class TestJSMOpsSchedulesTool(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = JSMOpsSchedulesTool(mock_mode=True)

    def test_list_schedules_returns_expected_shape(self) -> None:
        result = self.tool.list_schedules()
        self.assertTrue(result["success"])
        self.assertEqual(result["operation"], "list_schedules")
        self.assertGreaterEqual(result["count"], 1)
        self.assertIsInstance(result["schedules"], list)

    def test_get_on_calls_returns_participants(self) -> None:
        result = self.tool.get_on_calls("mock-schedule-1")
        self.assertTrue(result["success"])
        self.assertEqual(result["schedule_id"], "mock-schedule-1")
        self.assertEqual(result["participants"], [{"id": "mock-account-1", "type": "user"}])

    def test_get_timeline_returns_final_and_override_timelines(self) -> None:
        result = self.tool.get_timeline("mock-schedule-1", expand="override")
        self.assertTrue(result["success"])
        timeline = result["timeline"]
        self.assertIn("finalTimeline", timeline)
        self.assertIn("overrideTimeline", timeline)

    def test_list_overrides_returns_expected_shape(self) -> None:
        result = self.tool.list_overrides("mock-schedule-1")
        self.assertTrue(result["success"])
        self.assertEqual(result["overrides"], [])

    def test_resolve_user_display_known_mock_account(self) -> None:
        resolved = self.tool.resolve_user_display("mock-account-1")
        self.assertEqual(resolved["display_name"], "Mock User One")
        self.assertEqual(resolved["email"], "mock.user.one@example.com")

    def test_resolve_user_display_unknown_account_degrades_gracefully(self) -> None:
        resolved = self.tool.resolve_user_display("some-other-account")
        self.assertEqual(resolved["account_id"], "some-other-account")
        self.assertIn("Mock User", resolved["display_name"])


if __name__ == "__main__":
    unittest.main()
