"""Offline regressions for preserving work through mail, timeout and cleanup failures."""
import json
import smtplib
import ssl
import subprocess
from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import Mock

import pytest

import scout
from test_scout_selection import candidate, picker_option
from test_scout_workloads import brief


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(scout, "SEEN", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(scout, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(scout.http, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", Mock(side_effect=AssertionError("unexpected email send")))
    monkeypatch.setattr(scout.time, "sleep", Mock())
    monkeypatch.setenv("GMAIL_ADDRESS", "scout@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "sentinel-password-123456789")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("CI", raising=False)


def result_state(**changes):
    review = {key: "Review detail" for key in scout.BRIEF_SCHEMA["required"]}
    review.update(maintainer_questions=["Expected behavior confirmed"], checklist=["Review patch"])
    result = {"brief": review, "patch": "+return validated_value\n", "passed": True, "reproduces": True,
              "cost": 0, "log": "Targeted tests passed", "regression_log": "SCOUT_REGRESSION:1 assertion failed"}
    return {"issue": picker_option(), "result": result | changes, "why": "Focused regression fix"}


def mail_client():
    return Mock(send_message=Mock(return_value={}))


def test_disconnect_retries_fresh_connection_after_report_and_outcome_are_saved(monkeypatch):
    first, second = mail_client(), mail_client()
    first.login.side_effect = smtplib.SMTPServerDisconnected("Connection unexpectedly closed")

    def login(address, password):
        assert "Targeted tests passed" in (scout.REPORTS / "report.txt").read_text()
        assert (scout.REPORTS / "change.patch").read_text() == "+return validated_value\n"
        record = scout.load_history()[picker_option()["html_url"]]
        assert record["status"] == "completed" and record["notification"]["status"] == "pending"

    second.login.side_effect = login
    connect = Mock(side_effect=[first, second])
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", connect)
    scout.notify(result_state())
    record = scout.load_history()[picker_option()["html_url"]]
    assert record["status"] == "notified" and record["attempts"] == 1
    assert record["notification"] == {"status": "sent", "attempts": 2}
    assert connect.call_count == 2
    for call in connect.call_args_list:
        assert call.args == ("smtp.gmail.com", 465) and call.kwargs["timeout"] == 30
        context = call.kwargs["context"]
        assert context.check_hostname and context.verify_mode == ssl.CERT_REQUIRED
    first.send_message.assert_not_called()
    second.send_message.assert_called_once()
    first.close.assert_called_once()
    second.close.assert_called_once()
    scout.time.sleep.assert_called_once_with(2)


def test_exhausted_smtp_retries_preserve_patch_and_do_not_make_verified_fix_retryable(monkeypatch, capsys):
    connect = Mock(side_effect=smtplib.SMTPServerDisconnected("Connection unexpectedly closed"))
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", connect)
    monkeypatch.setenv("CI", "true")
    state = result_state()
    scout.notify(state)
    record = scout.load_history()[state["issue"]["html_url"]]
    assert record["status"] == "completed" and record["attempts"] == 1
    assert record["notification"]["status"] == "failed" and record["notification"]["attempts"] == 3
    assert "retry_after" not in record
    assert not scout.can_retry(record, datetime.now(timezone.utc))
    assert (scout.REPORTS / "change.patch").read_text() == state["result"]["patch"]
    report = (scout.REPORTS / "report.txt").read_text()
    assert "Targeted tests passed" in report and "SCOUT_REGRESSION:1" in report
    assert json.loads((scout.REPORTS / "notification.json").read_text()) == record["notification"]
    assert connect.call_count == 3
    assert [call.args for call in scout.time.sleep.call_args_list] == [(2,), (4,)]
    assert "::warning::Email notification failed" in capsys.readouterr().out
    monkeypatch.setattr(scout, "gh", Mock(return_value={"items": [candidate()]}))
    assert not scout.discover({})["candidates"]


@pytest.mark.parametrize("error", [TimeoutError("socket timeout"), smtplib.SMTPDataError(451, b"temporary failure")])
def test_transient_network_or_server_errors_retry(monkeypatch, error):
    first, second = mail_client(), mail_client()
    first.send_message.side_effect = error
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", Mock(side_effect=[first, second]))
    assert scout.send_email(EmailMessage()) == {"status": "sent", "attempts": 2}
    first.close.assert_called_once()
    second.send_message.assert_called_once()


def test_permanent_authentication_error_stops_and_redacts_secret(monkeypatch, capsys):
    client = mail_client()
    client.login.side_effect = smtplib.SMTPAuthenticationError(535, b"Rejected sentinel-password-123456789")
    connect = Mock(return_value=client)
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", connect)
    scout.notify(result_state())
    record = scout.load_history()[picker_option()["html_url"]]
    assert record["notification"]["status"] == "failed" and record["notification"]["attempts"] == 1
    assert "SMTPAuthenticationError" in record["notification"]["error"]
    assert "sentinel-password" not in scout.SEEN.read_text() + capsys.readouterr().out
    assert "sentinel-password" not in (scout.REPORTS / "notification.json").read_text()
    assert record["status"] == "completed"
    connect.assert_called_once()
    scout.time.sleep.assert_not_called()
    client.send_message.assert_not_called()


def test_refused_recipient_is_recorded_as_failed_delivery(monkeypatch):
    client = mail_client()
    client.send_message.return_value = {"scout@example.com": (550, b"Recipient refused")}
    connect = Mock(return_value=client)
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", connect)
    delivery = scout.send_email(EmailMessage())
    assert delivery["status"] == "failed" and delivery["attempts"] == 1
    assert "SMTPRecipientsRefused" in delivery["error"]
    connect.assert_called_once()


def test_temporary_recipient_failure_retries(monkeypatch):
    first, second = mail_client(), mail_client()
    first.send_message.side_effect = smtplib.SMTPRecipientsRefused({"scout@example.com": (450, b"Try later")})
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", Mock(side_effect=[first, second]))
    assert scout.send_email(EmailMessage()) == {"status": "sent", "attempts": 2}


def test_disconnect_during_cleanup_after_acceptance_does_not_resend(monkeypatch):
    client = mail_client()
    client.close.side_effect = smtplib.SMTPServerDisconnected("cleanup disconnected")
    connect = Mock(return_value=client)
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", connect)
    scout.notify(result_state())
    assert scout.load_history()[picker_option()["html_url"]]["notification"]["status"] == "sent"
    connect.assert_called_once()
    client.send_message.assert_called_once()
    scout.time.sleep.assert_not_called()


def test_abrupt_mail_interruption_keeps_report_patch_and_pending_history(monkeypatch):
    client = mail_client()
    client.login.side_effect = KeyboardInterrupt()
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", Mock(return_value=client))
    with pytest.raises(KeyboardInterrupt):
        scout.notify(result_state())
    assert (scout.REPORTS / "report.txt").is_file() and (scout.REPORTS / "change.patch").is_file()
    assert json.loads((scout.REPORTS / "notification.json").read_text())["status"] == "pending"
    assert scout.load_history()[picker_option()["html_url"]]["status"] == "completed"
    client.close.assert_called_once()


def test_failed_solver_retains_retry_schedule_even_if_email_fails(monkeypatch):
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", Mock(side_effect=smtplib.SMTPServerDisconnected("offline")))
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
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
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
    monkeypatch.delenv("GMAIL_APP_PASSWORD")
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
