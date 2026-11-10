"""The versioned run result (reports/result.json) that the desktop app reads. It holds only what the run
produced, after the same redaction and patch rules as the email, and says plainly when there is no fix."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import scout
from redaction import scan_secrets
from test_scout_recovery import result_state
from test_scout_selection import picker_option, picker_verdict

SECRET = "groq-secret-value-1234567890"  # a configured credential's exact value
TOKEN = "ghp_" + "a" * 36  # credential-shaped text the model or a test printed
PLAN = {"runtime": "python312", "setup_commands": [["python", "-m", "pip", "install", "-e", "."]],
        "test_commands": [["python", "-m", "pytest", "tests/test_validation.py"]], "regression_marker": "SCOUT_REGRESSION:1"}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(scout, "SEEN", tmp_path / "state" / "seen.json")
    monkeypatch.setattr(scout, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(scout.http, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(scout.email_delivery.requests, "post", Mock(side_effect=AssertionError("unexpected email send")))
    monkeypatch.setitem(scout.PROFILE, "email_enabled", False)
    monkeypatch.setenv("GROQ_API_KEY", SECRET)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


def saved():
    text = (scout.REPORTS / "result.json").read_text(encoding="utf-8")
    assert not scan_secrets(text) and SECRET not in text
    return json.loads(text)


def fixed(**changes):
    state = result_state(fix_attempts=2, **changes)
    state["result"]["brief"]["validation"] = PLAN
    return state


def test_a_verified_fix_records_the_issue_brief_validation_and_patch():
    state = fixed()
    scout.notify(state)
    result = saved()
    assert set(result) == {"schema_version", "outcome", "reason", "issue", "fix", "report_file", "patch_file",
                           "patch_withheld_reason", "notification"}
    assert result["schema_version"] == 1 and result["outcome"] == "fix_verified"
    issue = picker_option()
    assert result["issue"] == {
        "url": issue["html_url"], "repo": "acme/core", "number": 1, "title": issue["title"], "labels": ["bug"],
        "repo_stars": None, "updated_at": issue["updated_at"], "body_excerpt": issue["body"],
        "work_type": "code", "language": "python", "validation_plan": None, "why": "Focused regression fix",
        "score": scout.score(issue),
        "score_components": scout.score_components(issue), "confidence": None,
        "ai_policy": {"status": "none", "quote": ""}, "feasibility": "Python package and pytest suite"}
    fix = result["fix"]
    assert fix["tests_passed"] is True and fix["regression_reproduced"] is True and fix["documentation_verified"] is False
    assert fix["runtime"] == "python312" and fix["test_commands"] == PLAN["test_commands"]
    assert fix["setup_commands"] == PLAN["setup_commands"] and fix["fix_attempts"] == 2
    assert fix["root_cause"] == "Review detail" and fix["maintainer_questions"] == ["Expected behavior confirmed"]
    assert fix["validation_log"] == "Targeted tests passed" and fix["cost_estimate"] == 0
    assert result["patch_file"] == "change.patch" and result["patch_withheld_reason"] is None
    assert (scout.REPORTS / "change.patch").read_text() == state["result"]["patch"]
    assert result["report_file"] == "report.txt" and (scout.REPORTS / "report.txt").exists()
    assert result["notification"] == {"status": "disabled", "attempts": 0}
    # The issue discussion and the solver's session stay out; only the issue's own text is excerpted.
    assert "regression test for nested None" not in json.dumps(result)
    scout.finish(state)  # the graph's final state: a run that reached notify keeps its result
    assert saved() == result


def test_secret_shaped_output_withholds_the_patch_and_is_redacted_everywhere():
    state = fixed(patch=f"+token = '{TOKEN}'\n", log=f"settings printed {SECRET}")
    state["result"]["brief"]["root_cause"] = "Found sk-ant-" + "b" * 30 + " in the config"
    scout.notify(state)
    result = saved()  # no credential shapes and no configured secret anywhere in the file
    assert result["patch_file"] is None and not (scout.REPORTS / "change.patch").exists()
    assert "github token" in result["patch_withheld_reason"]
    assert result["outcome"] == "fix_not_verified" and "withheld" in result["reason"]
    assert result["fix"]["root_cause"] == "Found [redacted] in the config"
    assert result["fix"]["validation_log"] == "settings printed [redacted]"


def test_a_failed_fix_attempt_is_reported_without_an_invented_fix():
    state = {"issue": picker_option(), "why": "Focused regression fix",
             "result": {"error": f"RuntimeError: claude-code failed ({SECRET})"}}
    scout.notify(state)
    result = saved()
    assert result["outcome"] == "no_fix" and result["reason"] == "RuntimeError: claude-code failed ([redacted])"
    assert result["fix"] is None and result["patch_file"] is None and result["patch_withheld_reason"] is None
    assert result["issue"]["number"] == 1 and result["report_file"] == "report.txt"


@pytest.mark.parametrize("changes,reason", [({"passed": False}, "tests failed"),
                                            ({"reproduces": False}, "unfixed code"),
                                            ({"patch": ""}, "no changes")])
def test_an_unverified_fix_says_why_it_isnt_verified(changes, reason):
    scout.notify(fixed(**changes))
    result = saved()
    assert result["outcome"] == "fix_not_verified" and reason in result["reason"].lower()
    assert result["fix"]["tests_passed"] is changes.get("passed", True)
    assert (result["patch_file"] == "change.patch") is bool(changes.get("patch", True))


def test_a_run_that_picks_nothing_says_so_and_sends_nothing(monkeypatch):
    earlier = {"why": "Focused regression fix"}  # a pick whose issue then failed revalidation
    scout.finish(earlier | scout.pick({"candidates": []}))  # how the graph merges the last pick into its state
    result = saved()
    assert result == {"schema_version": 1, "outcome": "no_pick",
                      "reason": "No issue was picked: no candidate passed the profile, score and feasibility checks",
                      "issue": None, "fix": None, "report_file": None, "patch_file": None,
                      "patch_withheld_reason": None, "notification": None}
    model = Mock()
    model.invoke.return_value = picker_verdict(index=None, why=f"Nothing fits the profile today {SECRET}")
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    scout.finish(scout.pick({"candidates": [picker_option()]}))
    assert saved()["reason"] == "No issue was picked: Nothing fits the profile today [redacted]"
    assert sorted(path.name for path in scout.REPORTS.iterdir()) == ["result.json"]  # no report, patch or email


def test_the_result_lands_before_the_email_and_then_records_its_outcome(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "email_enabled", True)
    monkeypatch.setenv("SCOUT_RECIPIENT_EMAIL", "scout@example.com")
    monkeypatch.setenv("SCOUT_EMAIL_RELAY_URL", "https://mail.example.com")
    monkeypatch.setattr(scout.email_delivery, "github_identity", Mock(return_value="signed-workflow-identity"))

    def accepted(*args, **kwargs):
        assert saved()["notification"] == {"status": "pending", "attempts": 0}
        return Mock(status_code=200, json=Mock(return_value={"status": "sent"}))

    monkeypatch.setattr(scout.email_delivery.requests, "post", Mock(side_effect=accepted))
    scout.notify(fixed())
    assert saved()["notification"] == {"status": "sent", "attempts": 1}


def test_the_workflow_uploads_the_result_with_the_existing_files():
    workflow = Path(scout.ROOT, ".github", "workflows", "daily.yml").read_text(encoding="utf-8")
    for name in ("report.txt", "change.patch", "notification.json", "message.eml", "result.json"):
        assert f"reports/{name}" in workflow
