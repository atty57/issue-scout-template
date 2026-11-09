"""Recipient-only client for the operator's Resend relay. No user mail passwords.

Also a small CLI for the email workflow (.github/workflows/email.yml), which never scouts or calls a model:
    python email_delivery.py verify <handle>              ask the relay to email a confirmation code
    python email_delivery.py resend <artifacts> <run-id>  email a finished run's saved report again
"""
import base64
import email
import email.policy
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

import requests


MESSAGE_FILE = "message.eml"  # the composed email, saved with each run's report so it can be sent again
ANNOTATION = "Issue Scout email"  # the app reads "<ANNOTATION>: <status> (<reason>)" from the job's annotations
# What a failed delivery means for the user; the relay answers with one of these reasons.
REASONS = {
    "unverified": "This address isn't confirmed for this repository. Confirm it in the Issue Scout app, then send the report again.",
    "user_limit": "You've reached Issue Scout's email limit for now. It resets daily and monthly (UTC).",
    "quota": "Issue Scout's shared email quota is used up for now. It resets daily and monthly (UTC).",
    "busy": "The email service was busy.",
    "in_progress": "The email service was still sending this report.",
    "unavailable": "The email service is temporarily unavailable.",
    "duplicate": "This run's email was already sent with different content.",
    "unconfirmed": "An earlier attempt may already have delivered this report, so it isn't sent again. Check your inbox; the report is also in the Issue Scout app.",
    "rejected": "The email service rejected the message. Contact the Issue Scout operator.",
    "forbidden": "This workflow isn't allowed to send Issue Scout email.",
    "not_configured": "Email is on, but this repository has no SCOUT_EMAIL_RELAY_URL. Turn email off and on again in the Issue Scout app.",
}
TRANSIENT = {"busy", "in_progress", "unavailable"}


