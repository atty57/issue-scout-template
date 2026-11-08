"""Secret hygiene: redact the values we hold, then fail closed on anything still
credential-shaped. Named redaction, not secrets, so it cannot shadow the stdlib secrets module."""
import json
import os
import re
from base64 import b64encode
from urllib.parse import quote

from settings import SECRETS


def scrub(payload):
    """LangSmith keeps whatever it is handed, so redact before anything leaves the process."""
    return json.loads(redact(json.dumps(payload, default=str)))


def forms(value):
    """The same secret as something downstream might have written it: raw, base64, URL-encoded."""
    return (value, b64encode(value.encode()).decode(), quote(value, safe=""))


def redact(text):
    values = {form for name in SECRETS if (value := os.environ.get(name)) and len(value) >= 12
              for form in forms(value)}
    # A provider key may be a prefix of another credential. Replace longer values
    # first so no suffix survives after the shorter credential has been removed.
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


# Shapes that must never leave this process. redact() only knows the exact values it was handed, so
# anything the agent read elsewhere, or rewrote, needs catching by shape instead.
LEAK_PATTERNS = {
    "github token": r"gh[pousr]_[A-Za-z0-9]{20,}",
    "anthropic key": r"sk-ant-[A-Za-z0-9_-]{20,}",
    "groq key": r"gsk_[A-Za-z0-9]{20,}",
    "resend key": r"\bre_[A-Za-z0-9_-]{20,}",
    "openai key": r"sk-(?!ant-)(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}",
    "slack token": r"xox[baprs]-[A-Za-z0-9-]{10,}",
    "aws key id": r"AKIA[0-9A-Z]{16}",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "bearer header": r"[Aa]uthorization[\"']?\s*[:=]\s*[\"']?Bearer [A-Za-z0-9._~+/-]{12,}",
    "credential assignment": r"(?:TOKEN|SECRET|API_?KEY|PASSWORD|PASSWD)[\"']?\s*[:=]\s*[\"']?[A-Za-z0-9/+_.-]{16,}",
    "app password": r"(?i:password)[^\n]{0,16}?\b[a-z]{4} [a-z]{4} [a-z]{4} [a-z]{4}\b",
}


def scan_secrets(text):
    return sorted(name for name, pattern in LEAK_PATTERNS.items() if re.search(pattern, text))


def redact_shapes(text):
    text = redact(text)
    for pattern in LEAK_PATTERNS.values():
        text = re.sub(pattern, "[redacted]", text)
    return text


def outbound(body, patch):
    """The only way text leaves this process: redact known secrets, then fail closed on anything
    still credential-shaped. Returns (body, patch) with the patch dropped if it was not clean."""
    body, patch = redact(body), redact(patch or "")
    hits = scan_secrets(body + patch)
    if hits:
        print(f"notify: withheld the patch ({', '.join(hits)})")
        body = redact_shapes(body)
        return f"{body}\n\nPATCH WITHHELD: {', '.join(hits)} matched in the output.", ""
    return body, patch
