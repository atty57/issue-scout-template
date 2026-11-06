"""Offline transport checks through the public GitHub request helpers."""
import json

import pytest
import requests

import github_api


def response(status=200, payload=None, headers=None, text=None):
    result = requests.Response()
    result.status_code = status
    result.headers.update(headers or {})
    result._content = (text if text is not None else json.dumps(payload)).encode()
    result._content_consumed = True
    return result


@pytest.fixture
def transport(monkeypatch):
    calls, sleeps = [], []

    def install(*outcomes):
        outcomes = iter(outcomes)

        def get(url, **kwargs):
            calls.append((url, kwargs))
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(github_api.http, "get", get)
        return calls, sleeps

    monkeypatch.setattr(github_api.time, "sleep", sleeps.append)
    return install


@pytest.mark.parametrize("failure", [requests.ConnectionError, requests.Timeout])
def test_transient_network_failure_can_recover(transport, failure):
    calls, sleeps = transport(failure("temporary outage"), response(payload={"items": []}))

    assert github_api.gh("search/issues", q="is:issue") == {"items": []}
    assert len(calls) == 2
    assert sleeps == [1]


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_server_failure_can_recover(transport, status):
    calls, sleeps = transport(response(status), response(payload={"items": []}))

    assert github_api.gh("search/issues") == {"items": []}
    assert len(calls) == 2
    assert sleeps == [1]


def test_rate_limit_never_retries_before_server_required_delay(transport):
    calls, sleeps = transport(response(429, headers={"Retry-After": "150"}), response(payload=[]))

    assert github_api.gh("search/issues") == []
    assert len(calls) == 2
    assert sleeps == [150]


def test_long_rate_limit_is_deferred_without_retrying_early(transport):
    calls, sleeps = transport(response(429, headers={"Retry-After": "3600"}))

    with pytest.raises(RuntimeError, match="GitHub.*defer"):
        github_api.gh("search/issues")
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("headers", [
    {"Retry-After": "invalid"},
    {"Retry-After": "-4"},
    {"Retry-After": "NaN"},
    {"X-RateLimit-Remaining": "0"},
    {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "broken"},
])
def test_bad_rate_limit_headers_use_a_safe_fallback(headers):
    assert github_api.retry_wait(response(429, headers=headers)) == 60


