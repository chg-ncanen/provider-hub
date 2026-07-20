import base64
import json
import time
from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:
    requests = None

DEFAULT_CLOUD_ID = "e9c4ecbc-1bf8-42f3-8aba-927fa85ccbe2"
DEFAULT_FILTER = 'responders:"PDE" AND status:open'


class _PermanentAPIError(RuntimeError):
    """A non-retryable API error (e.g. 401/403/404) — retrying wastes time
    since the same response is guaranteed every attempt."""


class JSMOpsAlertsTool:
    """Wrapper around JSM Ops alerts endpoints with a tool-friendly interface."""

    def __init__(
        self,
        cloud_id: str = DEFAULT_CLOUD_ID,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        filter_query: str = DEFAULT_FILTER,
        timeout_seconds: int = 20,
        max_retries: int = 3,
        mock_mode: bool = False,
    ) -> None:
        self.cloud_id = cloud_id
        self.base_url = f"https://api.atlassian.com/jsm/ops/api/{cloud_id}/v1/alerts"
        self.email = email
        self.api_token = api_token
        self.filter_query = filter_query
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.mock_mode = mock_mode
        self.session = requests.Session() if requests is not None else None

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.email and self.api_token:
            basic = base64.b64encode(f"{self.email}:{self.api_token}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {basic}"
        return headers

    def _validate_credentials(self) -> None:
        if self.mock_mode:
            return
        if not self.email or not self.api_token:
            raise ValueError(
                "Missing credentials. Set ATLASSIAN_EMAIL and ATLASSIAN_API_TOKEN."
            )

    def _extract_alerts(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        if isinstance(payload.get("values"), list):
            return payload["values"]
        if isinstance(payload.get("alerts"), list):
            return payload["alerts"]
        if isinstance(payload.get("data"), list):
            return payload["data"]
        if isinstance(payload.get("results"), list):
            return payload["results"]
        return []

    def _request(
        self,
        method: str,
        path: str = "",
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._validate_credentials()

        if requests is None:
            raise RuntimeError("requests package is not installed. Install dependencies to call JSM APIs.")

        if self.mock_mode:
            return self._mock_response(method=method, path=path, params=params, payload=payload)

        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=params,
                    json=payload,
                    timeout=self.timeout_seconds,
                )

                if response.status_code == 204:
                    return {"ok": True}

                body: Any
                try:
                    body = response.json()
                except ValueError:
                    body = {"raw": response.text}

                if response.status_code in (429, 500, 502, 503, 504):
                    excerpt = json.dumps(body)[:800]
                    raise RuntimeError(f"Transient JSM Ops API error {response.status_code}: {excerpt}")

                if response.status_code >= 400:
                    excerpt = json.dumps(body)[:800]
                    raise _PermanentAPIError(f"JSM Ops API error {response.status_code}: {excerpt}")

                return body if isinstance(body, dict) else {"data": body}
            except _PermanentAPIError:
                # Same request, same credentials, same alert ID — guaranteed
                # to fail identically every time, so retrying (with sleeps)
                # only adds latency without any chance of succeeding.
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))

        raise RuntimeError(f"JSM Ops request failed after retries: {last_error}")

    def get_tool_definitions(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "fetch_open_alerts",
                "description": "Fetch open alerts for PDE responders from JSM Ops.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Optional override query."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                        "cursor": {"type": "string"},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
            {
                "name": "get_alert_detail",
                "description": "Get full details for an alert.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "alert_id": {"type": "string"},
                    },
                    "required": ["alert_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "add_alert_note",
                "description": "Add a note to an alert.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "alert_id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["alert_id", "note"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "acknowledge_alert",
                "description": "Acknowledge an alert.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "alert_id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["alert_id"],
                    "additionalProperties": False,
                },
            },
            {
                "name": "close_alert",
                "description": "Close or resolve an alert.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "alert_id": {"type": "string"},
                        "note": {"type": "string"},
                    },
                    "required": ["alert_id"],
                    "additionalProperties": False,
                },
            },
        ]

    def execute_tool(self, name: str, tool_input: Dict[str, Any]) -> Dict[str, Any]:
        try:
            if name == "fetch_open_alerts":
                return self.fetch_open_alerts(
                    query=tool_input.get("query"),
                    limit=tool_input.get("limit"),
                    cursor=tool_input.get("cursor"),
                )
            if name == "get_alert_detail":
                return self.get_alert_detail(alert_id=tool_input["alert_id"])
            if name == "add_alert_note":
                return self.add_alert_note(alert_id=tool_input["alert_id"], note=tool_input["note"])
            if name == "acknowledge_alert":
                return self.acknowledge_alert(alert_id=tool_input["alert_id"], note=tool_input.get("note"))
            if name == "close_alert":
                return self.close_alert(alert_id=tool_input["alert_id"], note=tool_input.get("note"))
            return {
                "success": False,
                "error": f"Unknown tool: {name}",
                "tool": name,
            }
        except Exception as exc:
            return {
                "success": False,
                "error": str(exc),
                "tool": name,
            }

    def fetch_open_alerts(
        self,
        query: Optional[str] = None,
        limit: Optional[int] = None,
        cursor: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"query": query or self.filter_query}
        if limit is not None:
            params["limit"] = limit
        if cursor:
            params["cursor"] = cursor

        payload = self._request("GET", "", params=params)
        alerts = self._extract_alerts(payload)
        next_cursor = payload.get("nextCursor") or payload.get("nextPage") or payload.get("next")
        return {
            "success": True,
            "operation": "fetch_open_alerts",
            "query": params["query"],
            "count": len(alerts),
            "alerts": alerts,
            "next_cursor": next_cursor,
            "raw": payload,
        }

    def get_alert_detail(self, alert_id: str) -> Dict[str, Any]:
        payload = self._request("GET", f"/{alert_id}")
        return {
            "success": True,
            "operation": "get_alert_detail",
            "alert": payload,
        }

    def add_alert_note(self, alert_id: str, note: str) -> Dict[str, Any]:
        payload = self._request("POST", f"/{alert_id}/notes", payload={"note": note})
        return {
            "success": True,
            "operation": "add_alert_note",
            "alert_id": alert_id,
            "note": payload,
        }

    def acknowledge_alert(self, alert_id: str, note: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if note:
            body["note"] = note
        payload = self._request("POST", f"/{alert_id}/acknowledge", payload=body)
        return {
            "success": True,
            "operation": "acknowledge_alert",
            "alert_id": alert_id,
            "result": payload,
        }

    def close_alert(self, alert_id: str, note: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if note:
            body["note"] = note
        try:
            payload = self._request("POST", f"/{alert_id}/close", payload=body)
        except RuntimeError as first_error:
            fallback_payload = {"status": "closed"}
            if note:
                fallback_payload["note"] = note
            try:
                payload = self._request("POST", f"/{alert_id}/status", payload=fallback_payload)
            except Exception:
                raise first_error
        return {
            "success": True,
            "operation": "close_alert",
            "alert_id": alert_id,
            "result": payload,
        }

    def _mock_response(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]],
        payload: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        del params
        if method == "GET" and path == "":
            return {
                "values": [
                    {
                        "id": "alert-1001",
                        "title": "Payments API elevated 5xx rate",
                        "priority": "P1",
                        "status": "open",
                        "service": {"name": "payments-api"},
                        "createdAt": "2026-07-02T11:15:00Z",
                    },
                    {
                        "id": "alert-1002",
                        "title": "Checkout worker queue lag",
                        "priority": "P2",
                        "status": "open",
                        "service": {"name": "checkout-worker"},
                        "createdAt": "2026-07-02T11:22:00Z",
                    },
                    {
                        "id": "alert-1003",
                        "title": "Latency increase in auth service",
                        "priority": "P3",
                        "status": "open",
                        "service": {"name": "auth-service"},
                        "createdAt": "2026-07-02T11:30:00Z",
                    },
                ]
            }
        if method == "GET" and path.startswith("/"):
            alert_id = path.strip("/")
            return {
                "id": alert_id,
                "title": f"Mock detail for {alert_id}",
                "description": "Synthetic alert detail used in mock mode.",
                "priority": "P2",
                "status": "open",
                "responders": ["PDE"],
            }
        if method == "POST" and path.endswith("/notes"):
            alert_id = path.split("/")[1]
            return {
                "id": "note-1",
                "alert_id": alert_id,
                "content": (payload or {}).get("content", ""),
            }
        if method == "POST" and path.endswith("/acknowledge"):
            alert_id = path.split("/")[1]
            return {"id": alert_id, "status": "acknowledged"}
        if method == "POST" and (path.endswith("/close") or path.endswith("/status")):
            alert_id = path.split("/")[1]
            return {"id": alert_id, "status": "closed"}
        return {"ok": True}