def validate_email(value):
    if not isinstance(value, str) or len(value) > 254 or value != value.strip() or value.count("@") != 1:
        raise ValueError("Enter one destination email address")
    local, domain = value.rsplit("@", 1)
    if (not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}", local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise ValueError("Enter one destination email address")
    try:
        domain = domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError("Enter one destination email address") from None
    labels = domain.split(".")
    if (len(local) + len(domain) + 1 > 254 or len(labels) < 2
            or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
        raise ValueError("Enter one destination email address")
    return f"{local}@{domain}"


def email_enabled(profile):
    """Email goes out only when it was turned on; an address left in the profile never sends by itself."""
    return profile.get("email_enabled") is True


def recipient_email(profile):
    value = os.environ.get("SCOUT_RECIPIENT_EMAIL") or profile.get("email", "")
    return validate_email(value) if value else ""


def relay_url():
    """The relay's base HTTPS URL. It is also the audience of the GitHub identity sent to it."""
    value = os.environ.get("SCOUT_EMAIL_RELAY_URL", "").rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("SCOUT_EMAIL_RELAY_URL must be HTTPS without embedded credentials, query, or fragment")
    if parsed.path.endswith("/send"):
        raise ValueError("SCOUT_EMAIL_RELAY_URL is the relay's base URL, without /send")
    return value


def check_configuration(profile):
    if not isinstance(profile.get("email_enabled", False), bool):
        raise ValueError("email_enabled must be true or false")
    if email_enabled(profile) and not recipient_email(profile):
        raise ValueError("Email is on, but profile.json has no destination address")
    recipient_email(profile)
    relay_url()


def message_payload(message, recipient):
    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    payload = {"to": recipient, "subject": str(message.get("Subject", "Issue Scout draft")),
               "text": plain.get_content() if plain else "", "html": html.get_content() if html else "",
               "attachments": []}
    for attachment in message.iter_attachments():
        payload["attachments"].append({"filename": attachment.get_filename(),
                                       "content": base64.b64encode(attachment.get_payload(decode=True)).decode("ascii")})
    return payload


def load_message(path):
    return email.message_from_bytes(Path(path).read_bytes(), policy=email.policy.default)


def github_identity(audience):
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL", "")
    token = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", "")
    parsed = urlsplit(url)
    if (not token or parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".actions.githubusercontent.com")
            or parsed.username is not None or parsed.password is not None):
        raise ValueError("Email delivery requires GitHub Actions identity; local runs keep the report for download")
    try:
        response = requests.get(url, params={"audience": audience}, headers={"Authorization": f"Bearer {token}"},
                                timeout=30, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError
        identity = response.json()["value"]
        if not isinstance(identity, str) or not identity or len(identity) > 16_384:
            raise ValueError
        return identity
    except (requests.RequestException, ValueError, KeyError, TypeError):
        raise ValueError("Could not authenticate email delivery with GitHub Actions identity") from None


def failed(attempts, reason=None, error=None):
    return {"status": "failed", "attempts": attempts, **({"reason": reason} if reason else {}),
            "error": error or REASONS.get(reason, "Email delivery failed")}


def relay_post(path, body):
    """POST to the relay with this run's GitHub identity, retrying only answers that can succeed later."""
    try:
        base = relay_url()
        if not base:
            return failed(0, "not_configured")
        identity = github_identity(base)
    except ValueError as error:
        return failed(0, error=str(error))
    for attempt in range(1, 4):
        try:
            response = requests.post(f"{base}{path}", json=body, headers={"Authorization": f"Bearer {identity}"},
                                     timeout=30, allow_redirects=False)
            status = response.status_code
            try:
                data = response.json()
            except (ValueError, AttributeError):
                data = {}
            data = data if isinstance(data, dict) else {}
            if status == 200 and data.get("status") == "sent":
                return {"status": "sent", "attempts": attempt}
            reason = data.get("reason") if data.get("reason") in REASONS else None
            transient = (status == 200 or status == 408 or status >= 500 or reason in TRANSIENT
                         or (status == 429 and reason not in {"quota", "user_limit"}))
            error = REASONS.get(reason) or (f"Email relay returned HTTP {status}" if status != 200
                                            else "Email relay returned an invalid acknowledgement")
        except (requests.RequestException, ValueError, AttributeError):
            # The relay keeps one Resend idempotency key per run, so ambiguous POST
            # failures can be retried without creating another copy of the email.
            transient, reason, error = True, None, "Email relay connection or acknowledgement failed"
        if not transient or attempt == 3:
            return failed(attempt, reason, error)
        time.sleep(2 ** attempt)


def send_email(message, profile, source=None):
    """Email a composed report. `source` names the earlier run whose report this sends again."""
    if not email_enabled(profile):
        return {"status": "disabled", "attempts": 0}
    try:
        recipient = recipient_email(profile)
        if not recipient:
            raise ValueError("Email is on, but profile.json has no destination address")
        payload = message_payload(message, recipient)
    except ValueError as error:
        return failed(0, error=str(error))
    return relay_post("/send", payload | ({"source": source} if source else {}))


def request_verification(profile, handle):
    """Ask the relay to email a confirmation code for the profile's address (before email is turned on)."""
    try:
        recipient = recipient_email(profile)
        if not recipient:
            raise ValueError("Save an email address in the Issue Scout app first")
        if not isinstance(handle, str) or not re.fullmatch(r"[0-9a-f]{64}", handle):
            raise ValueError("Start the confirmation from the Issue Scout app")
    except ValueError as error:
        return failed(0, error=str(error))
    return relay_post("/verify", {"to": recipient, "handle": handle})


def resend(directory, run_id, profile):
    """Email a finished run's saved report again from its downloaded artifact, without scouting again."""
    if not re.fullmatch(r"\d{1,20}", run_id or ""):
        return failed(0, error="Choose the scout run to email again")
    saved = []
    for path in Path(directory).glob(f"scout-result-{run_id}-*/{MESSAGE_FILE}"):
        if match := re.fullmatch(rf"scout-result-{run_id}-(\d{{1,4}})", path.parent.name):
            saved.append((int(match.group(1)), path))
    if not saved:
        return failed(0, error="That run's saved report has expired or wasn't saved, so it can't be emailed again")
    attempt, path = max(saved)
    return send_email(load_message(path), profile, {"run_id": run_id, "run_attempt": str(attempt)})


def _escape(value, property=False):
    """Workflow command escaping; property values also escape ':' and ','."""
    table = {"%": "%25", "\r": "%0D", "\n": "%0A"} | ({":": "%3A", ",": "%2C"} if property else {})
    return "".join(table.get(char, char) for char in value)


def annotate(delivery):
    """A workflow annotation that both the app and GitHub's run page show."""
    if not os.environ.get("CI"):
        return
    status, reason = delivery["status"], delivery.get("reason")
    message = {"sent": "Your report was emailed.",
               "disabled": "Email is off. The report and patch are saved with this run."}.get(status) or (
        delivery.get("error", "Email delivery failed").rstrip(".") + ". Download the scout-result artifact for the report and patch.")
    title = f"{ANNOTATION}: {status}" + (f" ({reason})" if reason else "")
    print(f"::{'warning' if status == 'failed' else 'notice'} title={_escape(title, True)}::{_escape(message)}")


def main(argv):
    profile = json.loads(Path(os.environ.get("SCOUT_PROFILE", "profile.json")).read_text(encoding="utf-8"))
    if not isinstance(profile, dict):
        raise SystemExit("profile.json must be an object")
    if argv[:1] == ["verify"] and len(argv) == 2:
        delivery = request_verification(profile, argv[1])
    elif argv[:1] == ["resend"] and len(argv) == 3:
        delivery = resend(argv[1], argv[2], profile)
    else:
        raise SystemExit(__doc__)
    annotate(delivery)
    print(f"email: {delivery['status']}" + (f" ({delivery['error']})" if delivery.get("error") else ""))
    return 0 if delivery["status"] == "sent" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
