import json
from typing import Any

import mcp.types as types


_ASA_TOOL_NAMES = {
    "list_asa_schedules",
    "get_current_asa",
    "get_asa_timeline",
    "list_asa_overrides",
}


def definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="list_asa_schedules",
            description=(
                "List PDE's ASA (App Support Ambassador) rotation schedules from JSM Ops. "
                "ASA is PDE's name for what JSM/Opsgenie calls \"on-call\" — the two terms mean "
                "the same thing here, so use this tool whether the user says 'ASA schedules' or "
                "'on-call schedules' or 'on-call rotations'."
            ),
            inputSchema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_current_asa",
            description=(
                "Get who is on ASA (App Support Ambassador) — PDE's name for on-call — for a given "
                "schedule, at a specific point in time. Defaults to right now. Use this whenever the "
                "user asks who is on-call, who is on ASA, who is ASA right now, or who will be on "
                "ASA/on-call at a future or past date/time — 'on-call' and 'ASA' are the same thing at PDE."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule": {
                        "type": "string",
                        "description": (
                            "Schedule name or partial name (e.g. 'Team 3', 'Release Manager'), "
                            "or the schedule's UUID."
                        ),
                    },
                    "date": {
                        "type": "string",
                        "description": "ISO 8601 timestamp to check (defaults to now), e.g. '2026-08-19T00:00:00Z'.",
                    },
                },
                "required": ["schedule"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="get_asa_timeline",
            description=(
                "Get the ASA (App Support Ambassador / on-call) rotation history for a schedule over "
                "a time window, including any manual substitutions (overrides). Use this to find who "
                "was on ASA/on-call in the past, who is scheduled next, or whether a substitution "
                "happened — 'ASA' and 'on-call' refer to the same rotation at PDE."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule": {
                        "type": "string",
                        "description": "Schedule name or partial name, or the schedule's UUID.",
                    },
                    "date": {
                        "type": "string",
                        "description": "ISO 8601 window start (defaults to ~now minus the interval).",
                    },
                    "interval": {
                        "type": "integer",
                        "description": "Window length, in interval_unit units (default 4).",
                    },
                    "interval_unit": {
                        "type": "string",
                        "enum": ["days", "weeks", "months"],
                        "description": "Unit for interval (default 'weeks').",
                    },
                    "include_overrides": {
                        "type": "boolean",
                        "description": "Whether to flag manual substitutions separately (default true).",
                    },
                },
                "required": ["schedule"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name="list_asa_overrides",
            description=(
                "List active/upcoming manual substitutions (overrides) on a PDE ASA (on-call) "
                "schedule — cases where someone is covering ASA/on-call outside the normal rotation. "
                "Use this whether the user says 'ASA substitution' or 'on-call override'."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "schedule": {
                        "type": "string",
                        "description": "Schedule name or partial name, or the schedule's UUID.",
                    },
                },
                "required": ["schedule"],
                "additionalProperties": False,
            },
        ),
    ]


def can_handle(name: str) -> bool:
    return name in _ASA_TOOL_NAMES


def handle(name: str, arguments: dict[str, Any], api: Any) -> dict[str, Any]:
    if name == "list_asa_schedules":
        return api.list_asa_schedules()

    if name == "get_current_asa":
        return api.get_current_asa(schedule=arguments["schedule"], date=arguments.get("date"))

    if name == "get_asa_timeline":
        return api.get_asa_timeline(
            schedule=arguments["schedule"],
            date=arguments.get("date"),
            interval=arguments.get("interval", 4),
            interval_unit=arguments.get("interval_unit", "weeks"),
            include_overrides=arguments.get("include_overrides", True),
        )

    if name == "list_asa_overrides":
        return api.list_asa_overrides(schedule=arguments["schedule"])

    raise ValueError(f"Unknown ASA tool: {name}")


def as_text_content(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]
