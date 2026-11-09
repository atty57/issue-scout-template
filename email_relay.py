"""Operator-hosted WSGI Resend relay, authenticated by GitHub Actions OIDC.

Run behind HTTPS using: gunicorn 'email_relay:create_app()'. The operator's Resend key, confirmed
recipients, sending quotas and duplicate protection stay on this service, in one SQLite file on a
persistent disk (SCOUT_EMAIL_DB). Routes:

    POST /send     a Scout workflow emails a run's report to its repository's confirmed address
    POST /verify   the email workflow asks for a confirmation code for the profile's address
    POST /confirm  the Issue Scout app confirms that address with the code
    GET  /health
"""
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import jwt
import requests

from email_delivery import validate_email


ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
MAX_BODY = 2_000_000
CODE_SECONDS = 30 * 60
CODE_ATTEMPTS = 5
IN_FLIGHT_SECONDS = 120  # a send still marked in progress after this is retried with the same idempotency key
# Resend forgets an idempotency key after 24 hours. A send that may have gone out is never retried after this,
# because a retry could then deliver a second copy.
IDEMPOTENCY_SECONDS = 23 * 3600
KEEP_SECONDS = 100 * 86_400  # delivery records older than this are pruned; quotas look back a month at most
# Workflows that may use the relay, and the events they may run on. email.yml runs from the app to
# send a confirmation code or a saved report again; it never scouts.
WORKFLOWS = {"daily.yml": {"schedule", "workflow_dispatch"}, "email.yml": {"workflow_dispatch"}}
# Limits from the environment. The shared defaults match Resend's free plan (100 a day, 3,000 a month);
# reports leave code_reserve percent of the shared quota for confirmation codes.
LIMITS = {
    "daily": ("SCOUT_EMAIL_DAILY_LIMIT", 100),
    "monthly": ("SCOUT_EMAIL_MONTHLY_LIMIT", 3000),
    "report_daily": ("SCOUT_EMAIL_USER_DAILY_LIMIT", 3),
    "report_monthly": ("SCOUT_EMAIL_USER_MONTHLY_LIMIT", 40),
    "code_daily": ("SCOUT_EMAIL_USER_DAILY_CODES", 3),
    "code_monthly": ("SCOUT_EMAIL_USER_MONTHLY_CODES", 10),
    "code_reserve": ("SCOUT_EMAIL_CODE_RESERVE_PERCENT", 10),
}
SCHEMA = """
PRAGMA journal_mode = WAL;
CREATE TABLE IF NOT EXISTS recipients (repository_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, email TEXT NOT NULL,
                                       ref TEXT NOT NULL, confirmed_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS codes (handle TEXT PRIMARY KEY, repository_id TEXT NOT NULL, owner_id TEXT NOT NULL,
                                  email TEXT NOT NULL, ref TEXT NOT NULL, attempts INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries (key TEXT PRIMARY KEY, kind TEXT NOT NULL, owner_id TEXT NOT NULL, status TEXT NOT NULL,
                                       day TEXT NOT NULL, month TEXT NOT NULL, updated_at INTEGER NOT NULL,
                                       first_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS deliveries_day ON deliveries (day, status);
CREATE INDEX IF NOT EXISTS deliveries_month ON deliveries (month, status);
CREATE INDEX IF NOT EXISTS deliveries_owner ON deliveries (owner_id, kind, month);
"""
UNAUTHORIZED = ("401 Unauthorized", {"error": "Invalid workflow identity"})
SENT = ("200 OK", {"status": "sent"})
OUTCOMES = {  # what a send that didn't go through tells the workflow
    "user_limit": ("429 Too Many Requests", {"error": "Email limit reached for this account", "reason": "user_limit"}),
    "quota": ("429 Too Many Requests", {"error": "Shared email quota reached", "reason": "quota"}),
    "busy": ("429 Too Many Requests", {"error": "Email provider busy", "reason": "busy"}),
    "in_progress": ("409 Conflict", {"error": "This email is still being sent", "reason": "in_progress"}),
    "duplicate": ("409 Conflict", {"error": "This run's email was already sent with different content", "reason": "duplicate"}),
    "unconfirmed": ("409 Conflict", {"error": "An earlier attempt may already have delivered this email, so it isn't sent again",
                                     "reason": "unconfirmed"}),
    "unknown": ("503 Service Unavailable", {"error": "Email provider temporarily unavailable", "reason": "unavailable"}),
    "rejected": ("422 Unprocessable Entity", {"error": "Email provider rejected delivery; contact the service operator", "reason": "rejected"}),
}


