# pyproject.toml declares requires-python >=3.9, but this file uses PEP 604
# `X | Y` union syntax (e.g. `datetime | str`), which only evaluates at
# runtime on 3.10+ — deferring annotation evaluation keeps it working on 3.9.
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Dict, List, Optional

from api.jsm.config import AppConfig
from api.jsm.alerts_tool import JSMOpsAlertsTool
from api.jsm.schedules_tool import JSMOpsSchedulesTool


class JSMOpsAPI:
    """Reusable Python API for JSM Ops alert operations."""

    # System actor names (bot/automation accounts) to exclude from human acknowledger attribution
    DEFAULT_SYSTEM_ACTORS = {"system", "alert api"}

    def __init__(
        self,
        config: AppConfig,
        mock_mode: bool = False,
        system_actors: Optional[set[str]] = None,
    ) -> None:
        self.config = config
        self.system_actors = system_actors or self.DEFAULT_SYSTEM_ACTORS
        self.tool = JSMOpsAlertsTool(
            cloud_id=config.atlassian_cloud_id,
            email=config.atlassian_email,
            api_token=config.atlassian_api_token,
            filter_query=config.alert_filter,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            mock_mode=mock_mode,
        )
        self.schedules_tool = JSMOpsSchedulesTool(
            cloud_id=config.atlassian_cloud_id,
            email=config.atlassian_email,
            api_token=config.atlassian_api_token,
            timeout_seconds=config.timeout_seconds,
            max_retries=config.max_retries,
            mock_mode=mock_mode,
        )
        self._schedules_cache: Optional[List[Dict[str, Any]]] = None
        self._user_display_cache: Dict[str, Dict[str, Optional[str]]] = {}

    @classmethod
    def from_config_file(
        cls,
        config_path: str = "app_config.json",
        mock_mode: bool = False,
    ) -> "JSMOpsAPI":
        cfg = AppConfig.from_env(config_path=config_path)
        return cls(config=cfg, mock_mode=mock_mode)

    def list_alerts(
        self,
        query: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        service: Optional[str] = None,
        text: Optional[str] = None,
    ) -> Dict[str, Any]:
        final_query = self.build_alert_query(
            base_query=query,
            status=status,
            priority=priority,
            service=service,
            text=text,
        )
        return self.tool.fetch_open_alerts(query=final_query, limit=limit, cursor=cursor)

    def list_closed_alerts(
        self,
        since_days: Optional[float] = None,
        start: Optional[datetime] = None,
        end: Optional[datetime] = None,
        query: Optional[str] = None,
        priority: Optional[str] = None,
        service: Optional[str] = None,
        text: Optional[str] = None,
        limit_per_page: int = 100,
        max_pages: int = 50,
    ) -> Dict[str, Any]:
        # Closed-alert history is unbounded (unlike "open", which is naturally
        # capped to whatever hasn't been resolved yet), so a time window is
        # required here to avoid paging through the entire archive.
        #
        # The alerts endpoint only supports newest-first offset pagination (no
        # server-side date filter), so pagination cost scales with (now -
        # window_start), not with the window width. This is cheap for the
        # common case (end defaults to now), but an explicit `end` set well in
        # the past still requires paging through everything newer than it
        # first; `max_pages` is the hard cap on that cost.
        if start is None and since_days is None:
            raise ValueError(
                "list_closed_alerts requires a time window: pass since_days or start."
            )

        window_end = self._as_utc(end) if end is not None else datetime.now(timezone.utc)
        window_start = (
            self._as_utc(start) if start is not None else window_end - timedelta(days=since_days)
        )

        final_query = self.build_alert_query(
            base_query=query,
            status="closed",
            priority=priority,
            service=service,
            text=text,
        )

        matched: List[Dict[str, Any]] = []
        offset = 0
        pages_fetched = 0

        while pages_fetched < max_pages:
            pages_fetched += 1
            payload = self.tool._request(
                "GET",
                "",
                params={"query": final_query, "size": limit_per_page, "offset": offset},
            )
            alerts = self.tool._extract_alerts(payload)
            if not alerts:
                break

            # Alerts are returned newest-first by default, so once an entire
            # page predates the window start, nothing further can match.
            page_has_any_in_window = False
            for alert in alerts:
                created_at = self._alert_created_at(alert)
                if created_at is None or created_at < window_start:
                    continue
                page_has_any_in_window = True
                if created_at <= window_end:
                    matched.append(alert)

            if not page_has_any_in_window:
                break

            links = payload.get("links") if isinstance(payload, dict) else None
            if not isinstance(links, dict) or not links.get("next"):
                break
            offset += limit_per_page

        return {
            "success": True,
            "operation": "list_closed_alerts",
            "query": final_query,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "count": len(matched),
            "alerts": matched,
            "pages_fetched": pages_fetched,
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @staticmethod
    def _alert_created_at(alert: Dict[str, Any]) -> Optional[datetime]:
        raw = (alert or {}).get("createdAt") or (alert or {}).get("created_at")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    def get_alert(self, alert_id: str) -> Dict[str, Any]:
        return self.tool.get_alert_detail(alert_id=alert_id)

    def list_all_alerts(
        self,
        query: Optional[str] = None,
        limit_per_page: int = 100,
        max_pages: int = 1000,
        include_details: bool = False,
    ) -> List[Dict[str, Any]]:
        all_alerts: List[Dict[str, Any]] = []
        offset = 0
        effective_query = query or self.config.alert_filter
        seen_ids: set[str] = set()
        seen_page_signatures: set[str] = set()
        page_count = 0

        while True:
            page_count += 1
            if page_count > max_pages:
                break

            payload = self.tool._request(
                "GET",
                "",
                params={
                    "query": effective_query,
                    "size": limit_per_page,
                    "offset": offset,
                },
            )
            alerts = self.tool._extract_alerts(payload)
            if not alerts:
                break

            page_signature = json.dumps(
                {
                    "offset": offset,
                    "count": len(alerts),
                    "ids": [str((a or {}).get("id") or "") for a in alerts],
                },
                sort_keys=True,
            )
            if page_signature in seen_page_signatures:
                break
            seen_page_signatures.add(page_signature)

            new_alerts: List[Dict[str, Any]] = []
            for alert in alerts:
                alert_id = str((alert or {}).get("id") or "").strip()
                if alert_id:
                    if alert_id in seen_ids:
                        continue
                    seen_ids.add(alert_id)
                new_alerts.append(alert)

            # Guard against APIs that keep returning the same page while still reporting a next link.
            if not new_alerts:
                break

            if include_details:
                enriched_alerts: List[Dict[str, Any]] = []
                for alert in new_alerts:
                    alert_id = str((alert or {}).get("id") or "").strip()
                    if not alert_id:
                        enriched_alerts.append(alert)
                        continue

                    try:
                        detail_result = self.get_alert(alert_id)
                        detail_alert = detail_result.get("alert") if isinstance(detail_result, dict) else None
                        if isinstance(detail_alert, dict):
                            merged = dict(alert)
                            merged.update(detail_alert)
                            enriched_alerts.append(merged)
                            continue
                    except Exception:
                        pass

                    enriched_alerts.append(alert)

                all_alerts.extend(enriched_alerts)
            else:
                all_alerts.extend(new_alerts)
            links = payload.get("links") if isinstance(payload, dict) else None
            if not isinstance(links, dict) or not links.get("next"):
                break
            offset += limit_per_page

        return all_alerts

    def get_alert_logs(
        self,
        alert_id: str,
        size: int = 100,
    ) -> Dict[str, Any]:
        payload = self.tool._request("GET", f"/{alert_id}/logs", params={"size": size})
        logs = payload.get("values", []) if isinstance(payload, dict) else []
        return {
            "success": True,
            "operation": "get_alert_logs",
            "alert_id": alert_id,
            "logs": logs,
            "raw": payload,
        }

    def get_ack_actor(self, alert_id: str) -> Optional[str]:
        lifecycle = self.get_lifecycle_events(alert_id=alert_id)
        return lifecycle.get("ack_actor")

    def resolve_acknowledger(
        self,
        alert: Dict[str, Any],
        lifecycle_events: Dict[str, Optional[datetime | str]],
    ) -> str:
        resolution = self.resolve_acknowledger_details(alert=alert, lifecycle_events=lifecycle_events)
        return str(resolution.get("acked_by") or "")

    def resolve_acknowledger_details(
        self,
        alert: Dict[str, Any],
        lifecycle_events: Dict[str, Optional[datetime | str]],
    ) -> Dict[str, Any]:
        del alert
        ack_actor = str(lifecycle_events.get("ack_actor") or "").strip()
        if ack_actor:
            if self._is_system_actor(ack_actor):
                assignee_after_system_ack = str(
                    lifecycle_events.get("assignee_after_system_ack") or ""
                ).strip()
                if assignee_after_system_ack and not self._is_system_actor(assignee_after_system_ack):
                    return {
                        "acked_by": assignee_after_system_ack,
                        "picked_up_by_automation": True,
                        "ack_attribution_source": "automation_proxy_assignee",
                        "automation_ack_actor": ack_actor,
                        "human_first_touch_at": lifecycle_events.get("assignee_after_system_ack_at"),
                    }
                return {
                    "acked_by": "",
                    "picked_up_by_automation": True,
                    "ack_attribution_source": "automation_unassigned",
                    "automation_ack_actor": ack_actor,
                    "human_first_touch_at": None,
                }
            return {
                "acked_by": ack_actor,
                "picked_up_by_automation": False,
                "ack_attribution_source": "direct_ack",
                "automation_ack_actor": "",
                "human_first_touch_at": lifecycle_events.get("ack_at"),
            }

        close_actor = str(lifecycle_events.get("close_actor") or "").strip()
        if close_actor and not self._is_system_actor(close_actor):
            return {
                "acked_by": close_actor,
                "picked_up_by_automation": False,
                "ack_attribution_source": "close_fallback",
                "automation_ack_actor": "",
                "human_first_touch_at": lifecycle_events.get("close_at"),
            }
        if close_actor and self._is_system_actor(close_actor):
            return {
                "acked_by": "",
                "picked_up_by_automation": True,
                "ack_attribution_source": "auto_closed",
                "automation_ack_actor": close_actor,
                "human_first_touch_at": None,
            }

        return {
            "acked_by": "",
            "picked_up_by_automation": False,
            "ack_attribution_source": "unacknowledged",
            "automation_ack_actor": "",
            "human_first_touch_at": None,
        }

    def get_lifecycle_events(self, alert_id: str) -> Dict[str, Optional[datetime | str]]:
        result = self.get_alert_logs(alert_id=alert_id)
        logs = result.get("logs", [])
        normalized = self._normalize_logs(logs)

        ack_event = self._first_event(normalized, self._is_ack_event)
        close_event = self._first_event(normalized, self._is_close_event)
        assignee_after_system_ack = None
        assignee_after_system_ack_at = None
        if ack_event and self._is_system_actor(str(ack_event.get("actor") or "")):
            assignment_event = self._first_assignment_after(
                normalized_logs=normalized,
                after_time=ack_event.get("time"),
            )
            if assignment_event:
                assignee_after_system_ack = str(assignment_event.get("assignee") or "")
                assignee_after_system_ack_at = assignment_event.get("time")

        return {
            "ack_at": ack_event.get("time") if ack_event else None,
            "ack_actor": ack_event.get("actor") if ack_event else None,
            "close_at": close_event.get("time") if close_event else None,
            "close_actor": close_event.get("actor") if close_event else None,
            "assignee_after_system_ack": assignee_after_system_ack,
            "assignee_after_system_ack_at": assignee_after_system_ack_at,
        }

    @staticmethod
    def _normalize_logs(logs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        normalized: List[Dict[str, Any]] = []
        for idx, log in enumerate(logs):
            raw_time = log.get("logTime") or log.get("createdAt") or log.get("updatedAt")
            event_time: Optional[datetime] = None
            if raw_time:
                try:
                    event_time = datetime.fromisoformat(str(raw_time).replace("Z", "+00:00"))
                except ValueError:
                    event_time = None

            actor = log.get("owner") or log.get("ownerName") or log.get("ownerId")
            text = f"{log.get('logType', '')} {log.get('log', '')}".lower()
            normalized.append(
                {
                    "index": idx,
                    "time": event_time,
                    "actor": str(actor) if actor else None,
                    "raw_log": str(log.get("log") or ""),
                    "text": text,
                }
            )

        normalized.sort(key=lambda item: (item["time"] is None, item["time"], item["index"]))
        return normalized

    @staticmethod
    def _first_event(
        normalized_logs: List[Dict[str, Any]],
        matcher,
    ) -> Optional[Dict[str, Any]]:
        for item in normalized_logs:
            if matcher(item["text"]):
                return item
        return None

    @staticmethod
    def _is_ack_event(text: str) -> bool:
        if "unack" in text:
            return False
        return bool(re.search(r"\b(ack|acknowledge|acknowledged)\b", text))

    @staticmethod
    def _is_close_event(text: str) -> bool:
        # Mirror _is_ack_event's un-prefix guard: "Alert un-resolved by X" (a
        # reopen) contains "resolved" as a substring and would otherwise be
        # misclassified as the alert's close event.
        if "unresolved" in text or "un-resolved" in text or "reopen" in text:
            return False
        return bool(re.search(r"\b(close|closed|resolve|resolved)\b", text))

    def _is_system_actor(self, actor: str) -> bool:
        return actor.strip().lower() in self.system_actors

    @staticmethod
    def _first_assignment_after(
        normalized_logs: List[Dict[str, Any]],
        after_time: Optional[datetime],
    ) -> Optional[Dict[str, Any]]:
        if after_time is None:
            # Can't establish "after the system ack" ordering without a
            # timestamp for that ack event (e.g. its logTime failed to parse)
            # — returning the first assignment match anywhere in the log
            # regardless of order risks misattributing a pre-ack assignment
            # as having happened after it, so report nothing instead.
            return None

        # More flexible regex pattern to handle variations in assignment message format
        pattern = re.compile(
            r"(?:alert ownership|owner|assigned)\s+(?:assigned\s+)?to\s+\[?([^\]\n,]+)\]?",
            re.IGNORECASE,
        )
        for item in normalized_logs:
            event_time = item.get("time")
            if event_time is None or event_time < after_time:
                continue

            raw_log = str(item.get("raw_log") or "")
            match = pattern.search(raw_log)
            if match:
                assignee = match.group(1).strip()
                if assignee:
                    return {
                        "assignee": assignee,
                        "time": event_time,
                    }
        return None

    def add_note(self, alert_id: str, note: str) -> Dict[str, Any]:
        return self.tool.add_alert_note(alert_id=alert_id, note=note)

    def acknowledge(self, alert_id: str, note: Optional[str] = None) -> Dict[str, Any]:
        return self.tool.acknowledge_alert(alert_id=alert_id, note=note)

    def close(self, alert_id: str, note: Optional[str] = None) -> Dict[str, Any]:
        return self.tool.close_alert(alert_id=alert_id, note=note)

    def assign(self, alert_id: str, owner: str) -> Dict[str, Any]:
        return self.tool.assign_alert(alert_id=alert_id, owner=owner)

    def build_alert_query(
        self,
        base_query: Optional[str] = None,
        status: Optional[str] = None,
        priority: Optional[str] = None,
        service: Optional[str] = None,
        text: Optional[str] = None,
    ) -> str:
        parts: List[str] = []
        base = base_query if base_query else self.config.alert_filter
        # An explicit status overrides any status clause baked into the base
        # (e.g. self.config.alert_filter defaults to "status:open"), otherwise
        # requesting status="closed" would AND against "status:open" and never match.
        if status and base:
            base = self._strip_status_clause(base)
        if base:
            parts.append(base)

        if status:
            parts.append(f"status:{self._quote(status)}")
        if priority:
            parts.append(f"priority:{self._quote(priority)}")
        if service:
            parts.append(f"service:{self._quote(service)}")
        if text:
            parts.append(f"message:{self._quote(text)}")

        return " AND ".join(parts)

    @staticmethod
    def _strip_status_clause(query: str) -> str:
        without_status = re.sub(r'status:(?:"[^"]*"|\S+)', "", query, flags=re.IGNORECASE)
        without_status = re.sub(r"\s+AND\s+AND\s+", " AND ", without_status, flags=re.IGNORECASE)
        without_status = re.sub(r"^\s*AND\s+", "", without_status, flags=re.IGNORECASE)
        without_status = re.sub(r"\s+AND\s*$", "", without_status, flags=re.IGNORECASE)
        return without_status.strip()

    @staticmethod
    def _quote(value: str) -> str:
        escaped = value.replace('"', '\\"').strip()
        if " " in escaped or ":" in escaped:
            return f'"{escaped}"'
        return escaped

    # ------------------------------------------------------------------
    # ASA (App Support Ambassador) rotation support
    #
    # PDE calls this rotation "ASA"; the underlying JSM Ops / Opsgenie API
    # calls the same concept "on-call" (schedules/on-calls/overrides). These
    # methods expose it under PDE's own name and resolve the bare Atlassian
    # accountIds the API returns into display names/emails.
    # ------------------------------------------------------------------

    _SCHEDULE_ID_PATTERN = re.compile(
        r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
    )

    def list_asa_schedules(self, refresh: bool = False) -> Dict[str, Any]:
        if self._schedules_cache is None or refresh:
            result = self.schedules_tool.list_schedules()
            self._schedules_cache = result.get("schedules", [])
        return {
            "success": True,
            "count": len(self._schedules_cache),
            "schedules": self._schedules_cache,
        }

    def _resolve_schedule(self, schedule: str) -> Dict[str, Any]:
        if self._SCHEDULE_ID_PATTERN.match(schedule.strip()):
            return {"id": schedule.strip(), "name": None}

        schedules = self.list_asa_schedules()["schedules"]
        needle = schedule.strip().lower()
        exact = [s for s in schedules if str(s.get("name", "")).strip().lower() == needle]
        if len(exact) == 1:
            return exact[0]

        matches = [s for s in schedules if needle in str(s.get("name", "")).lower()]
        if not matches:
            raise ValueError(f"No ASA schedule found matching {schedule!r}.")
        if len(matches) > 1:
            names = ", ".join(str(s.get("name", "")) for s in matches)
            raise ValueError(f"Multiple ASA schedules match {schedule!r}: {names}. Be more specific.")
        return matches[0]

    def _resolve_user(self, account_id: str) -> Dict[str, Optional[str]]:
        if account_id not in self._user_display_cache:
            self._user_display_cache[account_id] = self.schedules_tool.resolve_user_display(account_id)
        return self._user_display_cache[account_id]

    def get_current_asa(self, schedule: str, date: Optional[str] = None) -> Dict[str, Any]:
        sched = self._resolve_schedule(schedule)
        result = self.schedules_tool.get_on_calls(schedule_id=sched["id"], date=date)
        people = [
            self._resolve_user(p["id"])
            for p in result.get("participants", [])
            if isinstance(p, dict) and p.get("type") == "user" and p.get("id")
        ]
        return {
            "success": True,
            "schedule": sched.get("name") or schedule,
            "schedule_id": sched["id"],
            "date": date,
            "on_asa": people,
        }

    def get_asa_timeline(
        self,
        schedule: str,
        date: Optional[str] = None,
        interval: int = 4,
        interval_unit: str = "weeks",
        include_overrides: bool = True,
    ) -> Dict[str, Any]:
        sched = self._resolve_schedule(schedule)
        expand = "override" if include_overrides else None
        result = self.schedules_tool.get_timeline(
            schedule_id=sched["id"],
            date=date,
            interval=interval,
            interval_unit=interval_unit,
            expand=expand,
        )
        timeline = result.get("timeline", {}) if isinstance(result, dict) else {}
        periods = self._flatten_timeline_periods(timeline.get("finalTimeline"))
        overrides = (
            self._flatten_timeline_periods(timeline.get("overrideTimeline"))
            if include_overrides
            else []
        )
        for period in periods + overrides:
            responder_id = period.get("responder_id")
            if responder_id:
                period["responder"] = self._resolve_user(responder_id)

        return {
            "success": True,
            "schedule": sched.get("name") or schedule,
            "schedule_id": sched["id"],
            "window_start": timeline.get("startDate"),
            "window_end": timeline.get("endDate"),
            "periods": periods,
            "overrides": overrides,
        }

    @staticmethod
    def _flatten_timeline_periods(timeline_block: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for rotation in (timeline_block or {}).get("rotations", []):
            for period in rotation.get("periods", []):
                responder = period.get("responder") or {}
                out.append(
                    {
                        "rotation_name": rotation.get("name"),
                        "start": period.get("startDate"),
                        "end": period.get("endDate"),
                        "type": period.get("type"),
                        "responder_id": responder.get("id"),
                    }
                )
        return out

    def list_asa_overrides(self, schedule: str) -> Dict[str, Any]:
        sched = self._resolve_schedule(schedule)
        result = self.schedules_tool.list_overrides(schedule_id=sched["id"])
        overrides = result.get("overrides", [])
        for override in overrides:
            responder = override.get("user") or override.get("responder") or {}
            responder_id = responder.get("id") if isinstance(responder, dict) else None
            if responder_id:
                override["responder"] = self._resolve_user(responder_id)
        return {
            "success": True,
            "schedule": sched.get("name") or schedule,
            "schedule_id": sched["id"],
            "count": len(overrides),
            "overrides": overrides,
        }
