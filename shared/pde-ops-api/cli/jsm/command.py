import argparse
import csv
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

try:
    from dotenv import find_dotenv, load_dotenv
except Exception:
    find_dotenv = None
    load_dotenv = None

from api.jsm.config import AppConfig, DEFAULT_CSV_COLUMNS
from api.jsm.client import JSMOpsAPI


PROFILE_QUERIES = {
    "pde": 'responders:"PDE"',
}


def _parse_window_dt(value: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError("Date must be in 'YYYY-MM-DDTHH:MM' or 'YYYY-MM-DD HH:MM' format.")


def _tz_from_name(name: str) -> timezone:
    normalized = name.strip().upper()
    if normalized == "MST":
        return timezone(timedelta(hours=-7))
    if normalized == "UTC":
        return timezone.utc
    raise ValueError(f"Unsupported timezone: {name}")


def _resolve_query(explicit_query: str | None, profile: str | None, default_query: str) -> str:
    if explicit_query:
        return explicit_query
    if profile:
        preset = PROFILE_QUERIES.get(profile.lower())
        if not preset:
            raise ValueError(f"Unknown profile: {profile}")
        # AND the preset onto the configured base filter (e.g. status:open)
        # rather than replacing it, so "--profile pde" doesn't silently drop
        # that scoping — mirrors the same fix in mcp/tools/alerts.py.
        return f"{default_query} AND {preset}" if default_query else preset
    return default_query


def _resolve_profile(explicit_profile: str | None, default_profile: str | None) -> str | None:
    return explicit_profile or default_profile


def _resolve_columns(explicit_columns: str | None, default_columns: list[str] | None) -> list[str]:
    if explicit_columns:
        columns = [c.strip() for c in explicit_columns.split(",") if c.strip()]
        if not columns:
            raise ValueError("--columns must include at least one column name.")
        return columns
    return list(default_columns or DEFAULT_CSV_COLUMNS)


def _duration_seconds(start: datetime, end: datetime | None) -> str:
    if not end:
        return ""
    delta = (end - start).total_seconds()
    if delta < 0:
        return ""
    return str(int(delta))


def _duration_minutes(start: datetime, end: datetime | None) -> str:
    if not end:
        return ""
    delta = (end - start).total_seconds()
    if delta < 0:
        return ""
    return f"{delta / 60:.2f}"


def _is_out_of_hours(local_dt: datetime | None) -> bool:
    if not local_dt:
        return False
    weekday = local_dt.weekday()  # Monday=0
    if weekday >= 5:
        return True
    hour = local_dt.hour
    return hour < 9 or hour >= 18


def _stringify_list(value: Any) -> str:
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if str(v).strip())
    if value is None:
        return ""
    return str(value)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "JSM Ops CLI for alert operations and CSV export.\n"
            "Defaults for profile/timezone/columns are read from app_config.json unless overridden by flags."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m cli list\n"
            "  python -m cli list --profile pde --status open --priority P1\n"
            "  python -m cli list-closed --profile pde --since-days 2\n"
            "  python -m cli export-csv --output ./out/alerts.csv --start 2026-06-08T09:00 --end 2026-07-02T09:00\n"
            "  python -m cli export-csv --output ./out/subset.csv --start 2026-06-08T09:00 --end 2026-07-02T09:00 --columns alert_id,status,priority"
        ),
    )
    parser.add_argument(
        "--config",
        default="app_config.json",
        help="Path to non-secret JSON config file (default: app_config.json).",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Run in mock mode (no external API calls).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print raw JSON output.",
    )

    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")

    list_parser = subparsers.add_parser(
        "list",
        help="List alerts with optional filters.",
        description="List alerts using either explicit query or profile/default query.",
    )
    list_parser.add_argument("--query", help="Optional base JSM query.")
    list_parser.add_argument("--limit", type=int, help="Optional max alerts to fetch.")
    list_parser.add_argument("--cursor", help="Optional pagination cursor.")
    list_parser.add_argument("--status", help="Filter by status (for example: open, acknowledged).")
    list_parser.add_argument("--priority", help="Filter by priority (for example: P1, P2).")
    list_parser.add_argument("--service", help="Filter by service name.")
    list_parser.add_argument("--text", help="Filter by text search term.")
    list_parser.add_argument(
        "--profile",
        choices=["pde", "PDE"],
        help="Optional query preset profile (for example: pde).",
    )

    list_closed_parser = subparsers.add_parser(
        "list-closed",
        help="List closed alerts within a required time window.",
        description=(
            "List closed alerts. Closed-alert history is unbounded (unlike open alerts, "
            "which are naturally capped to whatever hasn't been resolved yet), so a time "
            "window is required: pass --since-days, or --start (optionally with --end)."
        ),
    )
    list_closed_parser.add_argument("--query", help="Optional base JSM query.")
    list_closed_parser.add_argument(
        "--since-days", type=float, help="Look back this many days from now, e.g. 2."
    )
    list_closed_parser.add_argument(
        "--start",
        help="Window start in local time, format YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM. Alternative to --since-days.",
    )
    list_closed_parser.add_argument(
        "--end",
        help="Window end in local time (defaults to now if --start is given without --end).",
    )
    list_closed_parser.add_argument(
        "--timezone",
        default=None,
        choices=["MST", "UTC", "mst", "utc"],
        help=(
            "Timezone for --start/--end.\n"
            "Default resolution order: --timezone > config default_timezone > MST"
        ),
    )
    list_closed_parser.add_argument("--priority", help="Filter by priority (for example: P1, P2).")
    list_closed_parser.add_argument("--service", help="Filter by service name.")
    list_closed_parser.add_argument("--text", help="Filter by text search term.")
    list_closed_parser.add_argument(
        "--profile",
        choices=["pde", "PDE"],
        help="Optional query preset profile (for example: pde).",
    )
    list_closed_parser.add_argument(
        "--limit-per-page", type=int, default=100, help="Alerts per page while paging (default: 100)."
    )
    list_closed_parser.add_argument(
        "--max-pages", type=int, default=50, help="Safety cap on pages fetched (default: 50)."
    )

    get_parser = subparsers.add_parser(
        "get",
        help="Get details for one alert.",
        description="Fetch full detail payload for a single alert.",
    )
    get_parser.add_argument("alert_id", help="Alert ID.")

    note_parser = subparsers.add_parser(
        "note",
        help="Add a note to an alert.",
        description="Add an operator note/comment to an alert.",
    )
    note_parser.add_argument("alert_id", help="Alert ID.")
    note_parser.add_argument("--note", required=True, help="Note content.")

    ack_parser = subparsers.add_parser(
        "ack",
        help="Acknowledge an alert.",
        description="Acknowledge an alert, optionally including a note.",
    )
    ack_parser.add_argument("alert_id", help="Alert ID.")
    ack_parser.add_argument("--note", help="Optional acknowledgement note.")

    close_parser = subparsers.add_parser(
        "close",
        help="Close an alert.",
        description="Close an alert, optionally including a note.",
    )
    close_parser.add_argument("alert_id", help="Alert ID.")
    close_parser.add_argument("--note", help="Optional close note.")

    export_parser = subparsers.add_parser(
        "export-csv",
        help="Export alerts to CSV for downstream analysis.",
        description=(
            "Export alerts in a local-time window to CSV.\n"
            "Ack and close actor attribution is sourced from lifecycle logs."
        ),
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m cli export-csv --output ./out/alerts.csv --start 2026-06-08T09:00 --end 2026-07-02T09:00\n"
            "  python -m cli export-csv --output ./out/alerts_utc.csv --start 2026-06-08T16:00 --end 2026-07-02T16:00 --timezone UTC\n"
            "  python -m cli export-csv --output ./out/subset.csv --start 2026-06-08T09:00 --end 2026-07-02T09:00 --columns alert_id,opened_at_local,acked_by,time_to_ack_minutes,status,priority"
        ),
    )
    export_parser.add_argument("--output", required=True, help="Output CSV file path.")
    export_parser.add_argument("--query", help="Base query used to fetch alerts (overrides profile/default query).")
    export_parser.add_argument(
        "--profile",
        choices=["pde", "PDE"],
        help="Optional query preset profile (overrides config default_profile).",
    )
    export_parser.add_argument(
        "--start",
        required=True,
        help="Start datetime in local timezone, format YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM.",
    )
    export_parser.add_argument(
        "--end",
        required=True,
        help="End datetime in local timezone, format YYYY-MM-DDTHH:MM or YYYY-MM-DD HH:MM.",
    )
    export_parser.add_argument(
        "--timezone",
        default=None,
        choices=["MST", "UTC", "mst", "utc"],
        help=(
            "Timezone for --start and --end boundaries.\n"
            "Default resolution order: --timezone > config default_timezone > MST"
        ),
    )
    export_parser.add_argument(
        "--columns",
        help=(
            "Comma-separated list of CSV columns to include.\n"
            "Defaults to config default_csv_columns.\n"
            "Valid names: " + ", ".join(DEFAULT_CSV_COLUMNS)
        ),
    )

    return parser.parse_args()


