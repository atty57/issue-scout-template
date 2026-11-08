"""Operator-hosted WSGI Resend relay, authenticated by GitHub Actions OIDC.

Run behind HTTPS using: gunicorn 'email_relay:create_app()'. The owner's
Resend key and repository-to-recipient enrollment stay on this service.
"""
import base64
import binascii
import hashlib
import json
import os
import re

import jwt
import requests

from email_delivery import validate_email


ISSUER = "https://token.actions.githubusercontent.com"
JWKS_URL = ISSUER + "/.well-known/jwks"
MAX_BODY = 2_000_000
DEFAULT_SENDER = "noreply@scouts.email"


def validate_payload(payload):
    if not isinstance(payload, dict) or set(payload) != {"to", "subject", "text", "html", "attachments"}:
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
    return payload


class EmailRelay:
    def __init__(self, api_key, sender, audience, recipients, jwks_client=None):
        if not api_key or not audience.startswith("https://") or not recipients:
            raise ValueError("Configure the Resend key, HTTPS relay audience, and enrolled recipients")
        self.api_key = api_key
        self.sender = validate_email(sender)
        self.audience = audience.rstrip("/")
        self.recipients = recipients
        self.jwks = jwks_client or jwt.PyJWKClient(JWKS_URL, cache_keys=True, timeout=10)

    def claims(self, token):
        if not token or len(token) > 16_384:
            raise ValueError("Invalid workflow identity")
        key = self.jwks.get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256"], audience=self.audience, issuer=ISSUER,
                          options={"require": ["iss", "aud", "sub", "exp", "iat", "nbf", "repository",
                                               "repository_id", "repository_owner_id", "workflow_ref", "ref",
                                               "event_name", "run_id", "run_attempt"]})

    def authorize(self, claims, payload):
        enrollment = self.recipients.get(str(claims["repository_id"]))
        if not isinstance(enrollment, dict):
            return False
        ref = enrollment.get("ref", "refs/heads/main")
        expected_workflow = f"{claims['repository']}/.github/workflows/daily.yml@{ref}"
        return (str(claims["repository_owner_id"]) == str(enrollment.get("owner_id", ""))
                and claims["ref"] == ref and claims["workflow_ref"] == expected_workflow
                and claims["event_name"] in {"schedule", "workflow_dispatch"}
                and validate_email(payload["to"]) == validate_email(enrollment.get("email", "")))

    @staticmethod
    def respond(start_response, status, payload):
        body = json.dumps(payload).encode("utf-8")
        start_response(status, [("Content-Type", "application/json"), ("Content-Length", str(len(body))),
                                ("Cache-Control", "no-store")])
        return [body]

    def __call__(self, environ, start_response):
        if environ.get("PATH_INFO") != "/send" or environ.get("REQUEST_METHOD") != "POST":
            return self.respond(start_response, "404 Not Found", {"error": "Not found"})
        authorization = environ.get("HTTP_AUTHORIZATION", "")
        if not authorization.startswith("Bearer "):
            return self.respond(start_response, "401 Unauthorized", {"error": "Workflow identity required"})
        try:
            claims = self.claims(authorization[7:])
        except (jwt.PyJWTError, ValueError, requests.RequestException, OSError):
            return self.respond(start_response, "401 Unauthorized", {"error": "Invalid workflow identity"})
        try:
            length = int(environ.get("CONTENT_LENGTH", "0"))
            if not 0 < length <= MAX_BODY:
                return self.respond(start_response, "413 Content Too Large", {"error": "Invalid request size"})
            if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/json":
                raise ValueError("JSON required")
            payload = validate_payload(json.loads(environ["wsgi.input"].read(length)))
            if not self.authorize(claims, payload):
                return self.respond(start_response, "403 Forbidden", {"error": "Repository and recipient are not enrolled"})
        except (ValueError, TypeError, KeyError, AttributeError):
            return self.respond(start_response, "400 Bad Request", {"error": "Invalid draft or recipient configuration"})
        # At most one draft per authenticated workflow run/attempt. Replays and
        # retries use the same Resend key, even if a caller changes the body.
        identity = f"{claims['repository_id']}:{claims['run_id']}:{claims['run_attempt']}"
        idempotency_key = "scout-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
        body = {"from": self.sender, "to": [validate_email(payload["to"])],
                "subject": payload["subject"], "text": payload["text"]}
        if payload["html"]:
            body["html"] = payload["html"]
        if payload["attachments"]:
            body["attachments"] = payload["attachments"]
        try:
            response = requests.post("https://api.resend.com/emails", json=body,
                                     headers={"Authorization": f"Bearer {self.api_key}", "Idempotency-Key": idempotency_key},
                                     timeout=30, allow_redirects=False)
            if 200 <= response.status_code < 300:
                if not isinstance(response.json().get("id"), str) or not response.json()["id"]:
                    raise ValueError("Invalid provider acknowledgement")
                return self.respond(start_response, "200 OK", {"status": "sent"})
            if response.status_code == 429:
                return self.respond(start_response, "429 Too Many Requests", {"error": "Sending limit reached"})
            if response.status_code >= 500:
                raise ValueError("Provider temporarily unavailable")
            return self.respond(start_response, "422 Unprocessable Entity", {"error": "Email provider rejected delivery; contact the service operator"})
        except (requests.RequestException, ValueError, AttributeError):
            return self.respond(start_response, "503 Service Unavailable", {"error": "Email provider temporarily unavailable"})


def create_app():
    return EmailRelay(os.environ.get("RESEND_API_KEY", ""), os.environ.get("SCOUT_EMAIL_FROM", DEFAULT_SENDER),
                      os.environ.get("SCOUT_EMAIL_RELAY_URL", ""),
                      json.loads(os.environ.get("SCOUT_EMAIL_RECIPIENTS", "{}")))
