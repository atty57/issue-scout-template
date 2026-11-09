"""The relay's confirmed recipients, quotas and duplicate protection, and the engine's email switch and resend.

Offline: Resend and GitHub are mocked, and the relay's SQLite store lives in a temporary directory.
"""
import json
import re
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import jwt
import pytest
import requests

import email_delivery
import email_relay
import scout
import test_email_delivery
from test_email_delivery import ENDPOINT, claims, payload
from test_scout_recovery import result_state

NOW = 1_800_000_000  # 2027-01-15, a fixed relay clock
LATE = NOW - NOW % 86_400 + 23 * 3600  # 23:00 UTC that day
signing_key = test_email_delivery.signing_key  # the shared fixture
HANDLE = "a" * 64
OTHER_HANDLE = "b" * 64


@pytest.fixture(autouse=True)
def no_live_calls(monkeypatch):
    monkeypatch.setattr(requests, "get", Mock(side_effect=AssertionError("unexpected live GET")))
    monkeypatch.setattr(requests, "post", Mock(side_effect=AssertionError("unexpected live POST")))


@pytest.fixture
def resend(monkeypatch):
    """Resend accepts every email; tests change `return_value` or `side_effect` for failures."""
    post = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"}))
    monkeypatch.setattr(requests, "post", post)
    return post


def make_relay(signing_key, path, **limits):
    jwks = Mock(get_signing_key_from_jwt=Mock(return_value=SimpleNamespace(key=signing_key.public_key())))
    relay = email_relay.EmailRelay("private-resend-key", "noreply@example.com", ENDPOINT, b"relay-secret-" * 3,
                                   email_relay.Store(path), limits=email_relay.limits_from({}) | limits, jwks_client=jwks)
    relay.clock = lambda: NOW
    return relay


@pytest.fixture
def relay(signing_key, tmp_path):
    return make_relay(signing_key, tmp_path / "relay.sqlite3")


def token(signing_key, workflow="daily.yml", **changes):
    values = {"workflow_ref": f"alice/scout/.github/workflows/{workflow}@refs/heads/main"} | changes
    if workflow == "email.yml":
        values.setdefault("event_name", "workflow_dispatch")
    return jwt.encode(claims(**values), signing_key, algorithm="RS256")