class TooLarge(ValueError):
    pass


def limits_from(environ):
    limits = {}
    for name, (variable, default) in LIMITS.items():
        value = environ.get(variable, "")
        limits[name] = int(value) if value else default  # junk fails at startup, not mid-send
        if limits[name] < 0:
            raise ValueError(f"{variable} must be zero or more")
    if limits["code_reserve"] > 90:
        raise ValueError("SCOUT_EMAIL_CODE_RESERVE_PERCENT must be at most 90")
    return limits


def periods(now):
    moment = datetime.fromtimestamp(now, timezone.utc)  # Resend's daily quota also resets at midnight UTC
    return moment.strftime("%Y-%m-%d"), moment.strftime("%Y-%m")


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Store:
    """Confirmed recipients, pending codes and every send, in one SQLite file. Each change runs in its own
    write transaction, so several gunicorn workers on one host share the limits safely."""

    def __init__(self, path):
        if not path:
            raise ValueError("Set SCOUT_EMAIL_DB to a file on a persistent disk")
        self.path = str(path)
        db = self.connect()
        db.executescript(SCHEMA)
        db.close()

    def connect(self):
        return sqlite3.connect(self.path, timeout=10, isolation_level=None)

    @contextmanager
    def transaction(self):
        db = self.connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def recipient(self, repository_id):
        with self.transaction() as db:
            row = db.execute("SELECT owner_id, email, ref FROM recipients WHERE repository_id = ?", (repository_id,)).fetchone()
        return dict(zip(("owner_id", "email", "ref"), row)) if row else None

    def reserve(self, key, kind, owner_id, limits, now):
        """-> 'new' | 'resume' | 'sent' | 'in_progress' | 'unconfirmed' | 'user_limit' | 'quota'. 'new' and 'resume'
        go to Resend, and each holds one place in the current day's and month's counts."""
        day, month = periods(now)
        with self.transaction() as db:
            db.execute("DELETE FROM deliveries WHERE updated_at < ?", (now - KEEP_SECONDS,))
            row = db.execute("SELECT status, updated_at, day, month, first_at FROM deliveries WHERE key = ?", (key,)).fetchone()
            if row and row[0] == "sent":
                return "sent"
            # An earlier attempt may have gone out; Resend's idempotency key covers it only within its window.
            resuming = bool(row) and row[0] == "sending"
            if resuming:
                if now - row[1] < IN_FLIGHT_SECONDS:
                    return "in_progress"
                if now - row[4] >= IDEMPOTENCY_SECONDS:
                    return "unconfirmed"
                if (row[2], row[3]) == (day, month):  # already counted today
                    db.execute("UPDATE deliveries SET updated_at = ? WHERE key = ?", (now, key))
                    return "resume"
            # A new send, or one left unfinished on an earlier day, must fit today's and this month's limits.

            def used(where, *args):  # every other send in that period
                return db.execute(f"SELECT COUNT(*) FROM deliveries WHERE status != 'released' AND key != ? AND {where}",
                                  (key, *args)).fetchone()[0]

            if (used("owner_id = ? AND kind = ? AND day = ?", owner_id, kind, day) >= limits[f"{kind}_daily"]
                    or used("owner_id = ? AND kind = ? AND month = ?", owner_id, kind, month) >= limits[f"{kind}_monthly"]):
                return "user_limit"
            share = 1 if kind == "code" else 1 - limits["code_reserve"] / 100
            if used("day = ?", day) >= limits["daily"] * share or used("month = ?", month) >= limits["monthly"] * share:
                return "quota"
            db.execute("INSERT OR REPLACE INTO deliveries VALUES (?, ?, ?, 'sending', ?, ?, ?, ?)",
                       (key, kind, owner_id, day, month, now, row[4] if resuming else now))
            return "resume" if resuming else "new"

    def finish(self, key, status, now):
        with self.transaction() as db:
            db.execute("UPDATE deliveries SET status = ?, updated_at = ? WHERE key = ?", (status, now, key))

    def save_code(self, handle, repository_id, owner_id, email, ref, now):
        """One pending code per repository: a newer request replaces the older code. A request ID reused by another
        repository replaces that one's too; its code still only confirms the address it was emailed to."""
        with self.transaction() as db:
            db.execute("DELETE FROM codes WHERE repository_id = ? OR handle = ? OR expires_at < ?", (repository_id, handle, now))
            db.execute("INSERT INTO codes VALUES (?, ?, ?, ?, ?, 0, ?)", (handle, repository_id, owner_id, email, ref, now + CODE_SECONDS))

    def confirm(self, handle, correct, now):
        """-> (result, detail): ('confirmed', email) | ('wrong', attempts left) | ('not_found' | 'expired' | 'locked', None)."""
        with self.transaction() as db:
            row = db.execute("SELECT repository_id, owner_id, email, ref, attempts, expires_at FROM codes WHERE handle = ?",
                             (handle,)).fetchone()
            if not row:
                return "not_found", None
            repository_id, owner_id, email, ref, attempts, expires_at = row
            if expires_at < now:
                db.execute("DELETE FROM codes WHERE handle = ?", (handle,))
                return "expired", None
            if attempts >= CODE_ATTEMPTS:
                return "locked", None
            if not correct():
                db.execute("UPDATE codes SET attempts = attempts + 1 WHERE handle = ?", (handle,))
                return "wrong", CODE_ATTEMPTS - attempts - 1
            db.execute("INSERT OR REPLACE INTO recipients VALUES (?, ?, ?, ?, ?)", (repository_id, owner_id, email, ref, now))
            db.execute("DELETE FROM codes WHERE handle = ?", (handle,))
            return "confirmed", email


