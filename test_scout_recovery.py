"""Offline regressions for preserving work through delivery, timeout and cleanup failures."""
import json
import subprocess
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
import requests

import scout
from test_scout_selection import candidate, picker_option
from test_scout_workloads import brief


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(scout, "SEEN", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(scout, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(scout.http, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(scout.email_delivery.requests, "post", Mock(side_effect=AssertionError("unexpected email send")))
    monkeypatch.setattr(scout.email_delivery, "github_identity", Mock(return_value="signed-workflow-identity"))
    monkeypatch.setattr(scout.time, "sleep", Mock())
    monkeypatch.setenv("SCOUT_RECIPIENT_EMAIL", "scout@example.com")
    monkeypatch.setenv("SCOUT_EMAIL_RELAY_URL", "https://mail.example.com/send")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("CI", raising=False)


def result_state(**changes):
    review = {key: "Review detail" for key in scout.BRIEF_SCHEMA["required"]}
    review.update(maintainer_questions=["Expected behavior confirmed"], checklist=["Review patch"])
    result = {"brief": review, "patch": "+return validated_value\n", "passed": True, "reproduces": True,
              "cost": 0, "log": "Targeted tests passed", "regression_log": "SCOUT_REGRESSION:1 assertion failed"}
    return {"issue": picker_option(), "result": result | changes, "why": "Focused regression fix"}


def response(status=200):
    return Mock(status_code=status, json=Mock(return_value={"status": "sent"}))


def test_disconnect_retries_only_after_report_patch_and_pending_history_are_saved(monkeypatch):
    def accepted(*args, **kwargs):
        assert "Targeted tests passed" in (scout.REPORTS / "report.txt").read_text()
        assert (scout.REPORTS / "change.patch").read_text() == "+return validated_value\n"
        record = scout.load_history()[picker_option()["html_url"]]
        assert record["status"] == "completed" and record["notification"]["status"] == "pending"
        return response()

    post = Mock(side_effect=[requests.ConnectionError("disconnected"), response()])
    monkeypatch.setattr(scout.email_delivery.requests, "post", post)
    attempts = []
    def send(*args, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise requests.ConnectionError("disconnected")
        return accepted(*args, **kwargs)
    post.side_effect = send
    scout.notify(result_state())
    record = scout.load_history()[picker_option()["html_url"]]
    assert record["status"] == "notified" and record["attempts"] == 1
    assert record["notification"] == {"status": "sent", "attempts": 2}
    assert post.call_count == 2 and attempts[0]["json"] == attempts[1]["json"]
    assert attempts[0]["json"]["to"] == "scout@example.com"
    assert "from" not in attempts[0]["json"]
    assert attempts[0]["allow_redirects"] is False
    scout.time.sleep.assert_called_once_with(2)


def test_exhausted_email_retries_preserve_patch_without_resolving_the_issue_again(monkeypatch, capsys):
    post = Mock(side_effect=requests.Timeout("temporary timeout"))
    monkeypatch.setattr(scout.email_delivery.requests, "post", post)
    monkeypatch.setenv("CI", "true")
    state = result_state()
    scout.notify(state)
    record = scout.load_history()[state["issue"]["html_url"]]
    assert record["status"] == "completed" and record["attempts"] == 1
    assert record["notification"]["status"] == "failed" and record["notification"]["attempts"] == 3
    assert "retry_after" not in record and not scout.can_retry(record, datetime.now(timezone.utc))
    assert (scout.REPORTS / "change.patch").read_text() == state["result"]["patch"]
    assert "Targeted tests passed" in (scout.REPORTS / "report.txt").read_text()
    assert json.loads((scout.REPORTS / "notification.json").read_text()) == record["notification"]
    assert post.call_count == 3
    assert [call.args for call in scout.time.sleep.call_args_list] == [(2,), (4,)]
    assert "::warning::Email notification failed" in capsys.readouterr().out
    monkeypatch.setattr(scout, "gh", Mock(return_value={"items": [candidate()]}))
    assert not scout.discover({})["candidates"]


@pytest.mark.parametrize("status", [408, 429, 500, 503])
def test_transient_relay_failures_retry(monkeypatch, status):
    post = Mock(side_effect=[response(status), response()])
    monkeypatch.setattr(scout.email_delivery.requests, "post", post)
    assert scout.send_email(scout.compose(result_state())[0]) == {"status": "sent", "attempts": 2}
    assert post.call_count == 2


@pytest.mark.parametrize("status", [301, 400, 401, 403, 413, 422])
def test_permanent_delivery_errors_stop_without_exposing_response_bodies(monkeypatch, capsys, status):
    reply = response(status)
    reply.text = "secret-provider-key-in-error"
    post = Mock(return_value=reply)
    monkeypatch.setattr(scout.email_delivery.requests, "post", post)
    scout.notify(result_state())
    record = scout.load_history()[picker_option()["html_url"]]
    assert record["notification"]["status"] == "failed" and record["notification"]["attempts"] == 1
    assert "secret-provider-key" not in scout.SEEN.read_text() + capsys.readouterr().out
    assert record["status"] == "completed"
    post.assert_called_once()
    scout.time.sleep.assert_not_called()


def test_accepted_delivery_does_not_send_another_copy(monkeypatch):
    post = Mock(return_value=response())
    monkeypatch.setattr(scout.email_delivery.requests, "post", post)
    scout.notify(result_state())
    assert scout.load_history()[picker_option()["html_url"]]["notification"]["status"] == "sent"
    post.assert_called_once()
    scout.time.sleep.assert_not_called()


def test_abrupt_delivery_interruption_keeps_report_patch_and_pending_history(monkeypatch):
    monkeypatch.setattr(scout.email_delivery.requests, "post", Mock(side_effect=KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        scout.notify(result_state())
    assert (scout.REPORTS / "report.txt").is_file() and (scout.REPORTS / "change.patch").is_file()
    assert json.loads((scout.REPORTS / "notification.json").read_text())["status"] == "pending"
    assert scout.load_history()[picker_option()["html_url"]]["status"] == "completed"


def test_failed_solver_retains_retry_schedule_even_if_email_fails(monkeypatch):
    monkeypatch.setattr(scout.email_delivery.requests, "post", Mock(side_effect=requests.ConnectionError("offline")))
    scout.REPORTS.mkdir(parents=True)
    (scout.REPORTS / "change.patch").write_text("Previous patch")
    state = result_state()
    state["result"] = {"error": "clone failed"}
    scout.notify(state)
    record = scout.load_history()[state["issue"]["html_url"]]
    assert record["status"] == "failed" and record["notification"]["status"] == "failed"
    assert scout.parsed_time(record["retry_after"]) > scout.parsed_time(record["attempted_at"])
    assert "clone failed" in (scout.REPORTS / "report.txt").read_text()
    assert not (scout.REPORTS / "change.patch").exists()


def test_secret_gate_covers_saved_report_and_patch(monkeypatch, capsys):
    monkeypatch.delenv("SCOUT_EMAIL_RELAY_URL")
    unknown_token = "ghp_" + "z" * 36
    state = result_state(patch="+" + unknown_token + "\n")
    state["issue"]["title"] = "Fix response containing " + unknown_token
    state["result"]["brief"]["root_cause"] = "Response included " + unknown_token
    scout.notify(state)
    assert unknown_token not in (scout.REPORTS / "report.txt").read_text()
    assert "PATCH WITHHELD" in (scout.REPORTS / "report.txt").read_text()
    assert not (scout.REPORTS / "change.patch").exists()
    assert scout.load_history()[state["issue"]["html_url"]]["status"] == "failed"
    assert unknown_token not in capsys.readouterr().out


def test_credential_shapes_are_removed_from_persisted_failure_history(monkeypatch):
    monkeypatch.delenv("SCOUT_EMAIL_RELAY_URL")
    unknown_token = "ghp_" + "z" * 36
    state = result_state()
    state["why"] += " " + unknown_token
    state["result"] = {"error": "Request failed with " + unknown_token}
    scout.notify(state)
    assert unknown_token not in scout.SEEN.read_text() + (scout.REPORTS / "report.txt").read_text()


def test_validation_timeout_removes_its_container_and_is_not_regression_evidence(monkeypatch):
    calls = []

    def execute(command, **kwargs):
        calls.append(command)
        if command[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(command, 1500, output=scout.TEST_PHASE + "\nSCOUT_REGRESSION:1")
        return subprocess.CompletedProcess(command, 0, "removed", "")

    monkeypatch.setattr(scout.subprocess, "run", execute)
    passed, log = scout.run_validation("/checkout", brief(), "code", "javascript")
    assert not passed and "timed out" in log
    assert scout.TEST_PHASE not in log and "SCOUT_REGRESSION:1" not in log
    name = calls[0][calls[0].index("--name") + 1]
    assert name.startswith("issue-scout-")
    assert calls[1] == ["docker", "rm", "--force", name]


@pytest.mark.parametrize("cleanup_error", [False, True])
def test_cleanup_failure_does_not_mask_original_container_timeout(monkeypatch, capsys, cleanup_error):
    timeout = subprocess.TimeoutExpired("docker", 1500)
    cleanup = subprocess.TimeoutExpired("docker rm", 30) if cleanup_error else subprocess.CompletedProcess("docker rm", 1)
    monkeypatch.setattr(scout.subprocess, "run", Mock(side_effect=[timeout, cleanup]))
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        scout.run_container("/checkout", "true")
    assert caught.value is timeout
    assert "could not remove interrupted container" in capsys.readouterr().out


def test_solver_exception_releases_temporary_clone(monkeypatch, tmp_path):
    checkout = tmp_path / "clone"
    checkout.mkdir()
    (checkout / "download").write_text("Temporary repository data")
    monkeypatch.setattr(scout, "clone", Mock(return_value=str(checkout)))
    monkeypatch.setattr(scout, "claude", Mock(side_effect=RuntimeError("agent unavailable")))
    result = scout.solve({"issue": picker_option()})["result"]
    assert "agent unavailable" in result["error"] and not checkout.exists()


def test_failed_clone_removes_partial_download(monkeypatch, tmp_path):
    checkout = tmp_path / "clone"
    checkout.mkdir()
    monkeypatch.setattr(scout.tempfile, "mkdtemp", Mock(return_value=str(checkout)))
    monkeypatch.setattr(scout, "git", Mock(side_effect=subprocess.CalledProcessError(1, "git clone")))
    with pytest.raises(subprocess.CalledProcessError):
        scout.clone("acme/core")
    assert not checkout.exists()


def test_failed_worktree_setup_releases_patch_file_and_directory(monkeypatch, tmp_path):
    checkout = tmp_path / "baseline"
    checkout.mkdir()
    real_mkstemp = scout.tempfile.mkstemp
    monkeypatch.setattr(scout.tempfile, "mkdtemp", Mock(return_value=str(checkout)))
    monkeypatch.setattr(scout.tempfile, "mkstemp", lambda suffix: real_mkstemp(suffix=suffix, dir=tmp_path))
    monkeypatch.setattr(scout, "git", Mock(side_effect=subprocess.CalledProcessError(1, "git worktree", stderr="disk full")))
    passed, log = scout.fails_without_fix("/checkout", "test patch", brief())
    assert not passed and "disk full" in log
    assert not checkout.exists() and not list(tmp_path.glob("*.patch"))
