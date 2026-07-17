import json
from typing import Any

import mcp.types as types


_EMAIL_TOOL_NAMES = {
    "find_emails",
    "send_email",
}


def definitions() -> list[types.Tool]:
    return [
        types.Tool(
            name="find_emails",
            description=(
                "Search for emails in a mailbox using IMAP. "
                "Filter by subject, sender, or date. Returns matching email subjects, senders, dates, and body text."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "description": "Search term to match in the email subject."},
                    "sender": {"type": "string", "description": "Filter by sender email address or name."},
                    "since": {"type": "string", "description": "Return emails since this date. Format: DD-Mon-YYYY e.g. '01-Jul-2026'."},
                    "mailbox": {"type": "string", "description": "Mailbox/folder to search (default: INBOX)."},
                    "limit": {"type": "integer", "description": "Max number of emails to return (default: 10)."},
                },
            },
        ),
        types.Tool(
            name="send_email",
            description=(
                "Send a plain-text email via SMTP. "
                "Use this to send notifications, alerts, or messages to recipients."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "to": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "Recipient email address(es).",
                    },
                    "subject": {"type": "string", "description": "Email subject line."},
                    "body": {"type": "string", "description": "Plain-text email body."},
                    "cc": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "CC recipient email address(es).",
                    },
                },
                "required": ["to", "subject", "body"],
            },
        ),
    ]


def can_handle(name: str) -> bool:
    return name in _EMAIL_TOOL_NAMES


def handle(name: str, arguments: dict[str, Any], email_tool: Any) -> dict[str, Any]:
    if name == "find_emails":
        return email_tool.find_emails(
            subject=arguments.get("subject"),
            sender=arguments.get("sender"),
            since=arguments.get("since"),
            mailbox=arguments.get("mailbox", "INBOX"),
            limit=arguments.get("limit", 10),
        )

    if name == "send_email":
        return email_tool.send_email(
            to=arguments["to"],
            subject=arguments["subject"],
            body=arguments["body"],
            cc=arguments.get("cc"),
        )

    raise ValueError(f"Unknown email tool: {name}")


def as_text_content(payload: dict[str, Any]) -> list[types.TextContent]:
    return [types.TextContent(type="text", text=json.dumps(payload, indent=2))]