def validate_payload(payload):
    fields = {"to", "subject", "text", "html", "attachments"}
    if not isinstance(payload, dict) or not fields <= set(payload) <= fields | {"source"}:
        raise ValueError("Invalid email payload")
    validate_email(payload["to"])
    subject = payload["subject"]
    if (not isinstance(subject, str) or not 1 <= len(subject) <= 180
            or any(ord(char) < 32 or ord(char) == 127 for char in subject)):
        raise ValueError("Invalid subject")
    for name in ("text", "html"):
        if not isinstance(payload[name], str) or len(payload[name].encode("utf-8")) > 500_000:
            raise ValueError("Email body exceeds the relay limit")
    if not payload["text"].strip():
        raise ValueError("Plain text report required")
    attachments = payload["attachments"]
    if not isinstance(attachments, list) or len(attachments) > 1:
        raise ValueError("Only one patch attachment is allowed")
    for attachment in attachments:
        if not isinstance(attachment, dict) or set(attachment) != {"filename", "content"}:
            raise ValueError("Invalid attachment")
        filename = attachment["filename"]
        if not isinstance(filename, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,180}\.patch", filename):
            raise ValueError("Attachment must be a patch with a plain filename")
        try:
            if not isinstance(attachment["content"], str) or len(base64.b64decode(attachment["content"], validate=True)) > 750_000:
                raise ValueError("Invalid patch attachment")
        except (binascii.Error, UnicodeError):
            raise ValueError("Invalid patch attachment") from None
    # A report sent again names the run that produced it, so it shares that run's idempotency key.
    source = payload.get("source")
    if source is not None and (not isinstance(source, dict) or set(source) != {"run_id", "run_attempt"}
                               or any(not isinstance(value, str) or not re.fullmatch(r"\d{1,20}", value) for value in source.values())):
        raise ValueError("Invalid source run")
    return payload


