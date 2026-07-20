# pyproject.toml declares requires-python >=3.9, but this file uses PEP 604
# `X | Y` union syntax (e.g. `str | None`), which only evaluates at runtime on
# 3.10+ — deferring annotation evaluation keeps it working on 3.9.
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
import json


DEFAULT_CLOUD_ID = "e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2"
DEFAULT_ALERT_FILTER = "status:open"
DEFAULT_TIMEZONE = "MST"
DEFAULT_CSV_COLUMNS = [
    "alert_id",
    "tiny_id",
    "opened_at_utc",
    "opened_at_local",
    "ack_at_utc",
    "ack_at_local",
    "closed_at_utc",
    "closed_at_local",
    "acked_by",
    "picked_up_by_automation",
    "ack_attribution_source",
    "automation_ack_actor",
    "human_first_touch_at_utc",
    "human_first_touch_at_local",
    "out_of_hours_ack",
    "closed_by",
    "time_to_ack_seconds",
    "time_to_ack_minutes",
    "time_to_close_seconds",
    "time_to_close_minutes",
    "created_at_utc",
    "created_at_local",
    "status",
    "priority",
    "acknowledged",
    "ack_actor",
    "service_name",
    "source",
    "responders",
    "tags",
    "description",
    "owner",
    "message",
]
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "app_config.json"


@dataclass
class AppConfig:
    atlassian_cloud_id: str = DEFAULT_CLOUD_ID
    alert_filter: str = DEFAULT_ALERT_FILTER
    default_timezone: str = DEFAULT_TIMEZONE
    default_profile: str | None = None
    default_csv_columns: list[str] | None = None

    # Non-secret contact value: config or env
    atlassian_email: str | None = None

    # Secrets: env-only values
    atlassian_api_token: str | None = None

    # Runtime
    timeout_seconds: int = 20
    max_retries: int = 3

    @classmethod
    def from_env(cls, config_path: str | Path | None = None) -> "AppConfig":
        file_config = cls._load_file_config(config_path)
        email_from_file = file_config.get("atlassian_email")
        email_from_env = os.getenv("ATLASSIAN_EMAIL")
        default_columns = file_config.get("default_csv_columns", DEFAULT_CSV_COLUMNS)
        if not isinstance(default_columns, list) or not all(isinstance(c, str) for c in default_columns):
            raise ValueError("'default_csv_columns' must be an array of strings.")
        return cls(
            atlassian_cloud_id=str(file_config.get("atlassian_cloud_id", DEFAULT_CLOUD_ID)),
            alert_filter=str(file_config.get("alert_filter", DEFAULT_ALERT_FILTER)),
            default_timezone=str(file_config.get("default_timezone", DEFAULT_TIMEZONE)),
            default_profile=(str(file_config.get("default_profile")) if file_config.get("default_profile") is not None else None),
            default_csv_columns=list(default_columns),
            atlassian_email=(email_from_env if email_from_env else (str(email_from_file) if email_from_file is not None else None)),
            atlassian_api_token=os.getenv("ATLASSIAN_API_TOKEN"),
            timeout_seconds=int(file_config.get("timeout_seconds", 20)),
            max_retries=int(file_config.get("max_retries", 3)),
        )

    @staticmethod
    def _load_file_config(config_path: str | Path | None = None) -> dict:
        path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        if not path.exists():
            return {}

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in config file '{path}': {exc}") from exc

        if not isinstance(data, dict):
            raise ValueError(f"Config file '{path}' must contain a JSON object.")
        return data

    def validate_for_live_run(self) -> None:
        missing = []
        if not self.atlassian_email:
            missing.append("ATLASSIAN_EMAIL")
        if not self.atlassian_api_token:
            missing.append("ATLASSIAN_API_TOKEN")

        if missing:
            raise ValueError("Missing required values for live run: " + ", ".join(missing))

    def validate_for_alert_fetch(self) -> None:
        missing = []
        if not self.atlassian_email:
            missing.append("ATLASSIAN_EMAIL")
        if not self.atlassian_api_token:
            missing.append("ATLASSIAN_API_TOKEN")

        if missing:
            raise ValueError("Missing required values for alert fetch: " + ", ".join(missing))