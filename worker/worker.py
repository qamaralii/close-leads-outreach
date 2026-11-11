"""Sequential Railway worker. Supabase is the queue; SQLite is the finish-only outbox."""
import json
import logging
import os
import signal
import sqlite3
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

LOG = logging.getLogger("outreach")


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, supabase_url, supabase_key, composio_key):
        parsed = urlsplit(supabase_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("SUPABASE_URL must be an HTTPS origin")
        self.supabase = supabase_url.rstrip("/") + "/rest/v1/"
        self.db_headers = {"apikey": supabase_key}
        if not supabase_key.startswith("sb_secret_"):
            self.db_headers["Authorization"] = "Bearer " + supabase_key
        self.composio_headers = {"x-api-key": composio_key}
        self.opener = build_opener(NoRedirects())

    def request(self, url, headers, payload=None):
        headers = {**headers, "Accept": "application/json", "Content-Type": "application/json"}
        data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        try:
            with self.opener.open(Request(url, headers=headers, data=data), timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            raise RuntimeError(f"API HTTP {exc.code}") from None
        except (URLError, OSError, ValueError):
            raise RuntimeError("API response lost or invalid") from None

    def rpc(self, name, payload):
        return self.request(self.supabase + "rpc/" + name, self.db_headers, payload)

    def queued(self, cursor=None):
        params = {"select": "id,campaign_id,due_at,campaigns!inner(created_by,status)", "status": "eq.queued",
                  "campaigns.status": "in.(scheduled,submitted)", "due_at": "lte." + datetime.now(timezone.utc).isoformat(),
                  "order": "due_at.asc,id.asc", "limit": "100"}
        if cursor:
            due_at, recipient_id = cursor
            params["or"] = f"(due_at.gt.{due_at},and(due_at.eq.{due_at},id.gt.{recipient_id}))"
        return self.request(self.supabase + "campaign_recipients?" + urlencode(params), self.db_headers)

    def proxy(self, job, method, endpoint, body=None):
        payload = {"connected_account_id": job["connected_account_id"], "user_id": job["entity_id"],
                   "method": method, "endpoint": endpoint}
        if body is not None:
            payload["body"] = body
        return self.request("https://backend.composio.dev/api/v3.1/tools/execute/proxy", self.composio_headers, payload)

    def preflight(self, job):
        account = self.request("https://backend.composio.dev/api/v3.1/connected_accounts/" + quote(job["connected_account_id"], safe=""), self.composio_headers)
        if (not isinstance(account, dict) or account.get("id") != job["connected_account_id"]
                or account.get("user_id") != job["entity_id"] or account.get("status") != "ACTIVE"
                or account.get("is_disabled") is not False or account.get("toolkit", {}).get("slug") != "outlook"):
            raise RuntimeError("Account ownership/status verification failed")
        response = self.proxy(job, "GET", "https://graph.microsoft.com/v1.0/me?$select=mail,userPrincipalName")
        if not isinstance(response, dict) or response.get("status") != 200 or not isinstance(response.get("data"), dict):
            raise RuntimeError("Mailbox profile lookup failed")
        address = response["data"].get("mail") or response["data"].get("userPrincipalName")
        if not isinstance(address, str) or address.strip().lower() != job["sender_email"].strip().lower():
            raise RuntimeError("Authenticated mailbox differs from reviewed sender")

    def send(self, job):
        return self.proxy(job, "POST", "https://graph.microsoft.com/v1.0/me/sendMail", {
            "message": {"subject": job["subject"], "body": {"contentType": "Text", "content": job["body"]},
                        "toRecipients": [{"emailAddress": {"address": job["recipient_email"]}}]},
            "saveToSentItems": True,
        })


class Journal:
    """Requires a persistent Railway volume. Never replays provider calls."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outcomes(attempt_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
        self.db.commit()

    def store(self, receipt):
        with self.db:
            self.db.execute("INSERT INTO outcomes VALUES (?, ?)", (receipt["p_attempt_id"], json.dumps(receipt)))

    def flush(self, api):
        for attempt_id, text in self.db.execute("SELECT attempt_id, receipt FROM outcomes").fetchall():
            receipt = json.loads(text)
            try:
                if api.rpc("finish_outreach_attempt", receipt) != receipt["p_status"]:
                    raise RuntimeError("Unexpected finish response")
            except Exception:
                LOG.warning("finish_pending attempt=%s", attempt_id)
                continue
            with self.db:
                self.db.execute("DELETE FROM outcomes WHERE attempt_id = ?", (attempt_id,))
            LOG.info("outcome_recorded attempt=%s status=%s", attempt_id, receipt["p_status"])


def execute(api, journal, row):
    user = row["campaigns"]["created_by"]
    claimed = api.rpc("claim_outreach_recipient", {"p_user_id": user, "p_campaign_id": row["campaign_id"], "p_recipient_id": row["id"]})
    if claimed == []:
        return
    if not isinstance(claimed, list) or len(claimed) != 1:
        raise RuntimeError("Invalid claim response; inspect recipient state")
    claim = claimed[0]
    attempt = claim["attempt_id"]
    job = api.rpc("begin_outreach_send", {"p_user_id": user, "p_attempt_id": attempt, "p_claim_token": claim["claim_token"]})
    if not isinstance(job, dict) or job.get("authorized") is not True:
        LOG.warning("send_gate_not_authorized attempt=%s", attempt)
        return
    receipt = {"p_user_id": user, "p_attempt_id": attempt, "p_claim_token": claim["claim_token"],
               "p_status": "failed", "p_provider_status_code": None, "p_provider_message_id": None, "p_error": None}
    try:
        if job.get("campaign_id") != row["campaign_id"] or job.get("recipient_id") != row["id"] or job.get("is_html") is not False:
            raise RuntimeError("Stored job mismatch")
        for key in ("entity_id", "connected_account_id", "sender_email", "recipient_email", "subject", "body"):
            if not isinstance(job.get(key), str) or not job[key].strip():
                raise RuntimeError("Incomplete stored job")
        api.preflight(job)
    except Exception:
        receipt["p_error"] = "Preflight failed; no sendMail call attempted. Check connection ownership, mailbox, and permissions."
    else:
        receipt.update(p_status="unknown", p_error="Send response uncertain; do not resend.")
        try:
            response = api.send(job)
            status = response.get("status") if isinstance(response, dict) else None
            if type(status) is int and 100 <= status <= 599:
                receipt["p_provider_status_code"] = status
            if type(status) is int and status == 202:
                receipt.update(p_status="submitted", p_error=None)
            elif type(status) is int and 400 <= status < 500 and status != 408:
                receipt.update(p_status="failed", p_error=f"Microsoft Graph rejected sendMail (HTTP {status}).")
        except Exception:
            pass
    # If this write fails, stop processing: operator must reconcile the already gated attempt.
    journal.store(receipt)
    journal.flush(api)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api = API(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"], os.environ["COMPOSIO_API_KEY"])
    journal = Journal(os.environ.get("OUTCOME_DB_PATH", "/data/outcomes.sqlite"))
    interval = max(2, int(os.environ.get("POLL_SECONDS", "5")))
    stopping = False
    cursor = None
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("worker_started")
    while not stopping:
        journal.flush(api)
        try:
            rows = api.queued(cursor)
            if not isinstance(rows, list):
                raise RuntimeError("Invalid queue response")
            # Keyset pagination gives later recipients a turn even if older rows
            # repeatedly fail membership/sender checks. Wrap at the end.
            cursor = (rows[-1]["due_at"], rows[-1]["id"]) if rows else None
            for row in rows:
                if stopping:
                    break
                try:
                    execute(api, journal, row)
                except sqlite3.Error:
                    LOG.exception("outcome_journal_failure; reconcile last attempt before restart")
                    raise
                except Exception:
                    LOG.warning("recipient_execution_stopped recipient=%s; inspect state", row.get("id"))
                time.sleep(1)
        except sqlite3.Error:
            raise
        except Exception:
            LOG.warning("queue_poll_failed")
        for _ in range(interval):
            if stopping:
                break
            time.sleep(1)
    LOG.info("worker_stopped")


if __name__ == "__main__":
    main()
