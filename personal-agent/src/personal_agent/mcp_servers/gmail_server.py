"""Gmail MCP server — read-only, runs locally as a subprocess over stdio.

Not a hosted service: launched fresh by whichever agent needs it (via stdio_client),
and exits when that run is done. The Gmail OAuth token lives only in a local file on
this machine — never sent anywhere except directly to Google's own API.

One-time setup (you do this, not this code):
1. In Google Cloud Console, create/select a project and enable the Gmail API.
2. OAuth consent screen: External, add yourself as a test user.
3. Create OAuth client ID credentials, type "Desktop app".
4. Download the JSON, save it at GMAIL_OAUTH_CREDENTIALS_PATH (default below).

First run of this server opens a browser for you to log in and approve read-only
access; after that it caches a refresh token at GMAIL_OAUTH_TOKEN_PATH so later runs
don't need the browser again.

Run standalone for a one-time auth check:
    python -m personal_agent.mcp_servers.gmail_server
(It will hang waiting for stdio input after auth succeeds — Ctrl+C once you see
"Gmail authentication successful" is expected; a real client connects over stdio.)
"""

from __future__ import annotations

import base64
import os
import re

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from mcp.server.fastmcp import FastMCP

# Read-only: this server can never send, delete, or modify mail — only search/read.
# Delivery (sending the digest) is a separate concern with its own, narrower scope.
SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# This digest runs once a day, so anything older was already covered by a previous run.
# Enforced here (AND-ed into every query server-side), not left to the agent's prompt —
# same "enforce constraints in code, not just in the prompt" principle as tool scoping.
SEARCH_WINDOW = os.environ.get("GMAIL_SEARCH_WINDOW", "newer_than:1d")

# Marketing/promo mail is excluded server-side, via Gmail's own inbox categorization —
# so the agent never even sees it, rather than being told to ignore it. Stronger and
# more reliable than a prompt instruction alone (the email agent's system prompt also
# tells it to skip anything promotional that slips through, as a second layer).
#
# Procare (son's school app) mail is also excluded from the general search — it's owned
# by the school sub-agent's dedicated search_procare_emails tool below instead, so the
# same message isn't reported by both agents.
PROCARE_SENDER_DOMAIN = os.environ.get("PROCARE_SENDER_DOMAIN", "online.procaresoftware.com")
EXCLUDE_QUERY = os.environ.get(
    "GMAIL_EXCLUDE_QUERY", f"-category:promotions -from:{PROCARE_SENDER_DOMAIN}"
)

_DEFAULT_CREDENTIALS_PATH = os.path.join(os.getcwd(), ".secrets", "gmail_credentials.json")
_DEFAULT_TOKEN_PATH = os.path.join(os.getcwd(), ".secrets", "gmail_token.json")
CREDENTIALS_PATH = os.environ.get("GMAIL_OAUTH_CREDENTIALS_PATH", _DEFAULT_CREDENTIALS_PATH)
TOKEN_PATH = os.environ.get("GMAIL_OAUTH_TOKEN_PATH", _DEFAULT_TOKEN_PATH)

mcp = FastMCP("gmail")

_service = None  # lazily built on first tool call, reused for the life of this process


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


def _header(headers: list[dict], name: str) -> str:
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def _strip_html(html: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_body(payload: dict) -> str:
    """Walk a Gmail MIME payload for the best available body text (recursive parts)."""
    mime_type = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")

    if mime_type == "text/plain" and data:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts") or []:
        text = _extract_body(part)
        if text:
            return text

    if mime_type == "text/html" and data:
        html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
        return _strip_html(html)

    return ""


def _search(full_query: str, max_results: int) -> list[dict]:
    service = _get_service()
    resp = service.users().messages().list(userId="me", q=full_query, maxResults=max_results).execute()
    results = []
    for m in resp.get("messages", []):
        msg = (
            service.users()
            .messages()
            .get(userId="me", id=m["id"], format="metadata", metadataHeaders=["Subject", "From", "Date"])
            .execute()
        )
        headers = msg["payload"]["headers"]
        results.append(
            {
                "id": msg["id"],
                "subject": _header(headers, "Subject"),
                "from": _header(headers, "From"),
                "date": _header(headers, "Date"),
                "snippet": msg.get("snippet", ""),
            }
        )
    return results


@mcp.tool()
def search_emails(query: str = "", max_results: int = 10) -> list[dict]:
    """Search today's legitimate Gmail (the server always scopes results to the last 24
    hours, excludes promotional/marketing mail, and excludes Procare/school mail — that's
    the school sub-agent's job — regardless of query; don't bother adding your own date or
    category filter). Use Gmail search syntax for anything else, e.g. "from:example.com",
    "is:unread". Leave query empty to get everything from today. Returns id, subject,
    from, date, and a short snippet for each match — call get_email with an id to read
    the full body."""
    full_query = f"{SEARCH_WINDOW} {EXCLUDE_QUERY} {query}".strip()
    return _search(full_query, max_results)


@mcp.tool()
def search_procare_emails(max_results: int = 10) -> list[dict]:
    """Search today's email from Procare (son's school/daycare app) only — the server
    hardcodes both the sender filter and the last-24-hours window. Returns id, subject,
    from, date, and a short snippet for each match — call get_email with an id to read
    the full body."""
    full_query = f"{SEARCH_WINDOW} from:{PROCARE_SENDER_DOMAIN}"
    return _search(full_query, max_results)


@mcp.tool()
def get_email(message_id: str) -> dict:
    """Get the full body text of one email by id (from a search_emails or
    search_procare_emails result)."""
    service = _get_service()
    msg = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    headers = msg["payload"]["headers"]
    return {
        "id": msg["id"],
        "subject": _header(headers, "Subject"),
        "from": _header(headers, "From"),
        "date": _header(headers, "Date"),
        "body": _extract_body(msg["payload"]),
    }


if __name__ == "__main__":
    mcp.run()
