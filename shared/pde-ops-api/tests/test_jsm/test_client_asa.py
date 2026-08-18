import unittest

from api.jsm.config import AppConfig
from api.jsm.client import JSMOpsAPI


class TestJSMOpsAPIAsa(unittest.TestCase):
    def setUp(self) -> None:
        cfg = AppConfig(atlassian_email="user@example.com", atlassian_api_token="token")
        self.api = JSMOpsAPI(config=cfg, mock_mode=True)

    def test_list_asa_schedules_returns_expected_shape(self) -> None:
        result = self.api.list_asa_schedules()
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 2)

    def test_resolve_schedule_by_uuid_skips_name_lookup(self) -> None:
        sched = self.api._resolve_schedule("08b69ff0-49d2-453a-a4b4-1f798e6547dc")
        self.assertEqual(sched["id"], "08b69ff0-49d2-453a-a4b4-1f798e6547dc")
        self.assertIsNone(sched["name"])

    def test_resolve_schedule_exact_name_match_wins_over_ambiguity(self) -> None:
        sched = self.api._resolve_schedule("PDE - Mock ASA Schedule")
        self.assertEqual(sched["id"], "mock-schedule-1")

    def test_resolve_schedule_partial_name_match(self) -> None:
        sched = self.api._resolve_schedule("Schedule Two")
        self.assertEqual(sched["id"], "mock-schedule-2")

    def test_resolve_schedule_ambiguous_partial_match_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.api._resolve_schedule("Mock ASA Schedule")

    def test_resolve_schedule_no_match_raises(self) -> None:
        with self.assertRaises(ValueError):
            self.api._resolve_schedule("does not exist")

    def test_get_current_asa_resolves_participant_names(self) -> None:
        result = self.api.get_current_asa("PDE - Mock ASA Schedule")
        self.assertTrue(result["success"])
        self.assertEqual(result["schedule_id"], "mock-schedule-1")
        self.assertEqual(len(result["on_asa"]), 1)
        self.assertEqual(result["on_asa"][0]["display_name"], "Mock User One")

    def test_get_asa_timeline_resolves_periods_and_overrides(self) -> None:
        result = self.api.get_asa_timeline("PDE - Mock ASA Schedule")
        self.assertTrue(result["success"])
        self.assertEqual(len(result["periods"]), 2)
        self.assertEqual(result["periods"][0]["responder"]["display_name"], "Mock User One")
        self.assertEqual(result["periods"][1]["responder"]["display_name"], "Mock User Two")
        self.assertEqual(result["overrides"], [])

    def test_get_asa_timeline_without_overrides_skips_override_block(self) -> None:
        result = self.api.get_asa_timeline("PDE - Mock ASA Schedule", include_overrides=False)
        self.assertEqual(result["overrides"], [])

    def test_list_asa_overrides_returns_expected_shape(self) -> None:
        result = self.api.list_asa_overrides("PDE - Mock ASA Schedule")
        self.assertTrue(result["success"])
        self.assertEqual(result["count"], 0)

    def test_user_resolution_is_cached(self) -> None:
        self.api.get_current_asa("PDE - Mock ASA Schedule")
        self.assertIn("mock-account-1", self.api._user_display_cache)


if __name__ == "__main__":
    unittest.main()
