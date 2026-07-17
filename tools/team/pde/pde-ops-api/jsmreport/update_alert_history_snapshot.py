from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api.jsm.config import AppConfig
from api.jsm.client import JSMOpsAPI


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_local_window_end(value: str, tz_token: str) -> datetime:
    base = datetime.fromisoformat(value)
    token = tz_token.strip().upper()
    if token == "MST":
        return (base + timedelta(hours=7)).replace(tzinfo=timezone.utc)
    if token == "UTC":
        return base.replace(tzinfo=timezone.utc)
    raise ValueError(f"Unsupported timezone token for refresh: {tz_token}")


def _escape_query_value(value: str) -> str:
    return value.replace('"', '\\"')


def _build_query(base_query: str, patterns: List[str]) -> str:
    safe_patterns = [p.strip() for p in patterns if p and p.strip()]
    if not safe_patterns:
        return base_query
    if len(safe_patterns) == 1:
        return f'{base_query} AND message:"{_escape_query_value(safe_patterns[0])}"'
    joined = " OR ".join(f'message:"{_escape_query_value(p)}"' for p in safe_patterns)
    return f"{base_query} AND ({joined})"


def _load_snapshot(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise ValueError(f"Snapshot file does not exist: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Snapshot content must be a JSON object")
    entries = payload.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Snapshot must contain an 'entries' array")
    return payload


def _refresh_snapshot(
    api: JSMOpsAPI,
    snapshot: Dict[str, Any],
    through_utc: datetime,
    max_pages: int,
) -> Dict[str, Any]:
    entries = snapshot.get("entries") or []
    if not isinstance(entries, list):
        return snapshot

    base_query = str(snapshot.get("base_query") or 'responders:"PDE"')
    covered = _parse_iso_datetime(snapshot.get("covered_through_utc") or snapshot.get("generated_at"))
    if covered is None:
        raise ValueError("Snapshot missing valid covered_through_utc/generated_at timestamp")

    if through_utc <= covered:
        snapshot["last_refreshed_at_utc"] = datetime.now(timezone.utc).isoformat()
        snapshot["last_delta_from_utc"] = covered.isoformat()
        snapshot["last_delta_to_utc"] = through_utc.isoformat()
        snapshot["last_delta_total_added"] = 0
        return snapshot

    total_added = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue

        patterns = entry.get("title_patterns") or []
        if not isinstance(patterns, list) or not patterns:
            entry["last_delta_added"] = 0
            continue

        query = _build_query(base_query=base_query, patterns=[str(p) for p in patterns])
        alerts = api.list_all_alerts(query=query, include_details=False, max_pages=max_pages)

        delta_count = 0
        for alert in alerts:
            created_at = _parse_iso_datetime(alert.get("createdAt") or alert.get("created_at"))
            if created_at is None:
                continue
            if covered < created_at <= through_utc:
                delta_count += 1

        current = entry.get("historical_count")
        try:
            current_count = int(current)
        except (TypeError, ValueError):
            current_count = 0

        entry["historical_count"] = current_count + delta_count
        entry["last_delta_added"] = delta_count
        total_added += delta_count

    snapshot["covered_through_utc"] = through_utc.isoformat()
    snapshot["last_refreshed_at_utc"] = datetime.now(timezone.utc).isoformat()
    snapshot["last_delta_from_utc"] = covered.isoformat()
    snapshot["last_delta_to_utc"] = through_utc.isoformat()
    snapshot["last_delta_total_added"] = total_added
    return snapshot


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Incrementally refresh alert history snapshot.")
    parser.add_argument(
        "--snapshot",
        default="./agents/reporting/alert_history_snapshot.json",
        help="Path to alert history snapshot JSON file.",
    )
    parser.add_argument(
        "--through",
        required=True,
        help="Inclusive local window end (example: 2026-07-06T09:00).",
    )
    parser.add_argument(
        "--timezone",
        default="MST",
        help="Timezone token for --through (MST or UTC).",
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=120,
        help="Maximum pages per alert query while applying delta updates.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    snapshot_path = Path(args.snapshot)

    try:
        through_utc = _parse_local_window_end(args.through, args.timezone)
        cfg = AppConfig.from_env()
        api = JSMOpsAPI(config=cfg)

        snapshot = _load_snapshot(snapshot_path)
        updated = _refresh_snapshot(api=api, snapshot=snapshot, through_utc=through_utc, max_pages=args.max_pages)

        snapshot_path.write_text(json.dumps(updated, indent=2), encoding="utf-8")
        print(f"Updated history snapshot: {snapshot_path}")
        print(
            "Delta window: "
            f"{updated.get('last_delta_from_utc')} -> {updated.get('last_delta_to_utc')} "
            f"(added={updated.get('last_delta_total_added', 0)})"
        )
        return 0
    except Exception as exc:
        print(f"History snapshot refresh failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
