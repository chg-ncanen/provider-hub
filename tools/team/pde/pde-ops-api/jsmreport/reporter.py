import json
import csv
from collections import defaultdict
from datetime import timezone
from pathlib import Path
import statistics
import sys
from typing import Any, Dict, List, Optional, Protocol

from api.jsm.alerts_tool import JSMOpsAlertsTool


class TextProvider(Protocol):
    def generate_text(self, prompt: str, model: Optional[str] = None, timeout_seconds: int = 180) -> str:
        ...


class ReportBuilder:
    """Deterministic report builder that processes PDE alerts and produces statistics."""

    def __init__(
        self,
        alerts_tool: JSMOpsAlertsTool,
        model: Optional[str] = None,
        mock_mode: bool = False,
        text_provider: Optional[TextProvider] = None,
    ) -> None:
        self.alerts_tool = alerts_tool
        self.model = model or "gpt-4.1"
        self.mock_mode = mock_mode
        self.text_provider = text_provider

    @property
    def system_prompt(self) -> str:
        return (
            "You are the PDE reporter agent. Use available tools to inspect open PDE alerts and produce "
            "a structured markdown report.\n"
            "Requirements:\n"
            "1) Group alerts by priority from highest to lowest.\n"
            "2) For each alert include id, title, service, status, age/context if available.\n"
            "3) Provide recommended next steps for each alert.\n"
            "4) Keep recommendations specific and operationally actionable.\n"
            "5) If no alerts are open, produce a short no-open-alerts report."
        )

    def generate_report(self, user_prompt: Optional[str] = None) -> str:
        if self.mock_mode:
            return self._generate_mock_report()

        result = self.alerts_tool.fetch_open_alerts()
        alerts = result.get("alerts", []) if result.get("success") else []
        prompt = user_prompt or "Analyze PDE open alerts and produce the required markdown report."
        full_prompt = (
            self.system_prompt
            + "\n\n"
            + prompt
            + "\n\nAlert dataset JSON:\n"
            + json.dumps(alerts, ensure_ascii=True)
        )
        provider = self.text_provider
        if provider is None:
            raise ValueError(
                "External AI provider execution is disabled in this backend. "
                "Use deterministic reporting and perform non-deterministic summarization in Copilot skill context."
            )
        return provider.generate_text(prompt=full_prompt, model=self.model).strip()

    def _generate_mock_report(self) -> str:
        result = self.alerts_tool.fetch_open_alerts()
        alerts = result.get("alerts", []) if result.get("success") else []
        if not alerts:
            return "# PDE Alert Report\n\nNo open alerts found for PDE responders."

        groups: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for alert in alerts:
            priority = str(alert.get("priority", "P3")).upper()
            groups[priority].append(alert)

        ordered = sorted(groups.items(), key=lambda item: self._priority_rank(item[0]))

        lines = [
            "# PDE Alert Report",
            "",
            "_Generated in mock mode. Live backend AI execution is disabled in this project._",
            "",
        ]

        for priority, items in ordered:
            lines.append(f"## {priority}")
            lines.append("")
            for alert in items:
                alert_id = alert.get("id") or alert.get("alertId") or "unknown"
                title = alert.get("title", "Untitled alert")
                status = alert.get("status", "unknown")
                service = (alert.get("service") or {}).get("name", "unknown-service")
                created_at = alert.get("createdAt", "unknown")
                lines.append(f"### {alert_id}: {title}")
                lines.append(f"- Service: {service}")
                lines.append(f"- Status: {status}")
                lines.append(f"- Created: {created_at}")
                lines.append("- Recommended next steps:")
                lines.append("  1. Validate impact and blast radius using logs and metrics.")
                lines.append("  2. Acknowledge the alert in JSM Ops and assign an incident owner.")
                lines.append("  3. Add a timeline note with current hypothesis and mitigation plan.")
                lines.append("")

        return "\n".join(lines).strip()

    @staticmethod
    def _priority_rank(priority: str) -> int:
        mapping = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4, "CRITICAL": 0, "HIGH": 1}
        return mapping.get(priority.upper(), 99)

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        try:
            return float(text)
        except ValueError:
            return None

    @staticmethod
    def _stat_summary(values: list[float]) -> Dict[str, Any]:
        if not values:
            return {"count": 0, "avg": None, "median": None}
        return {
            "count": len(values),
            "avg": round(sum(values) / len(values), 2),
            "median": round(statistics.median(values), 2),
        }

    @classmethod
    def summarize_csv(cls, input_path: str | Path) -> Dict[str, Any]:
        path = Path(input_path)
        if not path.exists():
            raise ValueError(f"Input CSV does not exist: {path}")

        try:
            with path.open("r", encoding="utf-8", newline="") as fp:
                reader = csv.DictReader(fp)
                rows = list(reader)
        except (IOError, OSError) as e:
            raise ValueError(f"Failed to read CSV file: {path}: {str(e)}")
        except Exception as e:
            raise ValueError(f"CSV parsing error: {str(e)}")

        if not rows:
            return {
                "success": True,
                "operation": "summarize_csv",
                "input": str(path),
                "rows": 0,
                "overall": {
                    "total_alerts": 0,
                    "open_count": 0,
                    "closed_count": 0,
                    "ack_with_actor_count": 0,
                    "automation_pickup_count": 0,
                    "out_of_hours_ack_count": 0,
                    "time_to_ack_minutes": cls._stat_summary([]),
                    "time_to_close_minutes": cls._stat_summary([]),
                },
                "by_acknowledger": [],
                "by_priority": [],
            }

        ack_groups: Dict[str, Dict[str, Any]] = {}
        priority_groups: Dict[str, Dict[str, Any]] = {}

        all_ack_mins: list[float] = []
        all_close_mins: list[float] = []
        open_count = 0
        closed_count = 0
        ack_with_actor_count = 0
        automation_pickup_count = 0
        out_of_hours_ack_count = 0

        def _ensure_group(group_map: Dict[str, Dict[str, Any]], key: str) -> Dict[str, Any]:
            if key not in group_map:
                group_map[key] = {
                    "count": 0,
                    "open_count": 0,
                    "closed_count": 0,
                    "automation_count": 0,
                    "out_of_hours_ack_count": 0,
                    "ack_minutes": [],
                    "close_minutes": [],
                }
            return group_map[key]

        for row in rows:
            try:
                status = str(row.get("status") or "").strip().lower()
                if status == "closed":
                    closed_count += 1
                elif status == "open":
                    open_count += 1

                acked_by = str(row.get("acked_by") or "").strip()
                picked_up_by_automation = str(row.get("picked_up_by_automation") or "").strip().lower() in {
                    "true",
                    "1",
                    "yes",
                }
                out_of_hours_ack = str(row.get("out_of_hours_ack") or "").strip().lower() in {
                    "true",
                    "1",
                    "yes",
                }

                if acked_by:
                    ack_key = acked_by
                    ack_with_actor_count += 1
                elif status == "closed":
                    ack_key = "Auto-Closed"
                else:
                    ack_key = "unacknowledged"

                if picked_up_by_automation:
                    automation_pickup_count += 1
                if out_of_hours_ack:
                    out_of_hours_ack_count += 1

                priority = str(row.get("priority") or "").strip() or "UNKNOWN"

                ack_minutes = cls._to_float(row.get("time_to_ack_minutes"))
                close_minutes = cls._to_float(row.get("time_to_close_minutes"))
                if ack_minutes is not None:
                    all_ack_mins.append(ack_minutes)
                if close_minutes is not None:
                    all_close_mins.append(close_minutes)

                ack_group = _ensure_group(ack_groups, ack_key)
                ack_group["count"] += 1
                if status == "closed":
                    ack_group["closed_count"] += 1
                elif status == "open":
                    ack_group["open_count"] += 1
                if picked_up_by_automation:
                    ack_group["automation_count"] += 1
                if out_of_hours_ack:
                    ack_group["out_of_hours_ack_count"] += 1
                if ack_minutes is not None:
                    ack_group["ack_minutes"].append(ack_minutes)
                if close_minutes is not None:
                    ack_group["close_minutes"].append(close_minutes)

                priority_group = _ensure_group(priority_groups, priority)
                priority_group["count"] += 1
                if status == "closed":
                    priority_group["closed_count"] += 1
                elif status == "open":
                    priority_group["open_count"] += 1
                if ack_minutes is not None:
                    priority_group["ack_minutes"].append(ack_minutes)
                if close_minutes is not None:
                    priority_group["close_minutes"].append(close_minutes)
            except Exception as e:
                # Skip malformed rows with a warning; don't fail entire report
                print(f"Warning: Skipping malformed row: {str(e)}", file=sys.stderr)

        by_acknowledger = []
        for key, group in ack_groups.items():
            by_acknowledger.append(
                {
                    "acknowledger": key,
                    "count": group["count"],
                    "open_count": group["open_count"],
                    "closed_count": group["closed_count"],
                    "automation_count": group["automation_count"],
                    "out_of_hours_ack_count": group["out_of_hours_ack_count"],
                    "time_to_ack_minutes": cls._stat_summary(group["ack_minutes"]),
                    "time_to_close_minutes": cls._stat_summary(group["close_minutes"]),
                }
            )

        by_acknowledger.sort(key=lambda item: (-item["count"], item["acknowledger"].lower()))

        by_priority = []
        for key, group in priority_groups.items():
            by_priority.append(
                {
                    "priority": key,
                    "count": group["count"],
                    "open_count": group["open_count"],
                    "closed_count": group["closed_count"],
                    "time_to_ack_minutes": cls._stat_summary(group["ack_minutes"]),
                    "time_to_close_minutes": cls._stat_summary(group["close_minutes"]),
                }
            )

        by_priority.sort(key=lambda item: cls._priority_rank(str(item["priority"])))

        return {
            "success": True,
            "operation": "summarize_csv",
            "input": str(path),
            "rows": len(rows),
            "overall": {
                "total_alerts": len(rows),
                "open_count": open_count,
                "closed_count": closed_count,
                "ack_with_actor_count": ack_with_actor_count,
                "automation_pickup_count": automation_pickup_count,
                "out_of_hours_ack_count": out_of_hours_ack_count,
                "time_to_ack_minutes": cls._stat_summary(all_ack_mins),
                "time_to_close_minutes": cls._stat_summary(all_close_mins),
            },
            "by_acknowledger": by_acknowledger,
            "by_priority": by_priority,
        }


class StatisticsAggregator:
    """Aggregates statistics from alert datasets."""

    @classmethod
    def build_stats(cls, input_path: str | Path) -> Dict[str, Any]:
        return ReportBuilder.summarize_csv(input_path=input_path)


class ReportComposer:
    """Composes deterministic report payloads from statistics."""

    @classmethod
    def build_report(
        cls,
        input_path: str | Path,
        summary_input_path: str | Path | None,
    ) -> Dict[str, Any]:
        report = StatisticsAggregator.build_stats(input_path=input_path)
        effective_summary_path = str(summary_input_path) if summary_input_path else str(input_path)
        report["services"] = {
            "statistics_aggregator": "StatisticsAggregator",
            "report_composer": "ReportComposer",
        }
        report["sources"] = {
            "stats_input": str(input_path),
            "alert_summary_input": effective_summary_path,
        }

        return report
