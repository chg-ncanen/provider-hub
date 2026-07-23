import unittest
from datetime import datetime, timezone

from api.jsm.config import AppConfig
from api.jsm.client import JSMOpsAPI


class TestJSMOpsAPI(unittest.TestCase):
    def test_api_list_get_and_actions_in_mock_mode(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        listed = api.list_alerts()
        self.assertTrue(listed["success"])
        self.assertGreaterEqual(listed["count"], 1)

        alert_id = listed["alerts"][0]["id"]
        detail = api.get_alert(alert_id)
        self.assertTrue(detail["success"])
        self.assertEqual(detail["alert"]["id"], alert_id)

        note = api.add_note(alert_id, "Investigating")
        self.assertTrue(note["success"])

        ack = api.acknowledge(alert_id, note="Owner assigned")
        self.assertTrue(ack["success"])

        closed = api.close(alert_id, note="Recovered")
        self.assertTrue(closed["success"])

    def test_build_alert_query_uses_default_base(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
            alert_filter='responders:"PDE" AND status:open',
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        query = api.build_alert_query(status="acknowledged", priority="P1", service="payments-api")

        self.assertEqual(
            query,
            'responders:"PDE" AND status:acknowledged AND priority:P1 AND service:payments-api',
        )

    def test_build_alert_query_status_override_replaces_default_status(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        query = api.build_alert_query(status="closed")

        self.assertEqual(query, "status:closed")

    def test_list_alerts_includes_filters_in_query(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        result = api.list_alerts(status="open", priority="P2", service="checkout worker", text="queue lag")

        self.assertTrue(result["success"])
        self.assertEqual(
            result["query"],
            'status:open AND priority:P2 AND service:"checkout worker" AND message:"queue lag"',
        )

    def test_list_closed_alerts_requires_a_time_window(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        with self.assertRaises(ValueError):
            api.list_closed_alerts()

    def test_list_closed_alerts_does_not_produce_contradictory_status(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        result = api.list_closed_alerts(since_days=30)

        self.assertTrue(result["success"])
        self.assertEqual(result["query"], "status:closed")

    def test_list_closed_alerts_filters_to_window(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        # Mock alerts are all created on 2026-07-02; a window that excludes
        # that date should return zero, one that includes it should return all.
        excluded = api.list_closed_alerts(
            start=datetime(2020, 1, 1, tzinfo=timezone.utc),
            end=datetime(2020, 1, 2, tzinfo=timezone.utc),
        )
        included = api.list_closed_alerts(
            start=datetime(2026, 7, 1, tzinfo=timezone.utc),
            end=datetime(2026, 7, 3, tzinfo=timezone.utc),
        )

        self.assertEqual(excluded["count"], 0)
        self.assertGreaterEqual(included["count"], 1)

    def test_get_lifecycle_events_does_not_treat_slack_as_ack_event(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        def fake_logs(alert_id: str, size: int = 100):
            del alert_id, size
            return {
                "success": True,
                "logs": [
                    {
                        "logTime": "2026-06-08T19:41:38.612Z",
                        "owner": "user1@example.com",
                        "log": "Slack notification sent to on-call channel",
                        "logType": "activity",
                    },
                    {
                        "logTime": "2026-06-08T19:51:38.612Z",
                        "owner": "user2@example.com",
                        "log": "Alert acknowledged",
                        "logType": "activity",
                    },
                ],
            }

        api.get_alert_logs = fake_logs  # type: ignore[method-assign]
        events = api.get_lifecycle_events("alert-2b")

        self.assertEqual(events["ack_actor"], "user2@example.com")

    def test_get_lifecycle_events_extracts_ack_and_close(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        def fake_logs(alert_id: str, size: int = 100):
            del alert_id, size
            return {
                "success": True,
                "logs": [
                    {
                        "logTime": "2026-06-08T19:41:38.612Z",
                        "owner": "engineer@example.com",
                        "log": "Alert acknowledged by user",
                        "logType": "activity",
                    },
                    {
                        "logTime": "2026-06-08T20:11:38.612Z",
                        "owner": "closer@example.com",
                        "log": "Alert closed",
                        "logType": "activity",
                    },
                ],
            }

        api.get_alert_logs = fake_logs  # type: ignore[method-assign]
        events = api.get_lifecycle_events("alert-1")

        self.assertEqual(events["ack_actor"], "engineer@example.com")
        self.assertEqual(events["close_actor"], "closer@example.com")
        self.assertIsInstance(events["ack_at"], datetime)
        self.assertIsInstance(events["close_at"], datetime)

    def test_get_lifecycle_events_ignores_unack_for_ack_event(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        def fake_logs(alert_id: str, size: int = 100):
            del alert_id, size
            return {
                "success": True,
                "logs": [
                    {
                        "logTime": "2026-06-08T19:41:38.612Z",
                        "owner": "user1@example.com",
                        "log": "Alert unacknowledged",
                        "logType": "activity",
                    },
                    {
                        "logTime": "2026-06-08T19:51:38.612Z",
                        "owner": "user2@example.com",
                        "log": "Alert acknowledged",
                        "logType": "activity",
                    },
                ],
            }

        api.get_alert_logs = fake_logs  # type: ignore[method-assign]
        events = api.get_lifecycle_events("alert-2")

        self.assertEqual(events["ack_actor"], "user2@example.com")

    def test_resolve_acknowledger_prefers_ack_actor(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        actor = api.resolve_acknowledger(
            alert={"id": "a1"},
            lifecycle_events={
                "ack_actor": "acker@example.com",
                "close_actor": "closer@example.com",
            },
        )

        self.assertEqual(actor, "acker@example.com")

    def test_resolve_acknowledger_falls_back_to_close_actor(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        actor = api.resolve_acknowledger(
            alert={"id": "a2"},
            lifecycle_events={
                "ack_actor": "",
                "close_actor": "closer@example.com",
            },
        )

        self.assertEqual(actor, "closer@example.com")

    def test_resolve_acknowledger_details_reassigns_system_ack_to_assignee(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        details = api.resolve_acknowledger_details(
            alert={"id": "a3"},
            lifecycle_events={
                "ack_actor": "System",
                "close_actor": "",
                "assignee_after_system_ack": "alesia.konold@chghealthcare.com",
            },
        )

        self.assertEqual(details["acked_by"], "alesia.konold@chghealthcare.com")
        self.assertEqual(details["picked_up_by_automation"], True)
        self.assertEqual(details["ack_attribution_source"], "automation_proxy_assignee")
        self.assertEqual(details["automation_ack_actor"], "System")

    def test_get_lifecycle_events_extracts_assignee_after_system_ack(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        def fake_logs(alert_id: str, size: int = 100):
            del alert_id, size
            return {
                "success": True,
                "logs": [
                    {
                        "logTime": "2026-06-12T19:56:00.000Z",
                        "owner": "System",
                        "log": "Alert acknowledged via JSM",
                        "logType": "activity",
                    },
                    {
                        "logTime": "2026-06-12T19:59:00.000Z",
                        "owner": "Antonio Guillermo",
                        "log": "Alert ownership assigned to [alesia.konold@chghealthcare.com] via web",
                        "logType": "activity",
                    },
                ],
            }

        api.get_alert_logs = fake_logs  # type: ignore[method-assign]
        events = api.get_lifecycle_events("alert-3")

        self.assertEqual(events["ack_actor"], "System")
        self.assertEqual(events["assignee_after_system_ack"], "alesia.konold@chghealthcare.com")
        self.assertIsInstance(events["assignee_after_system_ack_at"], datetime)

    def test_resolve_acknowledger_details_system_ack_without_assignee_is_unacknowledged(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        details = api.resolve_acknowledger_details(
            alert={"id": "a4"},
            lifecycle_events={
                "ack_actor": "System",
                "close_actor": "nate.canen@chghealthcare.com",
                "assignee_after_system_ack": "",
            },
        )

        self.assertEqual(details["acked_by"], "")
        self.assertEqual(details["picked_up_by_automation"], True)
        self.assertEqual(details["ack_attribution_source"], "automation_unassigned")
        self.assertEqual(details["automation_ack_actor"], "System")

    def test_resolve_acknowledger_details_non_human_closer_maps_to_auto_closed(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        details = api.resolve_acknowledger_details(
            alert={"id": "a5"},
            lifecycle_events={
                "ack_actor": "",
                "close_actor": "Alert API",
                "assignee_after_system_ack": "",
            },
        )

        self.assertEqual(details["acked_by"], "")
        self.assertEqual(details["picked_up_by_automation"], True)
        self.assertEqual(details["ack_attribution_source"], "auto_closed")
        self.assertEqual(details["automation_ack_actor"], "Alert API")

    def test_list_all_alerts_with_include_details_enriches_rows(self) -> None:
        cfg = AppConfig(
            atlassian_email="user@example.com",
            atlassian_api_token="token",
        )
        api = JSMOpsAPI(config=cfg, mock_mode=True)

        alerts = api.list_all_alerts(query='responders:"PDE"', include_details=True)

        self.assertGreaterEqual(len(alerts), 1)
        first = alerts[0]
        self.assertIn("description", first)
        self.assertTrue(str(first.get("description") or "").startswith("Synthetic alert detail"))


if __name__ == "__main__":
    unittest.main()
