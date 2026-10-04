"""Digest delivery — plain code, no LLM, no MCP: sends the composed digest via Gmail.

Deliberately outside the agent/MCP pattern used elsewhere in this project — there's no
judgment call here, just "take this text and send it," so a direct API call is simpler
and doesn't need a tool-use loop. Uses its own OAuth token, scoped to gmail.send only —
separate from the read-only token the sub-agents use (mcp_servers/gmail_server.py), so
this code can never read mail, only send. Same Google Cloud OAuth client as the read-only
setup, just a different scope and a separate cached token file — see this module's
one-time setup note below.

One-time setup (you do this, not this code):
1. Same OAuth consent screen as gmail_server.py — add the gmail.send scope alongside
   gmail.readonly (Data Access / Scopes -> Add or remove scopes -> Gmail API).
2. First call to send_digest() opens a browser for a separate consent (send-only),
   caching its own token at GMAIL_SEND_TOKEN_PATH.
"""

from __future__ import annotations

import base64
import datetime
import os
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

if TYPE_CHECKING:
    from personal_agent.orchestrator import DigestRun

SCOPES = ["https://www.googleapis.com/auth/gmail.send"]

_DEFAULT_CREDENTIALS_PATH = os.path.join(os.getcwd(), ".secrets", "gmail_credentials.json")
_DEFAULT_TOKEN_PATH = os.path.join(os.getcwd(), ".secrets", "gmail_send_token.json")
CREDENTIALS_PATH = os.environ.get("GMAIL_OAUTH_CREDENTIALS_PATH", _DEFAULT_CREDENTIALS_PATH)
TOKEN_PATH = os.environ.get("GMAIL_SEND_TOKEN_PATH", _DEFAULT_TOKEN_PATH)

DIGEST_RECIPIENT = os.environ.get("DIGEST_RECIPIENT")
DIGEST_SUBJECT_PREFIX = os.environ.get("DIGEST_SUBJECT_PREFIX", "Morning Digest")

_service = None

def _get_credentials() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_PATH):
        creds = Credentials.from_authorized_user_file(TOKEN_PATH, SCOPES)

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())

    if not creds or not creds.valid:
        if not os.path.exists(CREDENTIALS_PATH):
            raise FileNotFoundError(
                f"No Gmail OAuth credentials at {CREDENTIALS_PATH}. See this file's "
                "module docstring for the one-time Google Cloud Console setup."
            )
        flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_PATH, SCOPES)
        creds = flow.run_local_server(port=0)
        os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
        with open(TOKEN_PATH, "w") as f:
            f.write(creds.to_json())

    return creds


def _get_service():
    global _service
    if _service is None:
        _service = build("gmail", "v1", credentials=_get_credentials())
    return _service


def send_digest(subject: str, body: str, *, to: str | None = None) -> dict:
    """Send a plain-text email. Returns the Gmail API's send response."""
    recipient = to or DIGEST_RECIPIENT
    if not recipient:
        raise ValueError("No recipient — pass `to` or set DIGEST_RECIPIENT in .env")

    message = MIMEText(body)
    message["to"] = recipient
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

    service = _get_service()
    return service.users().messages().send(userId="me", body={"raw": raw}).execute()


def send_digest_run(run: "DigestRun", *, to: str | None = None) -> dict:
    """Build and send an email from a DigestRun — the real digest, or a kill-switch/
    composer-failure alert. This is the one function callers should reach for; it decides
    what to send so callers don't have to inspect DigestRun's internals themselves."""
    today = datetime.date.today().strftime("%b %d, %Y")

    if run.alert:
        failed = [r.agent for r in run.results if r.status != "ok"]
        succeeded = len(run.results) - len(failed)
        subject = f"{DIGEST_SUBJECT_PREFIX}: ALERT — not enough data today"
        body = (
            f"Only {succeeded}/{len(run.results)} agents succeeded today, so no digest "
            f"was composed.\n\nFailed: {', '.join(failed)}\n\n"
            "Check the individual sub-agents manually."
        )
    elif run.digest_text:
        subject = f"{DIGEST_SUBJECT_PREFIX} — {today}"
        body = run.digest_text
    else:
        error = run.composer_result.error if run.composer_result else "unknown error"
        subject = f"{DIGEST_SUBJECT_PREFIX}: ALERT — composer failed"
        body = (
            f"All sub-agents ran, but the composer failed: {error}\n\n"
            "Raw sub-agent data may still be useful — check logs."
        )

    return send_digest(subject, body, to=to)
