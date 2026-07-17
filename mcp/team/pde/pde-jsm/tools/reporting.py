import json
import re
import subprocess
from pathlib import Path
from typing import Any

import mcp.types as types

from api.jsmreport.reporter import ReportComposer


_REPORTING_TOOL_NAMES = {
    "summarize_alerts_csv",
    "run_reporting_pipeline",
}


def definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="summarize_alerts_csv",
            description=(
                "Build a PDE alert report from exported CSV artifacts. "
                "The deterministic stats section is always included. "
                "Backend AI grouping is disabled in this architecture."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "input": {
                        "type": "string",
                        "description": "Path to stats CSV input.",
                    },
                    "summary_input": {
                        "type": "string",
                        "description": "Optional alternate CSV input path for report composition.",
                    },
                },
                "required": ["input"],
            },
        ),
        types.Tool(
            name="run_reporting_pipeline",
            description=(
                "Run the PDE reporting pipeline end-to-end and generate report artifacts. "
                "Deterministic-only mode is enabled by default to avoid AI summary generation."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "start": {
                        "type": "string",
                        "description": "Window start, format YYYY-MM-DDTHH:MM.",
                    },
                    "end": {
                        "type": "string",
                        "description": "Window end, format YYYY-MM-DDTHH:MM.",
                    },
                    "timezone": {
                        "type": "string",
                        "description": "Window timezone, MST or UTC (default MST).",
                    },
                    "output_dir": {
                        "type": "string",
                        "description": "Optional output folder override.",
                    },
                    "basename": {
                        "type": "string",
                        "description": "Optional artifact filename prefix.",
                    },
                    "stats_profile": {
                        "type": "string",
                        "description": "Optional stats export profile.",
                    },
                    "summary_profile": {
                        "type": "string",
                        "description": "Optional summary export profile.",
                    },
                    "stats_query": {
                        "type": "string",
                        "description": "Optional explicit stats query.",
                    },
                    "summary_query": {
                        "type": "string",
                        "description": "Optional explicit summary query.",
                    },
                },
                "required": ["start", "end"],
            },
        ),
    ]


def can_handle(name: str) -> bool:
    return name in _REPORTING_TOOL_NAMES


def _run_reporting_pipeline(project_root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    script_path = project_root / "api" / "jsmreport" / "run_reporting_pipeline.sh"
    
    # Validate required arguments
    if "start" not in arguments or "end" not in arguments:
        return {
            "success": False,
            "error": "Missing required arguments: start and end",
            "return_code": 1,
        }
    
    # Validate datetime format (YYYY-MM-DDTHH:MM)
    datetime_pattern = re.compile(r'^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$')
    if not datetime_pattern.match(arguments["start"]):
        return {
            "success": False,
            "error": f"Invalid start format. Expected YYYY-MM-DDTHH:MM, got: {arguments['start']}",
            "return_code": 1,
        }
    if not datetime_pattern.match(arguments["end"]):
        return {
            "success": False,
            "error": f"Invalid end format. Expected YYYY-MM-DDTHH:MM, got: {arguments['end']}",
            "return_code": 1,
        }
    
    # Validate timezone
    allowed_timezones = {"MST", "UTC"}
    tz = arguments.get("timezone", "MST")
    if tz not in allowed_timezones:
        return {
            "success": False,
            "error": f"Invalid timezone. Expected one of {allowed_timezones}, got: {tz}",
            "return_code": 1,
        }
    
    command = [
        "bash",
        str(script_path),
        "--start",
        arguments["start"],
        "--end",
        arguments["end"],
        "--timezone",
        tz,
    ]
    if arguments.get("output_dir"):
        command.extend(["--output-dir", arguments["output_dir"]])
    if arguments.get("basename"):
        command.extend(["--basename", arguments["basename"]])
    if arguments.get("stats_profile"):
        command.extend(["--stats-profile", arguments["stats_profile"]])
    if arguments.get("summary_profile"):
        command.extend(["--summary-profile", arguments["summary_profile"]])
    if arguments.get("stats_query"):
        command.extend(["--stats-query", arguments["stats_query"]])
    if arguments.get("summary_query"):
        command.extend(["--summary-query", arguments["summary_query"]])

    try:
        completed = subprocess.run(
            command,
            text=True,
            capture_output=True,
            check=False,
            cwd=str(project_root),
            timeout=300,  # 5 minute timeout
        )
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "error": "Reporting pipeline timed out after 300 seconds",
            "return_code": 124,
            "command": command,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Subprocess error: {str(e)}",
            "return_code": 1,
            "command": command,
        }

    return {
        "success": completed.returncode == 0,
        "return_code": completed.returncode,
        "command": command,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def handle(name: str, arguments: dict[str, Any], project_root: Path) -> dict[str, Any]:
    if name == "summarize_alerts_csv":
        return ReportComposer.build_report(
            input_path=arguments["input"],
            summary_input_path=arguments.get("summary_input"),
        )

    if name == "run_reporting_pipeline":
        return _run_reporting_pipeline(project_root=project_root, arguments=arguments)

    raise ValueError(f"Unknown reporting tool: {name}")


def as_text_content(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]
