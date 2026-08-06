import json
from datetime import datetime
from typing import Any

import mcp.types as types


_ALERT_TOOL_NAMES = {
    "list_alerts",
    "list_closed_alerts",
    "get_alert",
    "acknowledge_alert",
    "close_alert",
    "add_alert_note",
}


def _alert_summary(alert: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": alert.get("id", ""),
        "tiny_id": alert.get("tinyId", ""),
        "message": alert.get("message") or alert.get("title") or "",
        "status": alert.get("status", ""),
        "priority": alert.get("priority", ""),
        "acknowledged": alert.get("acknowledged", False),
        "created_at": alert.get("createdAt", ""),
        "updated_at": alert.get("updatedAt", ""),
        "source": alert.get("source", ""),
        "owner": alert.get("owner", ""),
        "responders": alert.get("responders", []),
        "tags": alert.get("tags", []),
        "description": alert.get("description", ""),
    }


def definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_alerts",
            description=(
                "List PDE production alerts from JSM Ops (Jira Service Management). "
                "Use this whenever the user asks about open alerts, production issues, "
                "on-call alerts, or JSM alerts for the PDE (Provider Digital Experience) team. "
                "Defaults to the PDE responder filter. "
                "Optionally filter by status (e.g. 'open', 'acknowledged'), priority (e.g. 'P1', 'P2'), "
                "service, or free-text search."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "profile": {
                        "type": "string",
                        "enum": ["pde"],
                        "description": "Query preset profile. Use 'pde' for PDE alerts (responders:PDE).",
                    },
                    "status": {
                        "type": "string",
                        "description": "Filter by status, e.g. 'open', 'acknowledged'.",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Filter by priority, e.g. 'P1', 'P2'.",
                    },
                    "service": {
                        "type": "string",
                        "description": "Filter by service name.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Free-text search in alert message.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of alerts to return (default: 50).",
                    },
                },
            },
        ),
        types.Tool(
            name="list_closed_alerts",
            description=(
                "List closed PDE production alerts from JSM Ops. Use this when the user asks "
                "about resolved/closed alerts, alert history, or what closed in the past N days. "
                "Unlike list_alerts, a time window is required (since_days, or start/optionally end) "
                "because closed-alert history is unbounded and would otherwise mean pulling the entire archive."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "since_days": {
                        "type": "number",
                        "description": "Look back this many days from now, e.g. 2 for 'past 2 days'.",
                    },
                    "start": {
                        "type": "string",
                        "description": "ISO 8601 window start (alternative to since_days), e.g. '2026-07-20T00:00:00Z'.",
                    },
                    "end": {
                        "type": "string",
                        "description": "ISO 8601 window end (defaults to now if start is given without end).",
                    },
                    "profile": {
                        "type": "string",
                        "enum": ["pde"],
                        "description": "Query preset profile. Use 'pde' for PDE alerts (responders:PDE).",
                    },
                    "priority": {
                        "type": "string",
                        "description": "Filter by priority, e.g. 'P1', 'P2'.",
                    },
                    "service": {
                        "type": "string",
                        "description": "Filter by service name.",
                    },
                    "text": {
                        "type": "string",
                        "description": "Free-text search in alert message.",
                    },
                },
                "anyOf": [
                    {"required": ["since_days"]},
                    {"required": ["start"]},
                ],
            },
        ),
        types.Tool(
            name="get_alert",
            description=(
                "Get full details for a single PDE JSM Ops alert by its ID or tinyId. "
                "Use this to drill into a specific alert after listing alerts."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "alert_id": {
                        "type": "string",
                        "description": "The alert UUID or tinyId.",
                    },
                },
                "required": ["alert_id"],
            },
        ),
        types.Tool(
            name="acknowledge_alert",
            description=(
                "Acknowledge a PDE JSM Ops alert to signal it is being actively looked at. "
                "Use this when the user wants to ack or acknowledge an alert. Optionally include a note."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string", "description": "The alert UUID."},
                    "note": {"type": "string", "description": "Optional acknowledgement note."},
                },
                "required": ["alert_id"],
            },
        ),
        types.Tool(
            name="close_alert",
            description=(
                "Close/resolve a PDE JSM Ops alert once the issue is addressed. "
                "Use this when the user wants to close or resolve an alert. Optionally include a note."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string", "description": "The alert UUID."},
                    "note": {"type": "string", "description": "Optional close note."},
                },
                "required": ["alert_id"],
            },
        ),
        types.Tool(
            name="add_alert_note",
            description=(
                "Add an operator note or comment to a PDE JSM Ops alert. "
                "Use this when the user wants to add context, updates, or observations to an alert."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "alert_id": {"type": "string", "description": "The alert UUID."},
                    "note": {"type": "string", "description": "Note content to add."},
                },
                "required": ["alert_id", "note"],
            },
        ),
    ]


def can_handle(name: str) -> bool:
    return name in _ALERT_TOOL_NAMES


def _resolve_profile_query(profile: str | None, api_config: Any, default_query: str) -> str:
    # api_config.default_profile ("pde") is what the tool description promises
    # ("Defaults to the PDE responder filter") when the caller omits `profile`
    # — and when the "pde" profile does apply, AND it onto the configured base
    # filter (e.g. status:open) rather than replacing it, so "PDE alerts"
    # doesn't silently drop that scoping.
    resolved_profile = profile or api_config.default_profile
    if resolved_profile != "pde":
        return default_query
    return f'{default_query} AND responders:"PDE"' if default_query else 'responders:"PDE"'


def handle(name: str, arguments: dict[str, Any], api: Any) -> dict[str, Any]:
    if name == "list_alerts":
        base_query = _resolve_profile_query(arguments.get("profile"), api.config, api.config.alert_filter)
        result = api.list_alerts(
            query=base_query,
            status=arguments.get("status"),
            priority=arguments.get("priority"),
            service=arguments.get("service"),
            text=arguments.get("text"),
            limit=arguments.get("limit", 50),
        )
        alerts = result.get("alerts", [])
        summaries = [_alert_summary(a) for a in alerts]
        return {"count": len(summaries), "query": result.get("query"), "alerts": summaries}

    if name == "list_closed_alerts":
        base_query = _resolve_profile_query(arguments.get("profile"), api.config, api.config.alert_filter)
        start_raw = arguments.get("start")
        end_raw = arguments.get("end")
        start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00")) if start_raw else None
        end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00")) if end_raw else None
        result = api.list_closed_alerts(
            since_days=arguments.get("since_days"),
            start=start_dt,
            end=end_dt,
            query=base_query,
            priority=arguments.get("priority"),
            service=arguments.get("service"),
            text=arguments.get("text"),
        )
        alerts_list = result.get("alerts", [])
        summaries = [_alert_summary(a) for a in alerts_list]
        return {
            "count": len(summaries),
            "query": result.get("query"),
            "window_start": result.get("window_start"),
            "window_end": result.get("window_end"),
            "alerts": summaries,
        }

    if name == "get_alert":
        return api.get_alert(arguments["alert_id"])

    if name == "acknowledge_alert":
        return api.acknowledge(arguments["alert_id"], note=arguments.get("note"))

    if name == "close_alert":
        return api.close(arguments["alert_id"], note=arguments.get("note"))

    if name == "add_alert_note":
        return api.add_note(arguments["alert_id"], arguments["note"])

    raise ValueError(f"Unknown alert tool: {name}")


def as_text_content(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]