def call(relay, path, body, bearer=None):
    data = json.dumps(body).encode()
    environ = {"PATH_INFO": path, "REQUEST_METHOD": "POST", "CONTENT_TYPE": "application/json",
               "CONTENT_LENGTH": str(len(data)), "wsgi.input": __import__("io").BytesIO(data)}
    if bearer:
        environ["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
    start = Mock()
    return start.call_args.args[0] if (result := json.loads(b"".join(relay(environ, start)))) is not None and start.call_args else None, result


def request_code(relay, signing_key, resend, handle=HANDLE, to="dev@example.com", **changes):
    status, result = call(relay, "/verify", {"to": to, "handle": handle}, token(signing_key, "email.yml", **changes))
    code = re.search(r"code is (\d{6})", resend.call_args.kwargs["json"]["text"]).group(1) if status == "200 OK" else None
    return status, result, code


def confirmed(relay, signing_key, resend, to="dev@example.com", **changes):
    _, _, code = request_code(relay, signing_key, resend, to=to, **changes)
    assert call(relay, "/confirm", {"handle": HANDLE, "code": code})[0] == "200 OK"
    resend.reset_mock()


def send(relay, signing_key, run_id="123", **changes):
    return call(relay, "/send", payload(**changes.pop("body", {})), token(signing_key, run_id=run_id, **changes))


# Authorization and recipient restrictions

def test_an_unconfirmed_repository_cannot_send_and_nothing_reaches_resend(relay, signing_key, resend):
    status, result = send(relay, signing_key)
    assert status == "403 Forbidden" and result["reason"] == "unverified"
    resend.assert_not_called()


def test_a_code_confirms_one_address_for_one_repository_and_owner(relay, signing_key, resend):
    status, result, code = request_code(relay, signing_key, resend)
    assert status == "200 OK" and result == {"status": "sent"}
    sent = resend.call_args.kwargs["json"]
    assert sent["from"] == "noreply@example.com" and sent["to"] == ["dev@example.com"] and "alice/scout" in sent["text"]
    assert resend.call_args.kwargs["headers"]["Authorization"] == "Bearer private-resend-key"
    assert call(relay, "/confirm", {"handle": HANDLE, "code": code}) == ("200 OK", {"status": "confirmed", "email": "dev@example.com"})
    resend.reset_mock()
    assert send(relay, signing_key) == ("200 OK", {"status": "sent"})
    assert send(relay, signing_key, run_id="124", body={"to": "someone-else@example.com"})[1]["reason"] == "unverified"
    assert send(relay, signing_key, run_id="125", repository_id="99999")[1]["reason"] == "unverified"
    assert send(relay, signing_key, run_id="126", repository_owner_id="555")[1]["reason"] == "unverified"  # transferred
    assert resend.call_count == 1


@pytest.mark.parametrize("changes", [{"event_name": "push"}, {"workflow_ref": "alice/scout/.github/workflows/other.yml@refs/heads/main"},
                                     {"ref": "refs/heads/feature", "workflow_ref": "alice/scout/.github/workflows/daily.yml@refs/heads/feature"}])
def test_only_the_scout_workflows_on_the_confirmed_branch_can_send(relay, signing_key, resend, changes):
    confirmed(relay, signing_key, resend)
    status, result = send(relay, signing_key, **changes)
    assert status == "403 Forbidden" and result["reason"] in {"forbidden", "unverified"}
    resend.assert_not_called()


def test_codes_come_only_from_the_email_workflow_and_need_a_valid_request(relay, signing_key, resend):
    assert call(relay, "/verify", {"to": "dev@example.com", "handle": HANDLE}, token(signing_key))[1]["reason"] == "forbidden"
    assert call(relay, "/verify", {"to": "dev@example.com", "handle": HANDLE})[0] == "401 Unauthorized"
    assert call(relay, "/verify", {"to": "dev@example.com", "handle": "short"}, token(signing_key, "email.yml"))[0] == "400 Bad Request"
    resend.assert_not_called()


def test_wrong_codes_run_out_and_old_or_expired_codes_stop_working(relay, signing_key, resend, tmp_path):
    _, _, code = request_code(relay, signing_key, resend)
    wrong = "000000" if code != "000000" else "111111"
    for left in (4, 3, 2, 1, 0):
        assert call(relay, "/confirm", {"handle": HANDLE, "code": wrong}) == (
            "422 Unprocessable Entity", {"error": "That code isn't right", "reason": "wrong_code", "attempts_left": left})
    assert call(relay, "/confirm", {"handle": HANDLE, "code": code})[1]["reason"] == "too_many_attempts"
    # A newer request replaces the older code; a code also expires after 30 minutes.
    request_code(relay, signing_key, resend, run_id="200")
    _, _, newer = request_code(relay, signing_key, resend, handle=OTHER_HANDLE, run_id="201")
    assert call(relay, "/confirm", {"handle": HANDLE, "code": code})[1]["reason"] == "not_found"
    relay.clock = lambda: NOW + email_relay.CODE_SECONDS + 1
    assert call(relay, "/confirm", {"handle": OTHER_HANDLE, "code": newer})[1]["reason"] == "expired"
    assert call(relay, "/confirm", {"handle": OTHER_HANDLE, "code": "12345"})[0] == "400 Bad Request"


def test_a_retried_code_request_emails_the_same_code_once(relay, signing_key, resend):
    first = request_code(relay, signing_key, resend)
    assert request_code(relay, signing_key, resend) == first  # same run: answered from the store
    assert resend.call_count == 1


# Quotas

def test_each_account_has_daily_and_monthly_report_limits(signing_key, tmp_path, resend):
    relay = make_relay(signing_key, tmp_path / "relay.sqlite3", report_daily=2, report_monthly=3)
    confirmed(relay, signing_key, resend)
    assert [send(relay, signing_key, run_id=str(run))[0] for run in (1, 2, 3)] == ["200 OK", "200 OK", "429 Too Many Requests"]
    assert send(relay, signing_key, run_id="4")[1]["reason"] == "user_limit"
    relay.clock = lambda: NOW + 86_400  # the next UTC day, same month
    assert send(relay, signing_key, run_id="5")[0] == "200 OK"
    assert send(relay, signing_key, run_id="6")[1]["reason"] == "user_limit"  # monthly limit
    assert resend.call_count == 3


def test_the_shared_quota_keeps_headroom_for_confirmation_codes_and_persists(signing_key, tmp_path, resend):
    path = tmp_path / "relay.sqlite3"
    limits = {"daily": 10, "code_reserve": 20, "report_daily": 100, "code_daily": 100}  # only the shared quota applies
    relay = make_relay(signing_key, path, **limits)
    confirmed(relay, signing_key, resend)  # 1 code used
    results = [send(relay, signing_key, run_id=str(run))[1] for run in range(1, 10)]
    assert [result.get("reason") for result in results] == [None] * 7 + ["quota", "quota"]  # reports stop at 8 of 10
    # A restarted relay reads the same counts from disk, and codes can still use the reserved headroom.
    restarted = make_relay(signing_key, path, **limits)
    assert send(restarted, signing_key, run_id="20")[1]["reason"] == "quota"
    assert request_code(restarted, signing_key, resend, repository_id="777", run_id="30")[0] == "200 OK"
    assert request_code(restarted, signing_key, resend, repository_id="778", run_id="31")[0] == "200 OK"
    assert request_code(restarted, signing_key, resend, repository_id="779", run_id="32")[1]["reason"] == "quota"


def test_each_account_has_a_limit_on_confirmation_codes(signing_key, tmp_path, resend):
    relay = make_relay(signing_key, tmp_path / "relay.sqlite3", code_daily=2)
    assert [request_code(relay, signing_key, resend, run_id=str(run))[0] for run in (1, 2, 3)] == [
        "200 OK", "200 OK", "429 Too Many Requests"]
    assert resend.call_count == 2


def test_limits_come_from_the_environment_and_reject_junk():
    assert email_relay.limits_from({"SCOUT_EMAIL_DAILY_LIMIT": "50"})["daily"] == 50
    for bad in ({"SCOUT_EMAIL_DAILY_LIMIT": "lots"}, {"SCOUT_EMAIL_USER_DAILY_LIMIT": "-1"}, {"SCOUT_EMAIL_CODE_RESERVE_PERCENT": "95"}):
        with pytest.raises(ValueError):
            email_relay.limits_from(bad)


# Duplicate requests

def test_a_delivered_report_is_never_sent_or_counted_twice(signing_key, tmp_path, resend):
    relay = make_relay(signing_key, tmp_path / "relay.sqlite3", report_daily=2)
    confirmed(relay, signing_key, resend)
    assert send(relay, signing_key)[0] == "200 OK"
    for _ in range(3):  # replays, including a changed body, answer "sent" without Resend or quota
        assert send(relay, signing_key, body={"text": "Changed"}) == ("200 OK", {"status": "sent"})
    assert send(relay, signing_key, run_id="2")[0] == "200 OK"  # the second of two allowed today
    assert resend.call_count == 2


def test_a_report_sent_again_from_the_app_shares_its_runs_email(relay, signing_key, resend):
    confirmed(relay, signing_key, resend)
    assert send(relay, signing_key, run_id="123")[0] == "200 OK"
    again = call(relay, "/send", payload(source={"run_id": "123", "run_attempt": "1"}), token(signing_key, "email.yml", run_id="900"))
    assert again == ("200 OK", {"status": "sent"}) and resend.call_count == 1
    # The email workflow must name the run; the scout workflow can't borrow another run's key.
    assert call(relay, "/send", payload(), token(signing_key, "email.yml", run_id="901"))[1]["reason"] == "forbidden"
    assert call(relay, "/send", payload(source={"run_id": "1", "run_attempt": "1"}), token(signing_key))[1]["reason"] == "forbidden"


def test_a_send_still_in_progress_isnt_started_twice(relay, signing_key, resend):
    confirmed(relay, signing_key, resend)
    resend.side_effect = requests.Timeout("slow")
    assert send(relay, signing_key)[1]["reason"] == "unavailable"
    resend.side_effect = None
    assert send(relay, signing_key)[1]["reason"] == "in_progress"  # still within the in-flight window
    assert resend.call_count == 1


# Failure recovery

def test_an_unknown_outcome_is_retried_later_with_the_same_idempotency_key(relay, signing_key, resend):
    confirmed(relay, signing_key, resend)
    resend.return_value = SimpleNamespace(status_code=502, json=lambda: {})
    assert send(relay, signing_key)[0] == "503 Service Unavailable"
    relay.clock = lambda: NOW + email_relay.IN_FLIGHT_SECONDS + 1
    resend.return_value = SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"})
    assert send(relay, signing_key) == ("200 OK", {"status": "sent"})
    keys = {call.kwargs["headers"]["Idempotency-Key"] for call in resend.call_args_list}
    assert len(keys) == 1 and resend.call_count == 2


@pytest.mark.parametrize("limits,reason", [({"report_daily": 1}, "user_limit"), ({"daily": 1, "code_reserve": 0}, "quota")])
def test_a_send_left_unfinished_on_an_earlier_day_must_fit_todays_limits(signing_key, tmp_path, resend, limits, reason):
    relay = make_relay(signing_key, tmp_path / "relay.sqlite3", **limits)
    relay.clock = lambda: NOW - 86_400  # confirmed the day before, so the code doesn't use day 0's quota
    confirmed(relay, signing_key, resend)
    relay.clock = lambda: LATE
    resend.return_value = SimpleNamespace(status_code=502, json=lambda: {})
    assert send(relay, signing_key, run_id="1")[1]["reason"] == "unavailable"  # unknown outcome: stays reserved
    resend.return_value = SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"})
    relay.clock = lambda: LATE + 2 * 3600  # 01:00 the next UTC day, inside Resend's 24 hours
    assert send(relay, signing_key, run_id="2")[0] == "200 OK"  # uses up the next day's allowance
    calls = resend.call_count
    assert send(relay, signing_key, run_id="1")[1]["reason"] == reason
    assert resend.call_count == calls


@pytest.mark.parametrize("reply,reason", [(SimpleNamespace(status_code=502, json=lambda: {}), "unavailable"),
                                          (SimpleNamespace(status_code=409, json=lambda: {"name": "invalid_idempotent_request"}), "duplicate")])
def test_a_send_that_may_have_gone_out_is_retried_only_while_resend_remembers_its_key(relay, signing_key, resend, reply, reason):
    confirmed(relay, signing_key, resend)
    relay.clock = lambda: LATE
    resend.return_value = reply
    assert [send(relay, signing_key, run_id=run)[1]["reason"] for run in ("1", "2")] == [reason, reason]
    key = resend.call_args_list[0].kwargs["headers"]["Idempotency-Key"]
    relay.clock = lambda: LATE + 2 * 3600  # run 2 is tried again on the next UTC day, still without an answer
    assert send(relay, signing_key, run_id="2")[1]["reason"] == reason
    resend.return_value = SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"})
    relay.clock = lambda: LATE + email_relay.IDEMPOTENCY_SECONDS - 1  # the next UTC day, still inside the window
    assert send(relay, signing_key, run_id="1") == ("200 OK", {"status": "sent"})
    assert resend.call_args.kwargs["headers"]["Idempotency-Key"] == key
    calls = resend.call_count
    relay.clock = lambda: LATE + email_relay.IDEMPOTENCY_SECONDS  # Resend may have forgotten run 2's key: never sent again
    assert send(relay, signing_key, run_id="2")[1]["reason"] == "unconfirmed"
    again = call(relay, "/send", payload(source={"run_id": "2", "run_attempt": "1"}), token(signing_key, "email.yml", run_id="900"))
    assert again[0] == "409 Conflict" and again[1]["reason"] == "unconfirmed"
    assert resend.call_count == calls


@pytest.mark.parametrize("reply,reason", [({"name": "daily_quota_exceeded"}, "quota"), ({"name": "rate_limit_exceeded"}, "busy")])
def test_resends_own_limits_release_the_reservation(signing_key, tmp_path, resend, reply, reason):
    relay = make_relay(signing_key, tmp_path / "relay.sqlite3", report_daily=1)
    confirmed(relay, signing_key, resend)
    resend.return_value = SimpleNamespace(status_code=429, json=lambda: reply)
    assert send(relay, signing_key)[1]["reason"] == reason
    resend.return_value = SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"})
    assert send(relay, signing_key)[0] == "200 OK"  # the failed attempt didn't use today's one email


@pytest.mark.parametrize("reply,reason", [(SimpleNamespace(status_code=429, json=lambda: {"name": "rate_limit_exceeded"}), "busy"),
                                          (SimpleNamespace(status_code=429, json=lambda: {"name": "daily_quota_exceeded"}), "quota"),
                                          (SimpleNamespace(status_code=422, json=lambda: {"name": "validation_error"}), "rejected")])
def test_a_later_refusal_doesnt_release_a_send_that_may_have_gone_out(signing_key, tmp_path, resend, reply, reason):
    relay = make_relay(signing_key, tmp_path / "relay.sqlite3", report_monthly=1)
    confirmed(relay, signing_key, resend)
    resend.side_effect = requests.Timeout("answer lost")  # Resend may have accepted it
    assert send(relay, signing_key)[1]["reason"] == "unavailable"
    resend.side_effect, resend.return_value = None, reply
    relay.clock = lambda: NOW + email_relay.IN_FLIGHT_SECONDS
    assert send(relay, signing_key)[1]["reason"] == reason
    resend.return_value = SimpleNamespace(status_code=200, json=lambda: {"id": "email-id"})
    calls = resend.call_count
    relay.clock = lambda: NOW + 86_400  # after Resend's 24 hours: never sent again, and it still holds the month's one email
    assert send(relay, signing_key)[1]["reason"] == "unconfirmed"
    assert send(relay, signing_key, run_id="2")[1]["reason"] == "user_limit"
    assert resend.call_count == calls


def test_the_relay_needs_its_sender_secret_and_database(tmp_path):
    base = {"RESEND_API_KEY": "key", "SCOUT_EMAIL_FROM": "noreply@example.com", "SCOUT_EMAIL_RELAY_URL": ENDPOINT,
            "SCOUT_EMAIL_RELAY_SECRET": "x" * 32, "SCOUT_EMAIL_DB": str(tmp_path / "relay.sqlite3")}
    assert email_relay.create_app(base).sender == "noreply@example.com"
    for missing in ("SCOUT_EMAIL_FROM", "SCOUT_EMAIL_RELAY_SECRET", "SCOUT_EMAIL_DB", "RESEND_API_KEY"):
        with pytest.raises(ValueError):
            email_relay.create_app(base | {missing: ""})


# The engine side: the switch, quota answers, and sending a saved report again

@pytest.fixture
def workflow(monkeypatch, tmp_path):
    monkeypatch.setenv("SCOUT_EMAIL_RELAY_URL", ENDPOINT)
    monkeypatch.setattr(email_delivery, "github_identity", Mock(return_value="signed-workflow-identity"))
    monkeypatch.setattr(email_delivery.time, "sleep", Mock())
    monkeypatch.setattr(scout, "SEEN", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(scout, "REPORTS", tmp_path / "reports")
    return tmp_path


def relay_reply(code, **data):
    return Mock(status_code=code, json=Mock(return_value=data))


def test_turning_email_off_stops_sends_but_keeps_the_report_and_its_email(monkeypatch, workflow):
    monkeypatch.setitem(scout.PROFILE, "email", "old@example.com")  # an address left behind
    monkeypatch.setitem(scout.PROFILE, "email_enabled", False)
    scout.notify(result_state())
    record = scout.load_history()[result_state()["issue"]["html_url"]]
    assert record["notification"] == {"status": "disabled", "attempts": 0} and record["status"] == "notified"
    assert "Targeted tests passed" in (scout.REPORTS / "report.txt").read_text()
    saved = email_delivery.load_message(scout.REPORTS / email_delivery.MESSAGE_FILE)
    message, _, _ = scout.compose(result_state())
    assert email_delivery.message_payload(saved, "dev@example.com") == email_delivery.message_payload(message, "dev@example.com")


@pytest.mark.parametrize("reason", ["quota", "user_limit", "unverified"])
def test_quota_and_unconfirmed_answers_arent_retried_and_keep_the_report(monkeypatch, workflow, capsys, reason):
    monkeypatch.setitem(scout.PROFILE, "email", "dev@example.com")
    monkeypatch.setitem(scout.PROFILE, "email_enabled", True)
    monkeypatch.setenv("CI", "true")
    post = Mock(return_value=relay_reply(429 if reason != "unverified" else 403, reason=reason))
    monkeypatch.setattr(requests, "post", post)
    scout.notify(result_state())
    notification = scout.load_history()[result_state()["issue"]["html_url"]]["notification"]
    assert notification["status"] == "failed" and notification["reason"] == reason and post.call_count == 1
    assert (scout.REPORTS / "change.patch").is_file()
    assert f"::warning title=Issue Scout email%3A failed ({reason})::" in capsys.readouterr().out


def test_a_relay_still_sending_is_asked_again(monkeypatch, workflow):
    post = Mock(side_effect=[relay_reply(409, reason="in_progress"), relay_reply(200, status="sent")])
    monkeypatch.setattr(requests, "post", post)
    message, _, _ = scout.compose(result_state())
    assert email_delivery.send_email(message, {"email": "dev@example.com", "email_enabled": True}) == {"status": "sent", "attempts": 2}


def test_resend_emails_the_latest_saved_attempt_without_scouting(monkeypatch, workflow):
    message, _, _ = scout.compose(result_state())
    for attempt in (1, 2):
        folder = workflow / "earlier" / f"scout-result-55-{attempt}"
        folder.mkdir(parents=True)
        (folder / email_delivery.MESSAGE_FILE).write_bytes(message.as_bytes())
    post = Mock(return_value=relay_reply(200, status="sent"))
    monkeypatch.setattr(requests, "post", post)
    profile = {"email": "dev@example.com", "email_enabled": True}
    assert email_delivery.resend(workflow / "earlier", "55", profile) == {"status": "sent", "attempts": 1}
    sent = post.call_args.kwargs["json"]
    assert post.call_args.args == (f"{ENDPOINT}/send",)
    assert sent["source"] == {"run_id": "55", "run_attempt": "2"} and sent["to"] == "dev@example.com"
    assert email_delivery.resend(workflow / "earlier", "56", profile)["error"].startswith("That run's saved report")
    assert email_delivery.resend(workflow / "earlier", "55", profile | {"email_enabled": False})["status"] == "disabled"
    assert post.call_count == 1


def test_the_email_command_needs_no_engine_or_model_code(workflow):
    # The email workflow installs only `requests`; importing the client must not pull in scouting or providers.
    probe = "import email_delivery, sys; print(sorted(m for m in ('scout', 'settings', 'model_providers', 'langgraph') if m in sys.modules))"
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, cwd=scout.ROOT, check=True)
    assert result.stdout.strip() == "[]"


def test_a_confirmation_request_goes_to_the_verify_endpoint(monkeypatch, workflow):
    post = Mock(return_value=relay_reply(200, status="sent"))
    monkeypatch.setattr(requests, "post", post)
    assert email_delivery.request_verification({"email": "dev@example.com"}, HANDLE) == {"status": "sent", "attempts": 1}
    assert post.call_args.args == (f"{ENDPOINT}/verify",) and post.call_args.kwargs["json"] == {"to": "dev@example.com", "handle": HANDLE}
    assert email_delivery.request_verification({"email": ""}, HANDLE)["status"] == "failed"
    assert email_delivery.request_verification({"email": "dev@example.com"}, "not-a-handle")["status"] == "failed"
    assert post.call_count == 1