def _print_human(command: str, result: Dict[str, Any]) -> None:
    if command in {"list", "list-closed"}:
        alerts = result.get("alerts", [])
        print(f"Fetched {len(alerts)} alert(s) for query: {result.get('query')}")
        if command == "list-closed":
            print(
                f"Window: {result.get('window_start')} to {result.get('window_end')} "
                f"(pages fetched: {result.get('pages_fetched')})"
            )
        for alert in alerts:
            alert_id = alert.get("id") or alert.get("alertId") or "unknown"
            priority = alert.get("priority", "unknown")
            status = alert.get("status", "unknown")
            service = (alert.get("service") or {}).get("name", "unknown-service")
            title = alert.get("title", "Untitled alert")
            print(f"- {alert_id} | {priority} | {status} | {service} | {title}")
        return

    if command == "get":
        alert = result.get("alert", {})
        alert_id = alert.get("id", "unknown")
        title = alert.get("title", "Untitled alert")
        print(f"{alert_id}: {title}")
        print(json.dumps(alert, indent=2, sort_keys=True))
        return

    if command in {"note", "ack", "close"}:
        print(f"{command} succeeded for alert: {result.get('alert_id')}")
        print(json.dumps(result, indent=2, sort_keys=True))
        return

    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> int:
    if load_dotenv:
        # Search from the current working directory upward (not from this
        # file's own location) — a project's .env has no fixed relationship
        # to where this CLI's source happens to live.
        load_dotenv(find_dotenv(usecwd=True))
    args = _parse_args()

    try:
        cfg = AppConfig.from_env(config_path=args.config)
        if not args.mock:
            cfg.validate_for_alert_fetch()

        api = JSMOpsAPI(config=cfg, mock_mode=args.mock)

        if args.command == "list":
            profile = _resolve_profile(args.profile, cfg.default_profile)
            base_query = _resolve_query(args.query, profile, cfg.alert_filter)
            result = api.list_alerts(
                query=base_query,
                limit=args.limit,
                cursor=args.cursor,
                status=args.status,
                priority=args.priority,
                service=args.service,
                text=args.text,
            )
        elif args.command == "list-closed":
            profile = _resolve_profile(args.profile, cfg.default_profile)
            base_query = _resolve_query(args.query, profile, cfg.alert_filter)
            start_dt = None
            end_dt = None
            if args.start:
                tz_name = args.timezone or cfg.default_timezone
                tz = _tz_from_name(tz_name)
                start_dt = _parse_window_dt(args.start).replace(tzinfo=tz)
                if args.end:
                    end_dt = _parse_window_dt(args.end).replace(tzinfo=tz)
            result = api.list_closed_alerts(
                since_days=args.since_days,
                start=start_dt,
                end=end_dt,
                query=base_query,
                priority=args.priority,
                service=args.service,
                text=args.text,
                limit_per_page=args.limit_per_page,
                max_pages=args.max_pages,
            )
        elif args.command == "get":
            result = api.get_alert(args.alert_id)
        elif args.command == "note":
            result = api.add_note(args.alert_id, args.note)
        elif args.command == "ack":
            result = api.acknowledge(args.alert_id, note=args.note)
        elif args.command == "close":
            result = api.close(args.alert_id, note=args.note)
        elif args.command == "export-csv":
            profile = _resolve_profile(args.profile, cfg.default_profile)
            base_query = _resolve_query(args.query, profile, cfg.alert_filter)
            tz_name = args.timezone or cfg.default_timezone
            tz = _tz_from_name(tz_name)
            start_local = _parse_window_dt(args.start).replace(tzinfo=tz)
            end_local = _parse_window_dt(args.end).replace(tzinfo=tz)
            if end_local < start_local:
                raise ValueError("--end must be greater than or equal to --start.")

            fieldnames = _resolve_columns(args.columns, cfg.default_csv_columns)
            unknown_columns = [name for name in fieldnames if name not in DEFAULT_CSV_COLUMNS]
            if unknown_columns:
                raise ValueError(
                    "Unknown column(s) in export selection: "
                    + ", ".join(unknown_columns)
                    + ". Valid columns: "
                    + ", ".join(DEFAULT_CSV_COLUMNS)
                )

            print("Fetching alerts for export...", flush=True)
            alerts = api.list_all_alerts(query=base_query, include_details=False)
            print(f"Fetched {len(alerts)} alert(s) before window filtering.", flush=True)

            window_alerts: list[dict[str, Any]] = []
            for alert in alerts:
                created_raw = alert.get("createdAt")
                if not created_raw:
                    continue
                created_utc = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
                created_local = created_utc.astimezone(tz)
                if created_local < start_local or created_local > end_local:
                    continue
                window_alerts.append(alert)

            print(f"Retained {len(window_alerts)} alert(s) in time window; enriching details...", flush=True)

            enriched_alerts: list[dict[str, Any]] = []
            for idx, alert in enumerate(window_alerts, start=1):
                alert_id = str(alert.get("id") or "").strip()
                if alert_id:
                    try:
                        detail_result = api.get_alert(alert_id)
                        detail_alert = detail_result.get("alert") if isinstance(detail_result, dict) else None
                        if isinstance(detail_alert, dict):
                            merged = dict(alert)
                            merged.update(detail_alert)
                            alert = merged
                    except Exception:
                        pass
                enriched_alerts.append(alert)
                if idx % 25 == 0:
                    print(f"Enriched {idx}/{len(window_alerts)} alert(s)...", flush=True)

            rows = []
            for index, alert in enumerate(enriched_alerts, start=1):
                created_raw = alert.get("createdAt")
                if not created_raw:
                    continue

                created_utc = datetime.fromisoformat(str(created_raw).replace("Z", "+00:00")).astimezone(timezone.utc)
                created_local = created_utc.astimezone(tz)
                if created_local < start_local or created_local > end_local:
                    continue

                alert_id = str(alert.get("id", ""))

                lifecycle = {
                    "ack_at": None,
                    "ack_actor": None,
                    "close_at": None,
                    "close_actor": None,
                }
                if alert_id:
                    lifecycle = api.get_lifecycle_events(alert_id=alert_id)

                ack_at_utc = lifecycle.get("ack_at")
                close_at_utc = lifecycle.get("close_at")

                ack_at_utc_s = ack_at_utc.isoformat() if isinstance(ack_at_utc, datetime) else ""
                ack_at_local_s = ack_at_utc.astimezone(tz).isoformat() if isinstance(ack_at_utc, datetime) else ""
                close_at_utc_s = close_at_utc.isoformat() if isinstance(close_at_utc, datetime) else ""
                close_at_local_s = close_at_utc.astimezone(tz).isoformat() if isinstance(close_at_utc, datetime) else ""

                ack_actor = str(lifecycle.get("ack_actor") or "")
                close_actor = str(lifecycle.get("close_actor") or "")
                ack_resolution = api.resolve_acknowledger_details(alert=alert, lifecycle_events=lifecycle)
                resolved_acknowledger = str(ack_resolution.get("acked_by") or "")
                human_first_touch_at = ack_resolution.get("human_first_touch_at")
                human_first_touch_at_utc_s = (
                    human_first_touch_at.isoformat() if isinstance(human_first_touch_at, datetime) else ""
                )
                human_first_touch_at_local = (
                    human_first_touch_at.astimezone(tz) if isinstance(human_first_touch_at, datetime) else None
                )
                human_first_touch_at_local_s = human_first_touch_at_local.isoformat() if human_first_touch_at_local else ""
                out_of_hours_ack_s = str(_is_out_of_hours(human_first_touch_at_local)).lower()

                rows.append(
                    {
                        "alert_id": alert_id,
                        "tiny_id": str(alert.get("tinyId") or ""),
                        "opened_at_utc": created_utc.isoformat(),
                        "opened_at_local": created_local.isoformat(),
                        "ack_at_utc": ack_at_utc_s,
                        "ack_at_local": ack_at_local_s,
                        "closed_at_utc": close_at_utc_s,
                        "closed_at_local": close_at_local_s,
                        "acked_by": resolved_acknowledger,
                        "picked_up_by_automation": str(bool(ack_resolution.get("picked_up_by_automation"))).lower(),
                        "ack_attribution_source": str(ack_resolution.get("ack_attribution_source") or ""),
                        "automation_ack_actor": str(ack_resolution.get("automation_ack_actor") or ""),
                        "human_first_touch_at_utc": human_first_touch_at_utc_s,
                        "human_first_touch_at_local": human_first_touch_at_local_s,
                        "out_of_hours_ack": out_of_hours_ack_s,
                        "closed_by": close_actor,
                        "time_to_ack_seconds": _duration_seconds(created_utc, ack_at_utc),
                        "time_to_ack_minutes": _duration_minutes(created_utc, ack_at_utc),
                        "time_to_close_seconds": _duration_seconds(created_utc, close_at_utc),
                        "time_to_close_minutes": _duration_minutes(created_utc, close_at_utc),
                        "created_at_utc": created_utc.isoformat(),
                        "created_at_local": created_local.isoformat(),
                        "status": str(alert.get("status") or ""),
                        "priority": str(alert.get("priority") or ""),
                        "acknowledged": str(bool(alert.get("acknowledged"))).lower(),
                        "ack_actor": ack_actor,
                        "service_name": str((alert.get("service") or {}).get("name") or ""),
                        "source": str(alert.get("source") or ""),
                        "responders": _stringify_list(alert.get("responders") or []),
                        "tags": _stringify_list(alert.get("tags") or []),
                        "description": str(alert.get("description") or ""),
                        "owner": str(alert.get("owner") or ""),
                        "message": str(alert.get("message") or alert.get("title") or ""),
                    }
                )

                if index % 25 == 0:
                    print(f"Processed {index}/{len(enriched_alerts)} alert(s)...", flush=True)

            rows.sort(key=lambda item: item["created_at_utc"])

            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", newline="", encoding="utf-8") as fp:
                writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(rows)

            print(
                "Exported "
                f"{len(rows)} alert(s) to {out_path} "
                f"for window {start_local.isoformat()} to {end_local.isoformat()} ({tz_name.upper()})."
            )
            return 0
        else:
            raise ValueError(f"Unknown command: {args.command}")

        if not result.get("success"):
            raise RuntimeError(result.get("error", "Operation failed."))

        if args.json:
            print(json.dumps(result, indent=2, sort_keys=True))
        else:
            _print_human(args.command, result)

        return 0
    except Exception as exc:
        print(f"JSM CLI failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
