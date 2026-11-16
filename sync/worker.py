"""Manual Close export/import jobs. Runs separately from the email worker."""
import base64
import csv
import gzip
import hashlib
import io
import itertools
import json
import logging
import os
from pathlib import Path
import re
import signal
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

LOG = logging.getLogger("close_sync")
CLOSE_HOSTS = {"api.close.com", "app.close.com", "api.close.io", "app.close.io"}
MAX_EXPORT_BYTES = 256 * 1024 * 1024


class SyncFailure(RuntimeError):
    def __init__(self, message, retryable=False, delay=30):
        self.retryable = retryable
        self.delay = max(5, min(300, delay))
        super().__init__(message)


class LeaseLost(RuntimeError):
    pass


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def validate_https(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443) or parsed.fragment:
        raise SyncFailure("Unexpected export URL.")
    return parsed


class DownloadRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https(newurl)
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected:
            redirected.remove_header("Authorization")
        return redirected


class Database:
    def __init__(self, origin, key):
        parsed = validate_https(origin)
        if parsed.path not in ("", "/") or parsed.query:
            raise ValueError("SUPABASE_URL must be an HTTPS origin")
        if not key:
            raise ValueError("SUPABASE_SECRET_KEY is required")
        self.url = origin.rstrip("/") + "/rest/v1/"
        self.headers = {"apikey": key, "Content-Type": "application/json", "Accept": "application/json"}
        if not key.startswith("sb_secret_"):
            self.headers["Authorization"] = "Bearer " + key
        self.opener = build_opener(NoRedirects())

    def rpc(self, name, data):
        request = Request(self.url + "rpc/" + name, data=json.dumps(data, ensure_ascii=False, allow_nan=False).encode(), headers=self.headers, method="POST")
        try:
            with self.opener.open(request, timeout=120) as response:
                return json.load(response)
        except HTTPError as exc:
            if exc.code == 400:
                try:
                    message = json.load(exc).get("message", "")
                except (ValueError, OSError):
                    message = ""
                if "Sync lease lost" in message:
                    raise LeaseLost() from None
                if "Export changed" in message:
                    raise SyncFailure("Export contents changed. Start a fresh sync instead of resuming.") from None
                raise SyncFailure("Database rejected the import checkpoint. Check migration 010 and source data.") from None
            raise SyncFailure(f"Database request failed (HTTP {exc.code}).", exc.code in (408,429) or exc.code >= 500) from None
        except (URLError, OSError, ValueError):
            raise SyncFailure("Database response lost. The batch may have committed; progress will be rechecked.", True) from None

    def completed(self, ids):
        params = urlencode({"select": "id", "status": "eq.completed", "id": "in.(" + ",".join(ids) + ")"})
        try:
            with self.opener.open(Request(self.url + "close_lead_sync_jobs?" + params, headers=self.headers), timeout=60) as response:
                rows = json.load(response)
            return {row["id"] for row in rows}
        except (HTTPError, URLError, OSError, ValueError, KeyError):
            raise SyncFailure("Could not check completed sync cache entries.", True) from None


