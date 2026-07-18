"""Email tools: IMAP (search/read) and SMTP (send) via standard protocols."""

import imaplib
import json
import os
import smtplib
import email as email_lib
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import parsedate_to_datetime
from typing import Any, Optional


def _imap_conn(host: str, port: int, username: str, password: str) -> imaplib.IMAP4_SSL:
    conn = imaplib.IMAP4_SSL(host, port)
    conn.login(username, password)
    return conn


def _parse_message(raw: bytes) -> dict[str, Any]:
    msg = email_lib.message_from_bytes(raw)
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get("Content-Disposition"):
                payload = part.get_payload(decode=True)
                if payload:
                    body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                    break
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

    date_str = msg.get("Date", "")
    try:
        date_iso = parsedate_to_datetime(date_str).isoformat() if date_str else ""
    except Exception:
        date_iso = date_str

    return {
        "subject": msg.get("Subject", ""),
        "from": msg.get("From", ""),
        "to": msg.get("To", ""),
        "date": date_iso,
        "body": body.strip(),
    }


class EmailTool:
    """IMAP/SMTP email tool with find and send operations."""

    def __init__(
        self,
        imap_host: Optional[str] = None,
        imap_port: int = 993,
        smtp_host: Optional[str] = None,
        smtp_port: int = 587,
        username: Optional[str] = None,
        password: Optional[str] = None,
    ) -> None:
        self.imap_host = imap_host or os.getenv("EMAIL_IMAP_HOST", "imap.gmail.com")
        self.imap_port = imap_port
        self.smtp_host = smtp_host or os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
        self.smtp_port = smtp_port
        self.username = username or os.getenv("EMAIL_USERNAME", "")
        self.password = password or os.getenv("EMAIL_PASSWORD", "")

    def find_emails(
        self,
        subject: Optional[str] = None,
        sender: Optional[str] = None,
        since: Optional[str] = None,
        mailbox: str = "INBOX",
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search for emails matching criteria. Returns list of matching emails."""
        criteria: list[str] = []
        if subject:
            criteria.append(f'SUBJECT "{subject}"')
        if sender:
            criteria.append(f'FROM "{sender}"')
        if since:
            # Expects "DD-Mon-YYYY" e.g. "01-Jul-2026"
            criteria.append(f'SINCE "{since}"')
        if not criteria:
            criteria.append("ALL")

        search_str = " ".join(criteria)

        conn = _imap_conn(self.imap_host, self.imap_port, self.username, self.password)
        try:
            conn.select(mailbox, readonly=True)
            _, data = conn.search(None, search_str)
            ids = data[0].split()
            ids = ids[-limit:]  # most recent N

            results = []
            for uid in reversed(ids):
                _, msg_data = conn.fetch(uid, "(RFC822)")
                if msg_data and msg_data[0]:
                    raw = msg_data[0][1]
                    if isinstance(raw, bytes):
                        results.append(_parse_message(raw))
        finally:
            try:
                conn.logout()
            except Exception:
                pass

        return {"count": len(results), "emails": results}

    def send_email(
        self,
        to: str | list[str],
        subject: str,
        body: str,
        cc: Optional[str | list[str]] = None,
    ) -> dict[str, Any]:
        """Send a plain-text email via SMTP."""
        msg = EmailMessage()
        msg["From"] = self.username
        msg["To"] = ", ".join(to) if isinstance(to, list) else to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = ", ".join(cc) if isinstance(cc, list) else cc
        msg.set_content(body)

        with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.send_message(msg)

        return {"success": True, "to": msg["To"], "subject": subject}