class EmailRelay:
    def __init__(self, api_key, sender, audience, secret, store, recipients=None, limits=None, jwks_client=None):
        if not api_key or not audience.startswith("https://") or len(secret) < 32 or store is None:
            raise ValueError("Configure the Resend key, the relay's HTTPS URL, a relay secret of 32+ characters, and the database")
        if not sender:
            raise ValueError("Set SCOUT_EMAIL_FROM to the noreply address on your verified sending domain")
        self.api_key = api_key
        self.sender = validate_email(sender)
        self.audience = audience.rstrip("/")
        self.secret = secret
        self.store = store
        self.recipients = recipients or {}  # optional operator enrollments, keyed by repository ID
        self.limits = limits or limits_from({})
        self.jwks = jwks_client or jwt.PyJWKClient(JWKS_URL, cache_keys=True, timeout=10)
        self.clock = time.time

    def claims(self, token):
        if not token or len(token) > 16_384:
            raise ValueError("Invalid workflow identity")
        key = self.jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=ISSUER,
                          options={"require": ["iss", "aud", "sub", "exp", "iat", "nbf", "repository",
                                               "repository_id", "repository_owner_id", "workflow_ref", "ref",
                                               "event_name", "run_id", "run_attempt"]})

    def identity(self, environ):
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        if not authorization.startswith("Bearer "):
            return None
        try:
            return self.claims(authorization[7:])
        except (jwt.PyJWTError, ValueError, requests.RequestException, OSError):
            return None

    def workflow(self, claims, ref):
        """The Scout workflow file this token comes from, if it ran on `ref` for an allowed event."""
        for name, events in WORKFLOWS.items():
            if (claims["ref"] == ref and claims["event_name"] in events
                    and claims["workflow_ref"] == f"{claims['repository']}/.github/workflows/{name}@{ref}"):
                return name
        return None

    def enrollment(self, claims):
        """The repository's confirmed recipient: an operator enrollment, or an address its owner confirmed."""
        repository_id = str(claims["repository_id"])
        static = self.recipients.get(repository_id)
        if isinstance(static, dict):
            record = {"owner_id": str(static.get("owner_id", "")), "email": static.get("email", ""),
                      "ref": static.get("ref", "refs/heads/main")}
        else:
            record = self.store.recipient(repository_id)
        # A repository that moved to another owner has to be confirmed again.
        return record if record and record["owner_id"] == str(claims["repository_owner_id"]) else None

    def code(self, handle):
        """The confirmation code for a request, derived rather than stored, so a retried request emails the same code."""
        value = hmac.new(self.secret, handle.encode("ascii"), hashlib.sha256).digest()
        return f"{int.from_bytes(value[:8], 'big') % 1_000_000:06d}"

    @staticmethod
    def read_json(environ, limit=MAX_BODY):
        length = int(environ.get("CONTENT_LENGTH") or "0")
        if not 0 < length <= limit:
            raise TooLarge("Invalid request size")
        if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/json":
            raise ValueError("JSON required")
        return json.loads(environ["wsgi.input"].read(length))

    def provider(self, body, key):
        """Send through Resend. -> 'sent' | 'unknown' | 'in_progress' | 'busy' | 'quota' | 'duplicate' | 'rejected'."""
        try:
            response = requests.post("https://api.resend.com/emails", json=body,
                                     headers={"Authorization": f"Bearer {self.api_key}", "Idempotency-Key": key},
                                     timeout=30, allow_redirects=False)
            status = response.status_code
            if 200 <= status < 300:
                data = response.json()
                return "sent" if isinstance(data, dict) and isinstance(data.get("id"), str) and data["id"] else "unknown"
            if status >= 500:
                return "unknown"
            try:
                name = response.json().get("name")
            except (ValueError, AttributeError):
                name = None
            if status == 429:
                return "quota" if name in {"daily_quota_exceeded", "monthly_quota_exceeded"} else "busy"
            if status == 409:
                return "in_progress" if name == "concurrent_idempotent_requests" else "duplicate"
            return "rejected"
        except (requests.RequestException, ValueError, AttributeError):
            return "unknown"

    def deliver(self, key, kind, owner_id, body, prepare=lambda: None):
        """At most one email per key, within the account's and the shared limits."""
        now = int(self.clock())
        state = self.store.reserve(key, kind, owner_id, self.limits, now)
        if state == "sent":
            return SENT  # a replay or retry of something already delivered: no second email, no quota
        if state in OUTCOMES:
            return OUTCOMES[state]
        prepare()
        outcome = self.provider(body, "scout-" + digest(key))
        if outcome == "sent":
            self.store.finish(key, "sent", int(self.clock()))
            return SENT
        # Only a first attempt that Resend refused is known not to have gone out, so only that frees its place in the
        # limits. A resumed send keeps its reservation and first attempt time whatever this answer was, as does
        # Resend saying it saw this key with other content.
        if state == "new" and outcome not in {"unknown", "in_progress", "duplicate"}:
            self.store.finish(key, "released", int(self.clock()))
        return OUTCOMES[outcome]

    def send(self, environ):
        claims = self.identity(environ)
        if claims is None:
            return UNAUTHORIZED
        payload = validate_payload(self.read_json(environ))
        record = self.enrollment(claims)
        workflow = self.workflow(claims, record["ref"] if record else claims["ref"])
        if not workflow or (workflow == "email.yml") != ("source" in payload):
            return "403 Forbidden", {"error": "This workflow can't send Issue Scout email", "reason": "forbidden"}
        if not record or validate_email(payload["to"]) != validate_email(record["email"]):
            return "403 Forbidden", {"error": "Confirm this address in the Issue Scout app first", "reason": "unverified"}
        run = payload.get("source") or {"run_id": str(claims["run_id"]), "run_attempt": str(claims["run_attempt"])}
        body = {"from": self.sender, "to": [validate_email(payload["to"])], "subject": payload["subject"], "text": payload["text"]}
        if payload["html"]:
            body["html"] = payload["html"]
        if payload["attachments"]:
            body["attachments"] = payload["attachments"]
        # One email per run attempt: replays and retries share the key, even if a caller changes the body.
        key = f"report:{claims['repository_id']}:{run['run_id']}:{run['run_attempt']}"
        return self.deliver(key, "report", str(claims["repository_owner_id"]), body)

    def verify(self, environ):
        claims = self.identity(environ)
        if claims is None:
            return UNAUTHORIZED
        data = self.read_json(environ, 4096)
        if (not isinstance(data, dict) or set(data) != {"to", "handle"} or not isinstance(data["handle"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", data["handle"])):
            raise ValueError("Invalid confirmation request")
        to, handle = validate_email(data["to"]), data["handle"]
        if self.workflow(claims, claims["ref"]) != "email.yml":
            return "403 Forbidden", {"error": "Only the Scout email workflow can ask for a confirmation code", "reason": "forbidden"}
        repository_id, owner_id = str(claims["repository_id"]), str(claims["repository_owner_id"])
        body = {"from": self.sender, "to": [to], "subject": "Your Issue Scout confirmation code", "text": (
            f"Your Issue Scout confirmation code is {self.code(handle)}.\n\n"
            f"Enter it in the Issue Scout app to get scout reports for {claims['repository']} at this address. "
            "The code expires in 30 minutes.\n\n"
            "If you didn't ask for this, ignore this email. Nothing else will be sent to you unless the code is entered.")}
        key = f"code:{repository_id}:{claims['run_id']}:{claims['run_attempt']}"
        return self.deliver(key, "code", owner_id, body, prepare=lambda: self.store.save_code(
            digest(handle), repository_id, owner_id, to, claims["ref"], int(self.clock())))

    def confirm(self, environ):
        data = self.read_json(environ, 1024)
        if (not isinstance(data, dict) or set(data) != {"handle", "code"} or not isinstance(data["handle"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", data["handle"]) or not isinstance(data["code"], str)
                or not re.fullmatch(r"\d{6}", data["code"])):
            raise ValueError("Invalid confirmation")
        handle = data["handle"]
        result, detail = self.store.confirm(digest(handle), lambda: hmac.compare_digest(self.code(handle), data["code"]),
                                            int(self.clock()))
        return {
            "confirmed": ("200 OK", {"status": "confirmed", "email": detail}),
            "wrong": ("422 Unprocessable Entity", {"error": "That code isn't right", "reason": "wrong_code", "attempts_left": detail}),
            "expired": ("410 Gone", {"error": "That code has expired", "reason": "expired"}),
            "locked": ("429 Too Many Requests", {"error": "Too many wrong codes; ask for a new one", "reason": "too_many_attempts"}),
            "not_found": ("404 Not Found", {"error": "No code is waiting for this request", "reason": "not_found"}),
        }[result]

    @staticmethod
    def respond(start_response, status, payload):
        body = json.dumps(payload).encode("utf-8")
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                                ("Cache-Control", "no-store")])
        return [body]

    def __call__(self, environ, start_response):
        routes = {("POST", "/send"): self.send, ("POST", "/verify"): self.verify, ("POST", "/confirm"): self.confirm,
                  ("GET", "/health"): lambda environ: ("200 OK", {"status": "ok"})}
        handler = routes.get((environ.get("REQUEST_METHOD"), environ.get("PATH_INFO")))
        if handler is None:
            return self.respond(start_response, "404 Not Found", {"error": "Not found"})
        try:
            status, payload = handler(environ)
        except TooLarge:
            status, payload = "413 Content Too Large", {"error": "Invalid request size"}
        except (ValueError, TypeError, KeyError, AttributeError):
            status, payload = "400 Bad Request", {"error": "Invalid request or recipient configuration"}
        return self.respond(start_response, status, payload)


def create_app(environ=os.environ):
    return EmailRelay(environ.get("RESEND_API_KEY", ""), environ.get("SCOUT_EMAIL_FROM", ""),
                      environ.get("SCOUT_EMAIL_RELAY_URL", ""), environ.get("SCOUT_EMAIL_RELAY_SECRET", "").encode("utf-8"),
                      Store(environ.get("SCOUT_EMAIL_DB", "")),
                      recipients=json.loads(environ.get("SCOUT_EMAIL_RECIPIENTS") or "{}"), limits=limits_from(environ))