class Close:
    def __init__(self, key):
        if not key or not key.strip():
            raise ValueError("CLOSE_API_KEY is required")
        self.authorization = "Basic " + base64.b64encode((key.strip() + ":").encode()).decode("ascii")
        self.opener = build_opener(NoRedirects())
        self.download_opener = build_opener(DownloadRedirects())

    def request(self, path, body=None):
        headers = {"Authorization": self.authorization, "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode(); headers["Content-Type"] = "application/json"
        try:
            request = Request("https://api.close.com/api/v1/" + path, data=data, headers=headers, method="POST" if body is not None else "GET")
            with self.opener.open(request, timeout=60) as response:
                result = json.load(response)
            if not isinstance(result, dict):
                raise SyncFailure("Close returned an invalid response.", True)
            return result
        except HTTPError as exc:
            if exc.code in (401,403):
                raise SyncFailure("Close authentication failed. Check the sync worker API key and export permissions.") from None
            try:
                delay = int(exc.headers.get("Retry-After", "30"))
            except (ValueError, AttributeError):
                delay = 30
            raise SyncFailure(f"Close request failed (HTTP {exc.code}).", exc.code in (408,429) or exc.code >= 500, delay) from None
        except (URLError, OSError, ValueError):
            raise SyncFailure("Could not reach Close or parse its response.", True) from None

    def create_export(self):
        result = self.request("export/lead/", {"format": "csv", "type": "leads", "date_format": "iso8601", "send_done_email": False})
        export_id = result.get("id", "")
        if not isinstance(export_id, str) or not re.fullmatch(r"expo_[A-Za-z0-9]+", export_id):
            raise SyncFailure("Close did not return a valid export ID.", True)
        return export_id

    def export(self, export_id):
        if not re.fullmatch(r"expo_[A-Za-z0-9]+", export_id):
            raise SyncFailure("Invalid saved Close export ID.")
        return self.request("export/lead/" + quote(export_id, safe="") + "/")

    def download(self, url, destination, heartbeat, suffix):
        parsed = validate_https(url)
        request = Request(url, headers={"Accept-Encoding": "identity"})
        if parsed.hostname in CLOSE_HOSTS:
            # Never forward this credential to a redirect destination.
            request.add_unredirected_header("Authorization", self.authorization)
        raw = destination.with_name(destination.name + "." + suffix + ".download")
        decoded = destination.with_name(destination.name + "." + suffix + ".decoded")
        try:
            with self.download_opener.open(request, timeout=120) as response, raw.open("wb") as output:
                count = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > MAX_EXPORT_BYTES:
                        raise SyncFailure("Close export exceeds the 256 MiB import limit.")
                    output.write(chunk); heartbeat()
                output.flush(); os.fsync(output.fileno())
            with raw.open("rb") as source:
                magic = source.read(2); source.seek(0)
                reader = gzip.GzipFile(fileobj=source) if magic == b"\x1f\x8b" else source
                with decoded.open("wb") as output:
                    count = 0
                    while True:
                        chunk = reader.read(1024 * 1024)
                        if not chunk:
                            break
                        count += len(chunk)
                        if count > MAX_EXPORT_BYTES:
                            raise SyncFailure("Decoded Close export exceeds the 256 MiB import limit.")
                        output.write(chunk); heartbeat()
                    output.flush(); os.fsync(output.fileno())
            heartbeat(force=True)
            decoded.replace(destination)
        except HTTPError as exc:
            raise SyncFailure(f"Export download failed (HTTP {exc.code}).", exc.code in (408,429) or exc.code >= 500) from None
        except (gzip.BadGzipFile, EOFError):
            raise SyncFailure("Close returned an invalid gzip export.") from None
        except (URLError, TimeoutError):
            raise SyncFailure("Export download interrupted. The original export will be retried.", True) from None
        finally:
            raw.unlink(missing_ok=True); decoded.unlink(missing_ok=True)


def map_lead(row, synced_at):
    source = {str(key): str(value if value is not None else "") for key, value in row.items()}
    value = lambda name: source.get(name, "").strip()
    return {
        "close_lead_id": value("id"), "company_name": value("lead_name") or value("display_name"),
        "company_url": value("url"), "contact_name": value("primary_contact_name"),
        "first_name": value("primary_contact_first_name"), "last_name": value("primary_contact_last_name"),
        "contact_title": value("primary_contact_title"), "email": value("primary_contact_primary_email"),
        "lead_status": value("status_label"), "industry": value("custom.Industry"),
        "source_data": source, "last_synced_at": synced_at,
    }


def file_hash(path, heartbeat):
    digest = hashlib.sha256(); count = 0
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            count += len(chunk)
            if count > MAX_EXPORT_BYTES:
                raise SyncFailure("Cached export exceeds the import limit.")
            digest.update(chunk); heartbeat()
    return digest.hexdigest()


def prepare_records(source, destination, synced_at, heartbeat, suffix):
    """Validate the entire CSV before any database batch is written."""
    temporary = destination.with_name(destination.name + "." + suffix + ".tmp")
    seen = set(); total = 0
    csv.field_size_limit(4 * 1024 * 1024)
    try:
        with source.open("r", encoding="utf-8-sig", newline="") as incoming, temporary.open("w", encoding="utf-8") as output:
            rows = csv.DictReader(incoming, strict=True)
            if not rows.fieldnames or "id" not in rows.fieldnames or len(set(rows.fieldnames)) != len(rows.fieldnames):
                raise SyncFailure("Close export is missing its id header or contains duplicate columns.")
            for number, row in enumerate(rows, 1):
                if None in row:
                    raise SyncFailure(f"Malformed Close CSV at row {number}.")
                record = map_lead(row, synced_at)
                lead_id = record["close_lead_id"]
                if not lead_id:
                    raise SyncFailure(f"Close lead ID is missing at row {number}.")
                if lead_id in seen:
                    raise SyncFailure(f"Duplicate Close lead ID at row {number}.")
                seen.add(lead_id); total += 1
                output.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                heartbeat()
            if not total:
                raise SyncFailure("Close lead dump is empty. Nothing was written.")
            output.flush(); os.fsync(output.fileno())
        heartbeat(force=True)
        temporary.replace(destination)
        return total
    except (UnicodeError, csv.Error):
        raise SyncFailure("Close export contains invalid UTF-8 or malformed CSV.") from None
    finally:
        temporary.unlink(missing_ok=True)


class Worker:
    def __init__(self, db, close, cache, wait_seconds=300, batch_size=200, stopping=lambda: False):
        if not 1 <= wait_seconds or not 1 <= batch_size <= 1000:
            raise ValueError("Invalid export wait or batch size")
        self.db, self.close, self.cache = db, close, Path(cache)
        self.wait_seconds, self.batch_size, self.stopping = wait_seconds, batch_size, stopping
        self.job = None; self.last_heartbeat = 0
        self.cache.mkdir(parents=True, exist_ok=True)

    def update(self, operation, data=None):
        result = self.db.rpc("update_close_lead_sync", {"p_job_id": self.job["id"], "p_token": self.job["lease_token"], "p_operation": operation, "p_data": data or {}})
        if not isinstance(result, dict) or result.get("id") != self.job["id"]:
            raise SyncFailure("Database returned an invalid sync checkpoint.", True)
        self.job = result; self.last_heartbeat = time.monotonic()
        return result

    def heartbeat(self, force=False):
        if self.stopping():
            raise LeaseLost()  # Leave the durable checkpoint for the next worker.
        if force or time.monotonic() - self.last_heartbeat >= 30:
            self.update("heartbeat")

    def wait_export(self):
        deadline = time.monotonic() + self.wait_seconds
        while True:
            self.heartbeat()
            result = self.close.export(self.job["export_id"])
            if result.get("status") == "error":
                raise SyncFailure("Close could not generate this export. Start a fresh sync.")
            if result.get("status") == "done" and result.get("download_url"):
                return result["download_url"]
            if time.monotonic() >= deadline:
                raise SyncFailure("Close export is still processing. The saved export will be checked again.", True)
            time.sleep(min(3, max(0, deadline - time.monotonic())))

    def process(self, job):
        UUID(job["id"]); UUID(job["lease_token"])
        self.job = job; self.last_heartbeat = time.monotonic()
        self.heartbeat(force=True)
        source = self.cache / (job["id"] + ".csv")
        records = self.cache / (job["id"] + ".jsonl")
        token = job["lease_token"]
        if self.job["phase"] == "exporting":
            if not self.job.get("export_id"):
                self.update("export", {"export_id": self.close.create_export()})
            url = self.wait_export()
            self.update("download")
        else:
            url = None
        valid_cache = source.exists()
        if valid_cache and self.job.get("csv_sha256"):
            valid_cache = file_hash(source, self.heartbeat) == self.job["csv_sha256"]
        if not valid_cache:
            url = url or self.wait_export()
            self.close.download(url, source, self.heartbeat, token)
        digest = file_hash(source, self.heartbeat)
        if self.job.get("csv_sha256") and self.job["csv_sha256"] != digest:
            raise SyncFailure("Export contents changed. Start a fresh sync instead of resuming.")
        total = prepare_records(source, records, self.job["synced_at"], self.heartbeat, token)
        self.update("prepared", {"total_rows": total, "csv_sha256": digest})
        offset = self.job["rows_processed"]
        with records.open(encoding="utf-8") as incoming:
            remaining = itertools.islice(incoming, offset, None)
            while True:
                batch = [json.loads(line) for line in itertools.islice(remaining, self.batch_size)]
                if not batch:
                    break
                self.heartbeat()
                result = self.db.rpc("import_close_lead_batch", {"p_job_id": self.job["id"], "p_token": self.job["lease_token"], "p_offset": offset, "p_rows": batch})
                if not isinstance(result, dict) or result.get("rows_processed") != offset + len(batch):
                    raise SyncFailure("Database returned an invalid batch checkpoint.", True)
                self.job = result; self.last_heartbeat = time.monotonic(); offset = result["rows_processed"]
                LOG.info("sync_progress job=%s rows=%s total=%s", self.job["id"], offset, total)
        self.update("complete")
        source.unlink(missing_ok=True); records.unlink(missing_ok=True)
        LOG.info("sync_completed job=%s rows=%s", self.job["id"], total)

    def run_once(self):
        job = self.db.rpc("claim_close_lead_sync", {})
        if job is None:
            return False
        try:
            self.process(job)
        except LeaseLost:
            LOG.info("sync_lease_released job=%s", job["id"])
        except SyncFailure as error:
            try:
                self.update("failure", {"error": str(error), "retryable": error.retryable, "delay_seconds": error.delay})
            except (LeaseLost, SyncFailure):
                LOG.warning("sync_checkpoint_pending job=%s", job["id"])
            LOG.warning("sync_interrupted job=%s retryable=%s", job["id"], error.retryable)
        except OSError:
            # Preserve cache/checkpoints; never reset an uncertain import offset.
            try:
                self.update("failure", {"error": "Sync worker storage failed. Check its persistent volume.", "retryable": True})
            except (LeaseLost, SyncFailure):
                pass
            LOG.warning("sync_storage_failed job=%s", job["id"])
        return True

    def cleanup_completed(self):
        files = {}
        for path in self.cache.iterdir():
            if not re.fullmatch(r"[A-Fa-f0-9-]{36}\.(?:csv|jsonl)(?:\.[A-Fa-f0-9-]{36}\.(?:download|decoded|tmp))?", path.name):
                continue
            identifier = path.name.split(".")[0]
            try:
                UUID(identifier)
            except ValueError:
                continue
            if path.is_file():
                files.setdefault(identifier, []).append(path)
        ids = list(files)
        for offset in range(0, len(ids), 100):
            for identifier in self.db.completed(ids[offset:offset + 100]):
                # Completed jobs cannot be resumed through the API. Failed and
                # active job caches are kept for deterministic recovery.
                for path in files.get(identifier, []):
                    path.unlink(missing_ok=True)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stopping = False
    def stop(*_):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
    db = Database(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SECRET_KEY"])
    close = Close(os.environ["CLOSE_API_KEY"])
    worker = Worker(db, close, os.environ.get("SYNC_CACHE_DIR", "/data/close-sync"), int(os.environ.get("CLOSE_EXPORT_WAIT_SECONDS", "300")), int(os.environ.get("SYNC_BATCH_SIZE", "200")), lambda: stopping)
    LOG.info("sync_worker_started")
    cleanup_at = 0
    while not stopping:
        if time.monotonic() >= cleanup_at:
            try:
                worker.cleanup_completed()
            except (SyncFailure, OSError):
                LOG.warning("sync_cache_cleanup_pending")
            cleanup_at = time.monotonic() + 3600
        try:
            worker.run_once()
        except (SyncFailure, LeaseLost, OSError, ValueError):
            LOG.warning("sync_poll_failed; check database, migration 010, and worker configuration")
        for _ in range(5):
            if stopping:
                break
            time.sleep(1)
    LOG.info("sync_worker_stopped")


if __name__ == "__main__":
    main()
