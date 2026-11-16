"""Sequential Railway worker. Supabase is the queue; SQLite is the finish-only outbox."""
import json
import base64
import hashlib
import logging
import os
import signal
import sqlite3
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_ATTACHMENT_BYTES = 25_000_000
ATTACHMENT_PROPERTY = "String {32172473-72e5-4436-bb55-6b459e9ed693} Name IntugleAttemptId"


class PreparationPending(RuntimeError):
    """Provider preparation is uncertain; resume it without issuing a send."""


class APIHTTPError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(f"API HTTP {code}")

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
            raise APIHTTPError(exc.code) from None
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

    def proxy(self, job, method, endpoint, body=None, parameters=None):
        payload = {"connected_account_id": job["connected_account_id"], "user_id": job["entity_id"],
                   "method": method, "endpoint": endpoint}
        if body is not None:
            payload["body"] = body
        if parameters:
            payload["parameters"] = parameters
        return self.request("https://backend.composio.dev/api/v3.1/tools/execute/proxy", self.composio_headers, payload)

    def graph(self, job, method, path, body=None):
        try:
            response = self.proxy(job, method, "https://graph.microsoft.com/v1.0/me/" + path, body, [{"name": "Prefer", "type": "header", "value": 'outlook.body-content-type="text", IdType="ImmutableId"'}])
        except APIHTTPError as exc:
            if exc.code == 429 or exc.code >= 500:
                raise PreparationPending("Outlook preparation temporarily unavailable") from None
            raise
        except RuntimeError:
            raise PreparationPending("Outlook preparation response lost") from None
        status = response.get("status") if isinstance(response, dict) else None
        if type(status) is not int or not 200 <= status < 300:
            if type(status) is int and 400 <= status < 500 and status not in (408,429):
                raise APIHTTPError(status)
            raise PreparationPending("Outlook preparation temporarily unavailable")
        return response.get("data") or {}

    def pending(self):
        return self.rpc("pending_outreach_deliveries", {})

    def attachment(self, file):
        size = file.get("size_bytes")
        if type(size) is not int or not 0 < size <= MAX_ATTACHMENT_BYTES or not isinstance(file.get("sha256"), str):
            raise ValueError("Invalid attachment metadata")
        path = file.get("path", "")
        if not path.startswith("files/") or ".." in path or len(path.split("/")) != 3:
            raise ValueError("Invalid attachment path")
        url = self.supabase.removesuffix("rest/v1/") + "storage/v1/object/outreach-attachments/" + quote(path, safe="/")
        try:
            with self.opener.open(Request(url, headers=self.db_headers), timeout=90) as response:
                content = response.read(size + 1)
        except HTTPError as exc:
            if exc.code == 404:
                raise ValueError("Attachment is missing") from None
            raise PreparationPending("Attachment storage temporarily unavailable") from None
        except (OSError, URLError):
            raise PreparationPending("Attachment download interrupted") from None
        if len(content) != size or hashlib.sha256(content).hexdigest() != file["sha256"]:
            raise ValueError("Attachment bytes differ from reviewed file")
        return content

    def upload_range(self, url, content, offset):
        # Outlook upload URLs are pre-authenticated. No Composio/Supabase/Graph
        # credentials are sent to them, and redirects are never followed.
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "outlook.office.com" or parsed.port not in (None, 443) or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Untrusted Outlook upload URL")
        chunk = content[offset:offset + 3_276_800]  # 10 x 320 KiB, below 4 MB
        headers = {"Content-Type": "application/octet-stream", "Content-Length": str(len(chunk)),
                   "Content-Range": f"bytes {offset}-{offset+len(chunk)-1}/{len(content)}"}
        try:
            with self.opener.open(Request(url, data=chunk, headers=headers, method="PUT"), timeout=90) as response:
                status = response.status
                body = response.read()
                result = json.loads(body) if body else {}
                return status, result
        except HTTPError as exc:
            if exc.code in (404, 410):
                return exc.code, {}
            raise PreparationPending("Attachment upload interrupted") from None
        except (URLError, OSError, ValueError):
            raise PreparationPending("Attachment upload response uncertain") from None

    def upload_status(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname != "outlook.office.com" or parsed.port not in (None,443) or parsed.username or parsed.password or parsed.fragment:
            raise ValueError("Untrusted Outlook upload URL")
        try:
            with self.opener.open(Request(url), timeout=90) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code in (404,410):
                return None
            raise PreparationPending("Could not check upload progress") from None
        except (URLError, OSError, ValueError):
            raise PreparationPending("Could not check upload progress") from None

    def cleanup_uploads(self):
        for file in self.rpc("collect_outreach_uploads", {}):
            root = self.supabase.removesuffix("rest/v1/")
            paths = [f"staging/{file['created_by']}/{file['id']}", f"files/{file['created_by']}/{file['id']}"]
            request = Request(root + "storage/v1/object/outreach-attachments", headers={**self.db_headers, "Content-Type": "application/json"}, data=json.dumps({"prefixes": paths}).encode(), method="DELETE")
            with self.opener.open(request, timeout=90) as response:
                response.read()
            self.rpc("finish_outreach_upload_cleanup", {"p_id": file["id"]})

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
            "message": message_payload(job),
            "saveToSentItems": True,
        })