def test_http_date_retry_after_is_respected(monkeypatch):
    monkeypatch.setattr(github_api.time, "time", lambda: 1445412440)
    assert github_api.retry_wait(response(429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 40


@pytest.mark.parametrize("status,body", [(403, "secondary rate limit exceeded"), (429, "Too many requests")])
def test_repeated_secondary_throttling_backs_off_and_stops(transport, status, body):
    calls, sleeps = transport(*(response(status, text=body) for _ in range(3)))

    with pytest.raises(RuntimeError, match=f"GitHub {status}"):
        github_api.gh("search/issues")
    assert len(calls) == 3
    assert sleeps == [60, 120]


def test_invalid_success_json_has_a_predictable_error_without_the_body(transport):
    calls, sleeps = transport(response(text="private response data"))

    with pytest.raises(RuntimeError, match="GitHub returned invalid JSON") as caught:
        github_api.gh("search/issues")
    assert "private response data" not in str(caught.value)
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_nonretryable_errors_do_not_echo_request_or_response_secrets(transport, status):
    calls, sleeps = transport(response(status, text="private response data"))

    with pytest.raises(RuntimeError, match=f"GitHub {status}") as caught:
        github_api.gh("search/issues?access_token=private-request-data")
    assert "private" not in str(caught.value)
    assert len(calls) == 1
    assert sleeps == []


def test_unavailable_server_retry_after_is_respected(transport):
    calls, sleeps = transport(response(503, headers={"Retry-After": "150"}), response(payload=[]))

    assert github_api.gh("search/issues") == []
    assert len(calls) == 2
    assert sleeps == [150]


def test_other_request_errors_fail_predictably_without_echoing_credentials(transport):
    calls, sleeps = transport(requests.RequestException("Authorization: private credential"))

    with pytest.raises(RuntimeError, match="GitHub request failed") as caught:
        github_api.gh("search/issues")
    assert "private" not in str(caught.value)
    assert len(calls) == 1
    assert sleeps == []


def test_primary_limit_waits_past_reset_time(transport, monkeypatch):
    monkeypatch.setattr(github_api.time, "time", lambda: 1000)
    calls, sleeps = transport(response(403, headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1030"}),
                             response(payload=[]))

    assert github_api.gh("search/issues") == []
    assert len(calls) == 2
    assert sleeps == [31]


def test_valid_retry_after_takes_precedence_over_reset(monkeypatch):
    monkeypatch.setattr(github_api.time, "time", lambda: 1000)
    assert github_api.retry_wait(response(403, headers={"Retry-After": "4", "X-RateLimit-Remaining": "0",
                                                       "X-RateLimit-Reset": "1100"})) == 4


def test_bad_retry_after_falls_back_to_valid_primary_reset(monkeypatch):
    monkeypatch.setattr(github_api.time, "time", lambda: 1000)
    assert github_api.retry_wait(response(403, headers={"Retry-After": "broken", "X-RateLimit-Remaining": "0",
                                                       "X-RateLimit-Reset": "1010"})) == 11


def test_total_sleep_budget_does_not_restart_for_each_attempt(transport):
    calls, sleeps = transport(response(429, headers={"Retry-After": "150"}),
                             response(429, headers={"Retry-After": "40"}))

    with pytest.raises(RuntimeError, match="GitHub.*defer"):
        github_api.gh("search/issues")
    assert len(calls) == 2
    assert sleeps == [150]


@pytest.mark.parametrize("failure", [requests.Timeout, requests.ConnectionError])
def test_network_retry_cannot_exceed_a_budget_used_by_rate_limit_wait(transport, failure):
    calls, sleeps = transport(response(429, headers={"Retry-After": "180"}), failure("temporary outage"),
                             response(payload=[]))

    with pytest.raises(RuntimeError, match="GitHub.*defer"):
        github_api.gh("search/issues")
    assert len(calls) == 2
    assert sleeps == [180]


@pytest.mark.parametrize("failure", [requests.Timeout("private credential"), requests.ConnectionError("private credential"),
                                     response(500, text="private credential")])
def test_transient_failures_stop_after_three_attempts(transport, failure):
    calls, sleeps = transport(failure, failure, failure)

    with pytest.raises(RuntimeError, match="GitHub") as caught:
        github_api.gh("search/issues")
    assert "private" not in str(caught.value)
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_success_preserves_response_and_request_options(transport):
    expected = response(payload={"items": []})
    calls, sleeps = transport(expected)

    assert github_api.gh_response("search/issues", params={"q": "is:issue"}, headers={"X-Test": "value"}) is expected
    assert calls == [("https://api.github.com/search/issues", {
        "params": {"q": "is:issue"}, "headers": {"X-Test": "value"}, "timeout": 30})]
    assert sleeps == []


def test_missing_ok_returns_only_missing_response(transport):
    missing = response(404)
    calls, sleeps = transport(missing, response(401))

    assert github_api.gh_response("repos/org/repo/issues/1", missing_ok=True) is missing
    with pytest.raises(RuntimeError, match="GitHub 401"):
        github_api.gh_response("repos/org/repo/issues/1", missing_ok=True)
    assert len(calls) == 2
    assert sleeps == []


def test_file_content_remains_raw_and_missing_file_remains_empty(transport):
    calls, sleeps = transport(response(text="raw source without JSON"), response(404))

    assert github_api.gh_file("org/repo", "README.md") == "raw source without JSON"
    assert github_api.gh_file("org/repo", "MISSING.md") == ""
    assert calls[0][1]["headers"] == {"Accept": "application/vnd.github.raw+json"}
    assert sleeps == []
