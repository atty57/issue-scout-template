"""Recipient-only client for the operator's Resend relay. No user mail passwords."""
import base64
import os
import re
import time
from urllib.parse import urlsplit

import requests


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


def recipient_email(profile):
    value = os.environ.get("SCOUT_RECIPIENT_EMAIL") or profile.get("email", "")
    return validate_email(value) if value else ""


def relay_url():
    value = os.environ.get("SCOUT_EMAIL_RELAY_URL", "").rstrip("/")
    if not value:
        return ""
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment):
        raise ValueError("SCOUT_EMAIL_RELAY_URL must be HTTPS without embedded credentials, query, or fragment")
    return value


def check_configuration(profile):
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


def send_email(message, profile):
    try:
        recipient, endpoint = recipient_email(profile), relay_url()
        if not recipient or not endpoint:
            return {"status": "dry_run", "attempts": 0}
        identity = github_identity(endpoint)
        payload = message_payload(message, recipient)
    except ValueError as error:
        return {"status": "failed", "attempts": 0, "error": str(error)}
    for attempt in range(1, 4):
        try:
            response = requests.post(endpoint, json=payload, headers={"Authorization": f"Bearer {identity}"},
                                     timeout=30, allow_redirects=False)
            status = response.status_code
            if status == 200 and response.json().get("status") == "sent":
                return {"status": "sent", "attempts": attempt}
            transient = status in {200, 408, 429} or status >= 500
            error = f"Email relay returned HTTP {status}" if status != 200 else "Email relay returned an invalid acknowledgement"
        except (requests.RequestException, ValueError, AttributeError):
            # The relay uses a stable Resend idempotency key, so ambiguous POST
            # failures can be retried without creating another copy of the email.
            transient, error = True, "Email relay connection or acknowledgement failed"
        if not transient or attempt == 3:
            return {"status": "failed", "attempts": attempt, "error": error}
        time.sleep(2 ** attempt)
