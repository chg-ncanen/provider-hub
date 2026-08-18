import base64
import json
import time
from typing import Any, Dict, List, Optional

try:
    import requests
except Exception:
    requests = None

from api.jsm.alerts_tool import DEFAULT_CLOUD_ID, _PermanentAPIError


class JSMOpsSchedulesTool:
    """Wrapper around JSM Ops schedule endpoints (rotations, on-call, overrides).

    PDE refers to this rotation as "ASA" (App Support Ambassador); the
    underlying JSM Ops / Opsgenie API calls the same concept "on-call" —
    this class mirrors the API's own naming since it maps 1:1 to real
    endpoint paths, while callers (api.jsm.client.JSMOpsAPI) expose it
    under the "ASA" name PDE actually uses.
    """

    def __init__(
        self,
        cloud_id: str = DEFAULT_CLOUD_ID,
        email: Optional[str] = None,
        api_token: Optional[str] = None,
        timeout_seconds: int = 20,
        max_retries: int = 3,
        mock_mode: bool = False,
    ) -> None:
        self.cloud_id = cloud_id
        self.base_url = f"https://api.atlassian.com/jsm/ops/api/{cloud_id}/v1/schedules"
        self.jira_base_url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
        self.email = email
        self.api_token = api_token
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

    def _request(
        self,
        method: str,
        path: str = "",
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        self._validate_credentials()

        if requests is None:
            raise RuntimeError("requests package is not installed. Install dependencies to call JSM APIs.")

        if self.mock_mode:
            return self._mock_response(path=path)

        url = f"{self.base_url}{path}"
        last_error: Optional[Exception] = None

        for attempt in range(1, self.max_retries + 1):
            try:
                response = self.session.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=params,
                    timeout=self.timeout_seconds,
                )

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
                raise
            except Exception as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2 ** (attempt - 1), 8))

        raise RuntimeError(f"JSM Ops request failed after retries: {last_error}")

    def list_schedules(self) -> Dict[str, Any]:
        payload = self._request("GET", "")
        schedules = payload.get("values", []) if isinstance(payload, dict) else []
        return {
            "success": True,
            "operation": "list_schedules",
            "count": len(schedules),
            "schedules": schedules,
        }

    def get_on_calls(
        self,
        schedule_id: str,
        date: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if date:
            params["date"] = date
        payload = self._request("GET", f"/{schedule_id}/on-calls", params=params)
        participants = payload.get("onCallParticipants", []) if isinstance(payload, dict) else []
        return {
            "success": True,
            "operation": "get_on_calls",
            "schedule_id": schedule_id,
            "date": date,
            "participants": participants,
        }

    def get_timeline(
        self,
        schedule_id: str,
        date: Optional[str] = None,
        interval: Optional[int] = None,
        interval_unit: Optional[str] = None,
        expand: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        if date:
            params["date"] = date
        if interval is not None:
            params["interval"] = interval
        if interval_unit:
            params["intervalUnit"] = interval_unit
        if expand:
            params["expand"] = expand
        payload = self._request("GET", f"/{schedule_id}/timeline", params=params)
        return {
            "success": True,
            "operation": "get_timeline",
            "schedule_id": schedule_id,
            "timeline": payload,
        }

    def list_overrides(self, schedule_id: str) -> Dict[str, Any]:
        payload = self._request("GET", f"/{schedule_id}/overrides")
        overrides = payload.get("values", []) if isinstance(payload, dict) else []
        return {
            "success": True,
            "operation": "list_overrides",
            "schedule_id": schedule_id,
            "overrides": overrides,
        }

    def resolve_user_display(self, account_id: str) -> Dict[str, Optional[str]]:
        # Schedule/on-call/override responses only carry Atlassian accountIds
        # (no name or email) — resolve via the same Jira Cloud user API that
        # JSMOpsAlertsTool._resolve_account_id uses in the opposite direction
        # (email -> accountId). Best-effort: a deactivated or deleted account
        # shouldn't blow up an otherwise-successful ASA lookup, so failures
        # degrade to a bare accountId rather than raising.
        if self.mock_mode:
            return self._mock_user_display(account_id)

        try:
            response = self.session.get(
                f"{self.jira_base_url}/user",
                headers=self._headers(),
                params={"accountId": account_id},
                timeout=self.timeout_seconds,
            )
            if response.status_code >= 400:
                return {"account_id": account_id, "display_name": None, "email": None}
            data = response.json()
            return {
                "account_id": account_id,
                "display_name": data.get("displayName"),
                "email": data.get("emailAddress"),
            }
        except Exception:
            return {"account_id": account_id, "display_name": None, "email": None}

    def _mock_user_display(self, account_id: str) -> Dict[str, Optional[str]]:
        directory = {
            "mock-account-1": {"account_id": "mock-account-1", "display_name": "Mock User One", "email": "mock.user.one@example.com"},
            "mock-account-2": {"account_id": "mock-account-2", "display_name": "Mock User Two", "email": "mock.user.two@example.com"},
        }
        return directory.get(
            account_id,
            {"account_id": account_id, "display_name": f"Mock User ({account_id})", "email": None},
        )

    def _mock_response(self, path: str) -> Dict[str, Any]:
        if path == "":
            return {
                "values": [
                    {
                        "id": "mock-schedule-1",
                        "name": "PDE - Mock ASA Schedule",
                        "enabled": True,
                        "timezone": "America/Denver",
                    },
                    {
                        "id": "mock-schedule-2",
                        "name": "PDE - Mock ASA Schedule Two",
                        "enabled": True,
                        "timezone": "America/Denver",
                    },
                ]
            }
        if path.endswith("/on-calls"):
            return {"onCallParticipants": [{"id": "mock-account-1", "type": "user"}]}
        if path.endswith("/overrides"):
            return {"values": []}
        if path.endswith("/timeline"):
            return {
                "startDate": "2026-01-01T00:00:00Z",
                "endDate": "2026-01-29T00:00:00Z",
                "finalTimeline": {
                    "rotations": [
                        {
                            "id": "mock-rotation-1",
                            "name": "Mock Rotation",
                            "periods": [
                                {
                                    "startDate": "2026-01-01T00:00:00Z",
                                    "endDate": "2026-01-15T00:00:00Z",
                                    "type": "historical",
                                    "responder": {"id": "mock-account-1", "type": "user"},
                                },
                                {
                                    "startDate": "2026-01-15T00:00:00Z",
                                    "endDate": "2026-01-29T00:00:00Z",
                                    "type": "historical",
                                    "responder": {"id": "mock-account-2", "type": "user"},
                                },
                            ],
                        }
                    ]
                },
                "overrideTimeline": {"rotations": []},
            }
        return {"ok": True}