class Journal:
    """Durable preparation and finish journal. Never replays provider sends."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS outcomes(attempt_id TEXT PRIMARY KEY, receipt TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS preparations(attempt_id TEXT PRIMARY KEY, state TEXT NOT NULL)")
        self.db.commit()

    def store(self, receipt):
        with self.db:
            self.db.execute("INSERT INTO outcomes VALUES (?, ?)", (receipt["p_attempt_id"], json.dumps(receipt)))

    def state(self, attempt):
        row = self.db.execute("SELECT state FROM preparations WHERE attempt_id=?", (attempt,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save_state(self, attempt, state):
        with self.db:
            self.db.execute("INSERT INTO preparations VALUES (?, ?) ON CONFLICT(attempt_id) DO UPDATE SET state=excluded.state", (attempt, json.dumps(state)))

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
                self.db.execute("DELETE FROM preparations WHERE attempt_id = ?", (attempt_id,))
            LOG.info("outcome_recorded attempt=%s status=%s", attempt_id, receipt["p_status"])


def message_payload(job):
    return {"subject": job["subject"], "body": {"contentType": "Text", "content": job["body"]},
            "toRecipients": [{"emailAddress": {"address": job["recipient_email"]}}],
            "ccRecipients": [{"emailAddress": {"address": address}} for address in job.get("cc", [])]}


def prepare_draft(api, journal, attempt, job, state):
    files = job["attachments"]
    if len(files) > 20 or sum(file["size_bytes"] for file in files) > MAX_ATTACHMENT_BYTES:
        raise ValueError("Attachments exceed the configured limit")
    # Verify every source file before touching Outlook. At most 25 MB per job.
    contents = {file["id"]: api.attachment(file) for file in files}
    if not state.get("draft_id"):
        query = urlencode({"$filter": f"singleValueExtendedProperties/Any(ep: ep/id eq '{ATTACHMENT_PROPERTY}' and ep/value eq '{attempt}')", "$select": "id"})
        found = api.graph(job, "GET", "mailFolders/drafts/messages?" + query).get("value", [])
        if len(found) == 1:
            state["draft_id"] = found[0]["id"]
        elif len(found) > 1:
            raise ValueError("Multiple preparation drafts; reconcile before sending")
        elif state.get("draft_intent"):
            raise PreparationPending("Draft creation uncertain; waiting to reconcile the original draft")
        else:
            state["draft_intent"] = True
            journal.save_state(attempt, state)
            payload = message_payload(job)
            payload["singleValueExtendedProperties"] = [{"id": ATTACHMENT_PROPERTY, "value": attempt}]
            try:
                draft = api.graph(job, "POST", "messages", payload)
            except APIHTTPError:
                state["draft_intent"] = False
                journal.save_state(attempt, state)
                raise
            except Exception:
                raise PreparationPending("Draft creation uncertain; reconcile before retrying") from None
            if not isinstance(draft.get("id"), str):
                raise PreparationPending("Draft creation response uncertain")
            state["draft_id"] = draft["id"]
        journal.save_state(attempt, state)
    draft_path = "messages/" + quote(state["draft_id"], safe="")
    attached = api.graph(job, "GET", draft_path + "/attachments?$select=id,name,size").get("value", [])
    uploaded = state.setdefault("uploaded", [])
    sessions = state.setdefault("sessions", {})
    for file in files:
        fid, content = file["id"], contents[file["id"]]
        # Graph's reported attachment size can include storage overhead. Match
        # unique names here and verify actual decoded bytes/checksums below.
        matching = [a for a in attached if a.get("name") == file["name"]]
        if len(matching) > 1:
            raise ValueError("Duplicate Outlook attachment; reconcile draft")
        if matching:
            if fid not in uploaded:
                uploaded.append(fid)
                journal.save_state(attempt, state)
            continue
        if fid in uploaded:
            raise ValueError("A prepared attachment was removed from Outlook")
        if len(content) < 3_000_000:
            # POST may succeed with a lost response: next pass inspects the draft
            # before attaching again. Distinct filenames make reconciliation exact.
            try:
                api.graph(job, "POST", draft_path + "/attachments", {"@odata.type": "#microsoft.graph.fileAttachment", "name": file["name"], "contentType": file["content_type"], "contentBytes": base64.b64encode(content).decode("ascii")})
            except APIHTTPError:
                raise
            except Exception:
                raise PreparationPending("Small attachment response uncertain") from None
        else:
            session = sessions.get(fid)
            if session:
                current = api.upload_status(session["url"])
                if current is None:
                    sessions.pop(fid, None)
                    journal.save_state(attempt, state)
                    # The session may have completed with a lost final response.
                    # Reinspect attachments on the next pass before making a session.
                    raise PreparationPending("Upload session expired or finished; checking draft")
                session["offset"] = int(current["nextExpectedRanges"][0].split("-")[0])
            if not session:
                new = api.graph(job, "POST", draft_path + "/attachments/createUploadSession", {"AttachmentItem": {"attachmentType": "file", "name": file["name"], "size": len(content)}})
                session = {"url": new["uploadUrl"], "offset": 0}
                sessions[fid] = session
                journal.save_state(attempt, state)
            while session["offset"] < len(content):
                offset = session["offset"]
                status, result = api.upload_range(session["url"], content, offset)
                if status == 201:
                    session["offset"] = len(content)
                elif status == 200:
                    next_offset = int(result["nextExpectedRanges"][0].split("-")[0])
                    if not offset < next_offset <= len(content):
                        raise ValueError("Invalid upload progress")
                    session["offset"] = next_offset
                else:
                    sessions.pop(fid, None)
                    journal.save_state(attempt, state)
                    raise PreparationPending("Upload session needs reconciliation")
                journal.save_state(attempt, state)
        uploaded.append(fid)
        journal.save_state(attempt, state)
    # Do not dispatch an Outlook draft that was changed outside the application.
    draft = api.graph(job, "GET", draft_path + "?$select=id,isDraft,subject,body,from,replyTo,toRecipients,ccRecipients,bccRecipients,hasAttachments")
    addresses = lambda key: sorted(r["emailAddress"]["address"].strip().lower() for r in draft.get(key, []))
    final_files = api.graph(job, "GET", draft_path + "/attachments?$select=id,name,size").get("value", [])
    expected_files = sorted(f["name"] for f in files)
    draft_sender = (draft.get("from") or {}).get("emailAddress", {}).get("address")
    if (
        draft.get("isDraft") is not True
        or draft.get("subject") != job["subject"]
        or draft.get("body", {}).get("content", "").replace("\r\n", "\n") != job["body"].replace("\r\n", "\n")
        or (draft_sender and draft_sender.strip().lower() != job["sender_email"].strip().lower())
        or addresses("replyTo")
        or addresses("toRecipients") != [job["recipient_email"].strip().lower()]
        or addresses("ccRecipients") != sorted(job.get("cc", []))
        or addresses("bccRecipients")
        or sorted(f["name"] for f in final_files) != expected_files
    ):
        raise ValueError("Prepared Outlook draft differs from reviewed delivery")
    for file in files:
        provider_file = next(f for f in final_files if f["name"] == file["name"])
        remote = api.graph(job, "GET", draft_path + "/attachments/" + quote(provider_file["id"], safe=""))
        try:
            raw = base64.b64decode(remote["contentBytes"], validate=True)
        except (ValueError, KeyError):
            raise ValueError("Could not verify prepared Outlook attachment") from None
        if len(raw) != file["size_bytes"] or hashlib.sha256(raw).hexdigest() != file["sha256"]:
            raise ValueError("Prepared Outlook attachment changed")
    return draft_path


def execute(api, journal, row):
    user = row.get("user_id") or row["campaigns"]["created_by"]
    if "attempt_id" in row:
        claim = row
    else:
        claimed = api.rpc("claim_outreach_recipient", {"p_user_id": user, "p_campaign_id": row["campaign_id"], "p_recipient_id": row["id"]})
        if claimed == []:
            return
        if not isinstance(claimed, list) or len(claimed) != 1:
            raise RuntimeError("Invalid claim response; inspect recipient state")
        claim = claimed[0]
    attempt = claim["attempt_id"]
    if journal.db.execute("SELECT 1 FROM outcomes WHERE attempt_id=?", (attempt,)).fetchone():
        return
    credentials = {"p_user_id": user, "p_attempt_id": attempt, "p_claim_token": claim["claim_token"]}
    receipt = {"p_user_id": user, "p_attempt_id": attempt, "p_claim_token": claim["claim_token"],
                "p_status": "failed", "p_provider_status_code": None, "p_provider_message_id": None, "p_error": None}
    state = journal.state(attempt)
    # Local dispatch intent is durable before requesting the database gate. If a
    # crash or response loss follows, never call send again on restart.
    if state.get("dispatch_intent"):
        receipt.update(p_status="unknown", p_error="Dispatch authorization or response uncertain; do not resend.")
        journal.store(receipt)
        journal.flush(api)
        return
    try:
        job = api.rpc("prepare_outreach_delivery", credentials)
    except APIHTTPError as exc:
        if exc.code != 400:
            raise
        receipt["p_error"] = "Delivery is no longer eligible. Check sender, suppression, and attachment availability; no send attempted."
        journal.store(receipt)
        journal.flush(api)
        return
    if not isinstance(job, dict) or job.get("authorized") is not True:
        return
    draft_path = None
    try:
        if job.get("campaign_id") != row["campaign_id"] or job.get("recipient_id") != (row.get("recipient_id") or row.get("id")) or job.get("is_html") is not False:
            raise RuntimeError("Stored job mismatch")
        for key in ("entity_id", "connected_account_id", "sender_email", "recipient_email", "subject", "body"):
            if not isinstance(job.get(key), str) or not job[key].strip():
                raise RuntimeError("Incomplete stored job")
        api.preflight(job)
        if job.get("attachments"):
            draft_path = prepare_draft(api, journal, attempt, job, state)
            api.preflight(job)
    except PreparationPending:
        LOG.warning("preparation_pending attempt=%s", attempt)
        return
    except Exception:
        receipt["p_error"] = "Preparation failed; no send attempted. Check attachment availability, Outlook permissions, and mailbox message limits."
    else:
        state["dispatch_intent"] = True
        state["credentials"] = credentials
        journal.save_state(attempt, state)
        try:
            gate = api.rpc("dispatch_outreach_delivery", credentials)
        except APIHTTPError as exc:
            if exc.code != 400:
                raise
            receipt["p_error"] = "Delivery authorization rejected after preparation; no send attempted. Check sender and suppression."
            journal.store(receipt)
            journal.flush(api)
            return
        if not isinstance(gate, dict) or gate.get("authorized") is not True:
            LOG.warning("send_gate_not_authorized attempt=%s", attempt)
            return
        if gate != job:
            raise RuntimeError("Authorized delivery differs from prepared job")
        receipt.update(p_status="unknown", p_error="Send response uncertain; do not resend.")
        try:
            response = api.proxy(job, "POST", "https://graph.microsoft.com/v1.0/me/" + draft_path + "/send", parameters=[{"name": "Prefer", "type": "header", "value": 'IdType="ImmutableId"'}]) if draft_path else api.send(job)
            status = response.get("status") if isinstance(response, dict) else None
            if type(status) is int and 100 <= status <= 599:
                receipt["p_provider_status_code"] = status
            if type(status) is int and status == 202:
                receipt.update(p_status="submitted", p_error=None, p_provider_message_id=state.get("draft_id"))
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
    cleanup_at = 0
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    LOG.info("worker_started")
    while not stopping:
        journal.flush(api)
        try:
            # Reconcile locally recorded dispatch intents after a restart. The
            # database excludes these from resumable preparation candidates.
            for attempt, text in journal.db.execute("SELECT attempt_id,state FROM preparations").fetchall():
                state = json.loads(text)
                if state.get("dispatch_intent") and not journal.db.execute("SELECT 1 FROM outcomes WHERE attempt_id=?", (attempt,)).fetchone():
                    journal.store({**state["credentials"], "p_status": "unknown", "p_provider_status_code": None, "p_provider_message_id": None, "p_error": "Dispatch authorization or response uncertain; do not resend."})
            journal.flush(api)
            if time.monotonic() >= cleanup_at:
                try:
                    api.cleanup_uploads()
                except Exception:
                    LOG.warning("upload_cleanup_pending")
                cleanup_at = time.monotonic() + 3600
            for pending in api.pending():
                if stopping:
                    break
                try:
                    execute(api, journal, pending)
                except sqlite3.Error:
                    raise
                except Exception:
                    LOG.warning("preparation_execution_stopped attempt=%s", pending.get("attempt_id"))
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
