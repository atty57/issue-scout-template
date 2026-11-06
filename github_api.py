"""GitHub REST transport: one session, bounded retries, bounded pagination."""
import os
import time
from email.utils import parsedate_to_datetime

import requests

http = requests.Session()
http.headers["Accept"] = "application/vnd.github+json"
if os.environ.get("GH_TOKEN"):
    http.headers["Authorization"] = f"Bearer {os.environ['GH_TOKEN']}"


def gh_response(path, params=None, headers=None, missing_ok=False):
    waited = 0
    for attempt in range(3):
        try:
            r = http.get(f"https://api.github.com/{path}", params=params, headers=headers, timeout=30)
        except (requests.ConnectionError, requests.Timeout):
            if attempt == 2:
                raise RuntimeError("GitHub request failed after 3 attempts") from None
            if waited + 2 ** attempt > 180:
                raise RuntimeError("GitHub retry delayed; defer request until the server allows retrying") from None
            time.sleep(2 ** attempt)
            waited += 2 ** attempt
            continue
        except requests.RequestException:
            raise RuntimeError("GitHub request failed") from None
        throttled = r.status_code == 429 or r.status_code == 403 and (
            "retry-after" in r.headers or r.headers.get("x-ratelimit-remaining") == "0" or "rate limit" in r.text.lower())
        if (not throttled and r.status_code not in (500, 502, 503, 504)) or attempt == 2:
            break
        delay = retry_wait(r, attempt) if throttled or "retry-after" in r.headers else 2 ** attempt
        if waited + delay > 180:
            raise RuntimeError("GitHub retry delayed; defer request until the server allows retrying")
        time.sleep(delay)
        waited += delay
    if not r.ok and not (missing_ok and r.status_code == 404):
        # Error bodies and URLs can echo credentials; expose status only.
        raise RuntimeError(f"GitHub {r.status_code} request failed")
    return r


def gh(path, **params):
    r = gh_response(path, params=params)
    try:
        return r.json()
    except ValueError:
        raise RuntimeError("GitHub returned invalid JSON") from None


def retry_wait(r, attempt=0):
    """GitHub's rule: honor retry-after, else the reset time, else wait at least a minute."""
    try:
        delay = float(r.headers.get("retry-after", ""))
        if delay >= 0:  # also rejects NaN
            return delay
    except (TypeError, ValueError):
        pass
    try:
        retry_at = parsedate_to_datetime(r.headers.get("retry-after", ""))
        if retry_at.tzinfo is not None:
            return max(retry_at.timestamp() - time.time(), 0)
    except (TypeError, ValueError, OverflowError):
        pass
    if r.headers.get("x-ratelimit-remaining") == "0":
        try:
            reset = float(r.headers.get("x-ratelimit-reset", ""))
            if reset >= 0:
                return max(reset - time.time(), 0) + 1
        except (TypeError, ValueError):
            pass
    return 60 * 2 ** attempt


def gh_file(repo, path):
    r = gh_response(f"repos/{repo}/contents/{path}",
                    headers={"Accept": "application/vnd.github.raw+json"}, missing_ok=True)
    return r.text if r.ok else ""
