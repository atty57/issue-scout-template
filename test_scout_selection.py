"""Offline scouting regressions: no GitHub, model, Docker, or email calls."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import scout


NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def candidate(number=1, repo="acme/core", **changes):
    result = {
        "title": "Pydantic validation returns wrong result for nested input",
        "body": (
            "Steps to reproduce: call validate({'value': None}) on Python 3.12. "
            "Expected a ValidationError; actual behavior returns None. "
            "A regression test can reproduce this with pytest."
        ),
        "repo": repo,
        "number": number,
        "state": "open",
        "assignees": [],
        "labels": [{"name": "bug"}],
        "comments": 0,
        "author_association": "NONE",
        "created_at": "2026-10-05T12:00:00Z",
        "updated_at": "2026-10-06T12:00:00Z",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "html_url": f"https://github.com/{repo}/issues/{number}",
    }
    result.update(changes)
    return result


def comment(body, user="contributor", association="NONE", days_ago=1):
    return {
        "body": body,
        "user": {"login": user},
        "author_association": association,
        "created_at": (NOW - timedelta(days=days_ago)).isoformat(),
        "updated_at": (NOW - timedelta(days=days_ago)).isoformat(),
    }


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch, tmp_path):
    monkeypatch.setattr(scout, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(scout, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(scout.http, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(scout.time, "sleep", Mock())
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


def test_skill_matching_uses_boundaries_and_language_aliases():
    assert "rag" not in scout.skill_matches("storage migration")
    assert "agent" not in scout.skill_matches("reagent validation")
    assert "rag" in scout.skill_matches("RAG retrieval pipeline")
    assert "agent" in scout.skill_matches("agents fail while streaming")


def test_maintainer_support_comes_from_replies_not_reporter_association():
    reported_by_member = candidate(author_association="MEMBER")
    assert not scout.issue_features(reported_by_member)["maintainer_support"]
    replies = [
        comment("Confirmed; PR welcome with a regression test.", "maint", "MEMBER"),
        comment("PR welcome!", "random-person", "NONE"),
    ]
    features = scout.issue_features(reported_by_member, replies)
    assert features["maintainer_support"]
    assert "Confirmed" in str(features["maintainer_support"])
    assert "random-person" not in str(features["maintainer_support"])


def test_features_distinguish_local_reproducer_from_required_hardware():
    local = scout.issue_features(candidate())
    gpu = scout.issue_features(candidate(body="Requires an NVIDIA GPU and CUDA to reproduce the crash."))
    assert local["reproducible"]
    assert not local["blockers"]
    assert gpu["blockers"]


def test_actionable_bug_ranks_above_same_topic_design_request():
    actionable = candidate()
    vague = candidate(title="Pydantic validation redesign", body="Should we redesign this API? Let's discuss options.")
    assert scout.score(actionable) > scout.score(vague)


def test_withdrawn_claim_releases_only_that_contributor():
    claimed = comment("I'm working on a fix.", "first", days_ago=3)
    withdrawn = comment("I can no longer work on this; please let someone else take it.", "first", days_ago=1)
    assert not scout.active_claims([claimed, withdrawn], now=NOW)
    other_claim = comment("Can I take this?", "second", days_ago=2)
    assert scout.active_claims([claimed, other_claim, withdrawn], now=NOW) == ["second"]


def test_questions_and_quoted_claims_do_not_reserve_an_issue():
    replies = [
        comment("Is anyone working on this?"),
        comment("> I'm working on a fix.\n\nThat comment was from another contributor; is it still active?"),
    ]
    assert not scout.active_claims(replies, now=NOW)


def test_blockquote_does_not_hide_contributors_own_claim():
    reply = comment("> Another person: I'm working on a fix.\n\nI'll pick this up.", "new-contributor")
    assert scout.active_claims([reply], now=NOW) == ["new-contributor"]


def test_contributor_can_withdraw_using_no_longer_working():
    replies = [comment("I'm working on a fix.", days_ago=3), comment("I'm no longer working on this.", days_ago=1)]
    assert not scout.active_claims(replies, now=NOW)


def test_old_claims_do_not_reserve_unassigned_issues_forever():
    assert not scout.active_claims([comment("I'm working on a fix.", days_ago=365)], now=NOW)


def test_paginated_comments_include_claims_beyond_first_page(monkeypatch):
    first_page = [comment("I can reproduce.", str(n)) for n in range(100)]
    final_claim = comment("I'll pick this up.", "late-contributor")
    api = Mock(side_effect=[first_page, [final_claim]])
    monkeypatch.setattr(scout, "gh", api)
    replies = scout.gh_pages("repos/acme/core/issues/1/comments")
    assert scout.active_claims(replies, now=NOW) == ["late-contributor"]
    assert [call.kwargs["page"] for call in api.call_args_list] == [1, 2]


def test_incomplete_thread_pagination_is_not_treated_as_available(monkeypatch):
    monkeypatch.setattr(scout, "gh", Mock(return_value=[comment("reproduced")] * 100))
    with pytest.raises(RuntimeError):
        scout.gh_pages("repos/acme/core/issues/1/comments", max_pages=1)


def test_policy_rejections_backfill_the_shortlist_and_cache_repository_checks(monkeypatch):
    blocked = [candidate(n, "acme/blocked") for n in range(1, 4)]
    allowed = [candidate(n, "acme/allowed") for n in range(4, 10)]
    policy = Mock(side_effect=lambda repo: {"status": "banned" if repo.endswith("blocked") else "none", "quote": ""})
    context = Mock(return_value={"status": "supported", "reason": "Python package with pytest", "packages": ["."], "test_examples": ["tests/test_core.py"]})
    monkeypatch.setattr(scout, "policy", policy)
    monkeypatch.setattr(scout, "repo_context", context)
    monkeypatch.setattr(scout, "enrich_issue", lambda issue: dict(issue, thread="", features=scout.issue_features(issue)))
    state = scout.shortlist({"candidates": blocked + allowed})
    result = scout.gate(state)["candidates"]
    assert len(result) == scout.SCOUT["shortlist_size"]
    assert all(issue["repo"] == "acme/allowed" for issue in result)
    assert policy.call_count == 2
    assert sum(call.args == ("acme/allowed",) for call in context.call_args_list) == 1


def test_gate_excludes_repository_that_cannot_run_in_python_harness(monkeypatch):
    monkeypatch.setattr(scout, "policy", Mock(return_value={"status": "allowed", "quote": ""}))
    monkeypatch.setattr(scout, "repo_context", Mock(return_value={"status": "unsupported", "reason": "Node-only package", "packages": [], "test_examples": []}))
    monkeypatch.setattr(scout, "enrich_issue", Mock(return_value=candidate()))
    assert not scout.gate({"candidates": [candidate()]})["candidates"]


def picker_option(number=1):
    issue = candidate(number, thread="maint [MEMBER]: Confirmed, PR welcome. Add a regression test for nested None.")
    issue["features"] = scout.issue_features(issue, [comment("Confirmed, PR welcome.", "maint", "MEMBER")])
    issue["policy"] = {"status": "none", "quote": ""}
    issue["feasibility"] = {"status": "supported", "reason": "Python package and pytest suite", "packages": ["."], "test_examples": ["tests/test_validation.py"]}
    return issue


def picker_verdict(index=0, confidence=0.95, why="small regression fix", count=1):
    return {"index": index, "confidence": confidence, "why": why,
            "evaluations": [{"index": n, "suitable": n == index, "reason": why,
                             "work_type": "code", "language": "python", "validation_plan": "pytest nested validation regression",
                             "unmet_requirements": [] if n == index else ["scope"],
                             "resolution_evidence": ""} for n in range(count)]}


def test_picker_sees_maintainer_discussion_and_execution_context(monkeypatch):
    model = Mock()
    model.invoke.return_value = picker_verdict(why="Confirmed, reproducible small validation fix")
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    choice = scout.pick({"candidates": [picker_option()]})
    prompt = model.invoke.call_args.args[0]
    assert "Add a regression test for nested None" in prompt
    assert "tests/test_validation.py" in prompt
    assert choice["issue"]["number"] == 1


def test_picker_can_explicitly_abstain(monkeypatch):
    model = Mock()
    model.invoke.return_value = picker_verdict(index=None, why="No sufficiently scoped issue")
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    assert scout.pick({"candidates": [picker_option()]}).get("issue") is None


@pytest.mark.parametrize("index", [-1, 2, True, "0"])
def test_picker_rejects_invalid_indices_without_selecting_last_option(monkeypatch, index):
    model = Mock()
    model.invoke.return_value = picker_verdict(index=index, why="malformed selection", count=2)
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    assert scout.pick({"candidates": [picker_option(1), picker_option(2)]}).get("issue") is None


def test_picker_abstains_when_model_unavailable(monkeypatch):
    monkeypatch.setattr(scout, "llm", Mock(side_effect=RuntimeError("model unavailable")))
    assert scout.pick({"candidates": [picker_option()]}).get("issue") is None


def test_picker_abstains_on_low_confidence(monkeypatch):
    model = Mock()
    model.invoke.return_value = picker_verdict(confidence=0.1, why="Cannot establish a focused fix")
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    assert scout.pick({"candidates": [picker_option()]}).get("issue") is None


def test_unanswered_bug_report_can_be_picked_without_maintainer_endorsement(monkeypatch):
    issue = picker_option()
    issue["thread"] = ""
    issue["features"] = scout.issue_features(issue)
    model = Mock()
    model.invoke.return_value = picker_verdict(why="Clear expected behavior and a local pytest regression")
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    assert not issue["features"]["maintainer_support"]
    assert scout.pick({"candidates": [issue]})["issue"]["number"] == issue["number"]
    prompt = model.invoke.call_args.args[0]
    assert "Maintainer endorsement is NOT required" in prompt
    assert "Missing endorsement alone is never a rejection reason" in prompt


def test_abstention_logs_each_issue_and_full_redacted_reason(monkeypatch, capsys):
    secret = "gsk_" + "a" * 30
    monkeypatch.setenv("GROQ_API_KEY", secret)
    reason = "Insufficient execution evidence. " * 12 + "Requires an unavailable GPU. " + secret
    model = Mock()
    model.invoke.return_value = picker_verdict(index=None, why=reason, count=2)
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    issues = [picker_option(1), picker_option(2)]
    scout.pick({"candidates": issues})
    log = capsys.readouterr().out
    reviews = [json.loads(line.removeprefix("pick: candidate "))
               for line in log.splitlines() if line.startswith("pick: candidate ")]
    assert [review["url"] for review in reviews] == [issue["html_url"] for issue in issues]
    assert all(not review["suitable"] for review in reviews)
    assert all(review["reason"].endswith("Requires an unavailable GPU. [redacted]") for review in reviews)
    assert "Requires an unavailable GPU. [redacted])" in log
    assert secret not in log


@pytest.mark.parametrize("evaluations", [
    [],
    [{"index": 0, "suitable": True, "reason": "small fix"}],
    [{"index": 0, "suitable": True, "reason": "small fix"}] * 2,
    [{"index": False, "suitable": True, "reason": "small fix"},
     {"index": 1, "suitable": False, "reason": "broad feature"}],
    [{"index": 0, "suitable": "true", "reason": "small fix"},
     {"index": 1, "suitable": False, "reason": "broad feature"}],
])
def test_incomplete_or_invalid_evaluations_stop_without_backfilling(monkeypatch, evaluations):
    model = Mock()
    model.invoke.return_value = picker_verdict(count=2)
    model.invoke.return_value["evaluations"] = evaluations
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [picker_option(1), picker_option(2)],
                         "remaining_candidates": [candidate(3)]})
    assert result["issue"] is None
    assert not result["search_more"]


def test_abstention_cannot_contradict_a_suitable_candidate(monkeypatch):
    model = Mock()
    model.invoke.return_value = picker_verdict(index=None)
    model.invoke.return_value["evaluations"][0]["suitable"] = True
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [picker_option()], "remaining_candidates": [candidate(2)]})
    assert result["issue"] is None
    assert not result["search_more"]


def fake_issue_api(fresh, comments=(), timeline=(), pull=None):
    def get(path, **params):
        if path.endswith("/comments"):
            return list(comments)
        if path.endswith("/timeline"):
            return list(timeline)
        if "/pulls/" in path:
            return pull or {"state": "open", "merged": False}
        if path.endswith(f"/issues/{fresh['number']}"):
            return dict(fresh)
        raise AssertionError(f"Unexpected GitHub request: {path}")
    return get


@pytest.mark.parametrize("changes", [{"state": "closed"}, {"assignees": [{"login": "someone"}]}])
def test_fresh_issue_state_overrides_search_snapshot(monkeypatch, changes):
    snapshot = candidate()
    fresh = candidate(**changes)
    monkeypatch.setattr(scout, "gh", fake_issue_api(fresh))
    with pytest.raises((ValueError, RuntimeError)):
        scout.enrich_issue(snapshot)


def test_new_claim_is_detected_even_when_search_snapshot_has_no_comments(monkeypatch):
    snapshot = candidate(comments=0)
    fresh = candidate(comments=1)
    recent = {"body": "I'm working on a fix.", "user": {"login": "new-contributor"}, "created_at": datetime.now(timezone.utc).isoformat()}
    monkeypatch.setattr(scout, "gh", fake_issue_api(fresh, comments=[recent]))
    with pytest.raises((ValueError, RuntimeError)):
        scout.enrich_issue(snapshot)


def test_open_competing_pull_request_blocks_issue(monkeypatch):
    pull = {"state": "open", "merged": False}
    fresh = candidate()
    event = {"event": "cross-referenced", "source": {"issue": {
        "state": pull["state"], "url": "https://api.github.com/repos/acme/core/issues/99",
        "html_url": "https://github.com/acme/core/pull/99",
        "pull_request": {"url": "https://api.github.com/repos/acme/core/pulls/99"},
    }}}
    monkeypatch.setattr(scout, "gh", fake_issue_api(fresh, timeline=[event], pull=pull))
    with pytest.raises(ValueError, match="referenced PR"):
        scout.enrich_issue(fresh)


def test_cross_reference_from_a_plain_issue_does_not_block(monkeypatch):
    fresh = candidate()
    event = {"event": "cross-referenced", "source": {"issue": {
        "state": "open", "url": "https://api.github.com/repos/acme/core/issues/42",
        "html_url": "https://github.com/acme/core/issues/42"}}}  # no pull_request key
    monkeypatch.setattr(scout, "gh", fake_issue_api(fresh, timeline=[event]))
    assert scout.enrich_issue(fresh)["html_url"] == fresh["html_url"]


@pytest.mark.parametrize("merged", [False, True])
def test_closed_referenced_pr_keeps_verified_merge_state_for_picker(monkeypatch, merged):
    issue = candidate()
    pr_url = "https://github.com/acme/core/pull/99"
    timeline = [{"event": "cross-referenced", "source": {"issue": {
        "pull_request": {"url": "https://api.github.com/repos/acme/core/pulls/99"}}}}]
    pull = {"state": "closed", "merged": merged, "title": "Validation patch", "html_url": pr_url,
            "merged_at": "2026-10-06T10:00:00Z" if merged else None}
    monkeypatch.setattr(scout, "gh", fake_issue_api(issue, timeline=timeline, pull=pull))
    enriched = scout.enrich_issue(issue)
    model = Mock(invoke=Mock(return_value=picker_verdict()))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    scout.pick({"candidates": [dict(enriched, feasibility=picker_option()["feasibility"])]})
    listing = json.loads(model.invoke.call_args.args[0].rsplit("\n\n", 1)[-1])
    assert listing[0]["referenced_prs"][0]["merged"] is merged
    assert listing[0]["referenced_prs"][0]["url"] == pr_url


def test_revalidation_discards_verdict_when_referenced_pr_merge_state_changes(monkeypatch):
    selected, alternate = picker_option(1), picker_option(2)
    selected["referenced_prs"] = [{"url": "https://github.com/acme/core/pull/99", "merged": False}]
    refreshed = dict(selected, referenced_prs=[{"url": "https://github.com/acme/core/pull/99", "merged": True}])
    monkeypatch.setattr(scout, "enrich_issue", Mock(return_value=refreshed))
    result = scout.revalidate({"issue": selected, "candidates": [selected, alternate]})
    assert result["issue"] is None
    assert [issue["number"] for issue in result["candidates"]] == [2]


def test_revalidation_removes_stale_choice_and_keeps_alternatives(monkeypatch):
    selected, alternate = picker_option(1), picker_option(2)
    monkeypatch.setattr(scout, "enrich_issue", Mock(side_effect=ValueError("issue was assigned")))
    result = scout.revalidate({"issue": selected, "candidates": [selected, alternate], "why": "testable fix"})
    assert result["issue"] is None
    assert [issue["number"] for issue in result["candidates"]] == [2]


def test_revalidation_reranks_when_discussion_materially_changes(monkeypatch):
    selected, alternate = picker_option(1), picker_option(2)
    refreshed = dict(selected, thread="maint [MEMBER]: Also handle nested lists.")
    monkeypatch.setattr(scout, "enrich_issue", Mock(return_value=refreshed))
    result = scout.revalidate({"issue": selected, "candidates": [selected, alternate], "why": "small fix"})
    assert result["issue"] is None
    assert [issue["number"] for issue in result["candidates"]] == [2]


def test_discovery_searches_updated_watchlist_and_deduplicates_results(monkeypatch):
    item = candidate()
    api = Mock(return_value={"items": [item]})
    monkeypatch.setattr(scout, "gh", api)
    result = scout.discover({})
    queries = [call.kwargs["q"] for call in api.call_args_list]
    assert any("created:>=" in q and "language:" in q for q in queries)
    assert all(any(f"repo:{repo}" in q and "updated:>=" in q for q in queries) for repo in scout.PROFILE["watchlist"])
    assert len(result["candidates"]) == 1


def test_legacy_history_still_excludes_notified_issues(monkeypatch):
    item = candidate()
    scout.SEEN.write_text(json.dumps([item["html_url"]]))
    monkeypatch.setattr(scout, "gh", Mock(return_value={"items": [item]}))
    assert not scout.discover({})["candidates"]
    assert item["html_url"] in scout.load_history()


@pytest.mark.parametrize("status, retry_days, attempts, expected", [
    ("failed", -1, 1, True),
    ("failed", 1, 1, False),
    ("failed", -1, 100, False),
    ("notified", -1, 1, False),
])
def test_history_retries_only_due_failures_with_attempts_remaining(monkeypatch, status, retry_days, attempts, expected):
    item = candidate()
    record = {"status": status, "attempts": attempts, "retry_after": (datetime.now(timezone.utc) + timedelta(days=retry_days)).isoformat()}
    scout.SEEN.write_text(json.dumps({item["html_url"]: record}))
    monkeypatch.setattr(scout, "gh", Mock(return_value={"items": [item]}))
    assert bool(scout.discover({})["candidates"]) is expected


@pytest.mark.parametrize("requires_python, expected", [(">=3.10,<3.13", "supported"), (">=3.13", "unsupported")])
def test_repository_context_checks_python_version_without_running_code(monkeypatch, requires_python, expected):
    metadata = {"default_branch": "main", "archived": False, "disabled": False, "stargazers_count": 200}
    tree = {"truncated": False, "tree": [
        {"path": "python/pyproject.toml", "type": "blob"},
        {"path": "python/tests/test_validation.py", "type": "blob"},
    ]}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[metadata, tree]))
    monkeypatch.setattr(scout, "gh_file", Mock(return_value=f'[project]\nname = "core"\nrequires-python = "{requires_python}"\n'))
    context = scout.repo_context("acme/core")
    assert context["status"] == expected
    assert context["packages"][0]["path"] == "python"
    assert context["test_examples"] == ["python/tests/test_validation.py"]


def test_repository_context_supports_node_repository(monkeypatch):
    metadata = {"default_branch": "main", "archived": False, "disabled": False, "stargazers_count": 200}
    tree = {"truncated": False, "tree": [{"path": "package.json", "type": "blob"}, {"path": "tests/core.test.js", "type": "blob"}]}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[metadata, tree]))
    monkeypatch.setattr(scout, "gh_file", Mock(return_value='{"scripts":{"test":"node --test"}}'))
    context = scout.repo_context("acme/core")
    assert context["status"] == "supported"
    assert context["languages"]["javascript"]["status"] == "supported"


def test_graph_reranks_stale_pick_and_solves_only_available_alternative(monkeypatch):
    selected, alternate = picker_option(1), picker_option(2)
    events = []

    def discover(state):
        return {"candidates": [selected, alternate]}

    def shortlist(state):
        return {"candidates": state["candidates"]}

    def gate(state):
        return {"candidates": state["candidates"]}

    def enrich(issue):
        events.append(("revalidate", issue["number"]))
        if issue["number"] == 1:
            raise ValueError("new claim since ranking")
        return issue

    def solve(state):
        events.append(("solve", state["issue"]["number"]))
        return {"result": {"error": "mocked solver"}}

    def notify(state):
        events.append(("notify", state["issue"]["number"]))
        return {}

    for name, function in [("discover", discover), ("shortlist", shortlist), ("gate", gate), ("solve", solve), ("notify", notify), ("enrich_issue", enrich)]:
        monkeypatch.setattr(scout, name, function)
    model = Mock()
    model.invoke.side_effect = [picker_verdict(count=2), picker_verdict()]
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    outcome = scout.build().invoke({})
    assert model.invoke.call_count == 2
    assert events == [("revalidate", 1), ("revalidate", 2), ("solve", 2), ("notify", 2)]
    assert outcome["issue"]["number"] == 2


def mock_selection_graph(monkeypatch, issues, model, **settings):
    monkeypatch.setattr(scout, "SCOUT", scout.SCOUT | settings)
    monkeypatch.setattr(scout, "discover", Mock(__name__="discover", return_value={"candidates": issues}))
    policy = Mock(return_value={"status": "none", "quote": ""})
    context = Mock(return_value=picker_option()["feasibility"])
    enrich = Mock(side_effect=lambda issue: dict(issue))
    solve = Mock(__name__="solve", return_value={"result": {"error": "mocked solver"}})
    notify = Mock(__name__="notify", return_value={})
    for name, mock in [("policy", policy), ("repo_context", context), ("enrich_issue", enrich),
                       ("solve", solve), ("notify", notify)]:
        monkeypatch.setattr(scout, name, mock)
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    return policy, context, enrich, solve, notify


def test_graph_backfills_after_abstention_and_solves_one_later_candidate(monkeypatch):
    issues = [picker_option(n) for n in range(1, 8)]
    model = Mock()
    model.invoke.side_effect = [picker_verdict(index=None, why="First batch has no bounded code fix", count=5),
                                picker_verdict(index=1, count=2)]
    policy, context, _, solve, notify = mock_selection_graph(monkeypatch, issues, model, shortlist_size=5)
    outcome = scout.build().invoke({})
    assert model.invoke.call_count == 2
    assert outcome["issue"]["number"] == 7
    assert solve.call_count == notify.call_count == 1
    assert solve.call_args.args[0]["issue"]["number"] == 7
    assert policy.call_count == context.call_count == 1


def test_graph_exhausts_bounded_pool_before_final_abstention(monkeypatch):
    issues = [picker_option(n) for n in range(1, 36)]
    model = Mock()
    model.invoke.return_value = picker_verdict(index=None, why="No credible local regression test")
    policy, context, enrich, solve, notify = mock_selection_graph(
        monkeypatch, issues, model, candidate_limit=30, shortlist_size=1)
    outcome = scout.build().invoke({})
    assert outcome["issue"] is None
    assert not outcome["remaining_candidates"]
    assert model.invoke.call_count == enrich.call_count == 30
    assert len({call.args[0]["html_url"] for call in enrich.call_args_list}) == 30
    assert policy.call_count == context.call_count == 1
    solve.assert_not_called()
    notify.assert_not_called()


@pytest.mark.parametrize("malformed", [False, True])
def test_graph_does_not_backfill_after_unreliable_model_verdict(monkeypatch, malformed):
    model = Mock()
    if malformed:
        model.invoke.return_value = picker_verdict(index=99, count=5)
    else:
        model.invoke.side_effect = RuntimeError("model unavailable")
    _, _, enrich, solve, notify = mock_selection_graph(
        monkeypatch, [picker_option(n) for n in range(1, 8)], model, shortlist_size=5)
    outcome = scout.build().invoke({})
    assert outcome["issue"] is None
    assert len(outcome["remaining_candidates"]) == 2
    assert model.invoke.call_count == 1
    assert enrich.call_count == 5
    solve.assert_not_called()
    notify.assert_not_called()


@pytest.mark.parametrize("successful", [True, False])
def test_notify_persists_outcome_and_migrates_legacy_history(monkeypatch, capsys, successful):
    issue = picker_option()
    legacy_url = "https://github.com/acme/core/issues/999"
    scout.SEEN.write_text(json.dumps([legacy_url]))
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setattr(scout.smtplib, "SMTP_SSL", Mock(side_effect=AssertionError("unexpected email send")))
    brief = {key: "review detail" for key in scout.BRIEF_SCHEMA["required"]}
    brief.update(maintainer_questions=["Confirmed expected behavior"], checklist=["Review patch"])
    result = {"brief": brief, "patch": "+return validated_value\n", "passed": True, "reproduces": True, "cost": 0, "log": ""} if successful else {"error": "clone failed"}
    scout.notify({"issue": issue, "result": result, "why": "maintainer confirmed the reproducer"})
    capsys.readouterr()
    history = json.loads(scout.SEEN.read_text())
    assert history[legacy_url]["status"] == "notified"
    record = history[issue["html_url"]]
    assert record["status"] == ("notified" if successful else "failed")
    assert record["attempts"] == 1
    assert record["tests_passed"] is successful
    assert record["reproduces"] is successful
    assert record["score"] == scout.score(issue)
    assert record["why"] == "maintainer confirmed the reproducer"
    if successful:
        assert "retry_after" not in record
    else:
        attempted = scout.parsed_time(record["attempted_at"])
        retry = scout.parsed_time(record["retry_after"])
        assert retry - attempted == timedelta(days=scout.SCOUT["retry_days"])
        assert record["reason"] == "clone failed"


def test_manual_connected_event_blocks_unverifiable_linked_work(monkeypatch):
    fresh = candidate()
    connected = {"event": "connected", "id": 50, "created_at": "2026-10-06T10:00:00Z"}
    monkeypatch.setattr(scout, "gh", fake_issue_api(fresh, timeline=[connected]))
    with pytest.raises((ValueError, RuntimeError)):
        scout.enrich_issue(fresh)


def test_manual_connection_followed_by_disconnection_releases_issue(monkeypatch):
    fresh = candidate()
    timeline = [
        {"event": "connected", "id": 50, "created_at": "2026-10-06T10:00:00Z"},
        {"event": "disconnected", "id": 51, "created_at": "2026-10-06T11:00:00Z"},
    ]
    monkeypatch.setattr(scout, "gh", fake_issue_api(fresh, timeline=timeline))
    assert scout.enrich_issue(fresh)["html_url"] == fresh["html_url"]


def test_invitation_to_review_branch_does_not_withdraw_own_claim():
    reply = comment("I'm working on this; feel free to take a look at my branch.", "claimant")
    assert scout.active_claims([reply], now=NOW) == ["claimant"]


@pytest.mark.parametrize("malformed", [
    'project = "not a table"\n',
    '[project]\nname = "broken"\nrequires-python = [">=3.12"]\n',
])
def test_malformed_package_metadata_does_not_abort_other_candidates(monkeypatch, malformed):
    metadata = {"default_branch": "main", "archived": False, "disabled": False, "stargazers_count": 200}
    tree = {"truncated": False, "tree": [
        {"path": "pyproject.toml", "type": "blob"},
        {"path": "tests/test_validation.py", "type": "blob"},
    ]}

    def gh(path, **params):
        return tree if "/git/trees/" in path else metadata

    def gh_file(repo, path):
        return malformed if repo == "acme/broken" else '[project]\nname = "valid"\nrequires-python = ">=3.10"\n'

    monkeypatch.setattr(scout, "gh", gh)
    monkeypatch.setattr(scout, "gh_file", gh_file)
    monkeypatch.setattr(scout, "policy", Mock(return_value={"status": "none", "quote": ""}))
    monkeypatch.setattr(scout, "enrich_issue", lambda issue: dict(issue, thread="", features=scout.issue_features(issue)))
    assert scout.repo_context("acme/broken")["status"] == "unknown"
    result = scout.gate({"candidates": [candidate(1, "acme/broken"), candidate(2, "acme/valid")]})
    assert any(issue["repo"] == "acme/valid" for issue in result["candidates"])


def test_unknown_execution_context_does_not_trigger_fallback_when_model_fails(monkeypatch):
    option = picker_option()
    option["feasibility"] = {"status": "unknown", "reason": "package installation and pytest support unconfirmed", "packages": [], "test_examples": []}
    monkeypatch.setattr(scout, "llm", Mock(side_effect=RuntimeError("ranking model unavailable")))
    assert scout.pick({"candidates": [option]}).get("issue") is None


def test_revalidation_accepts_unchanged_discussion_and_propagates_fresh_timestamp(monkeypatch):
    selected = picker_option()
    refreshed = dict(selected, updated_at="2026-10-06T13:00:00Z")
    monkeypatch.setattr(scout, "enrich_issue", Mock(return_value=refreshed))
    result = scout.revalidate({"issue": selected, "candidates": [selected], "why": "small fix"})
    assert result["issue"]["updated_at"] == "2026-10-06T13:00:00Z"
    assert result["issue"]["thread"] == selected["thread"]


def test_recent_edit_renews_old_contributor_claim():
    reply = comment("I'm working on a fix.", "claimant", days_ago=365)
    reply["updated_at"] = (NOW - timedelta(days=1)).isoformat()
    assert scout.active_claims([reply], now=NOW) == ["claimant"]


def test_python_patch_version_requirement_is_not_treated_as_incompatible(monkeypatch):
    metadata = {"default_branch": "main", "archived": False, "disabled": False, "stargazers_count": 200}
    tree = {"truncated": False, "tree": [
        {"path": "pyproject.toml", "type": "blob"},
        {"path": "tests/test_validation.py", "type": "blob"},
    ]}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[metadata, tree]))
    monkeypatch.setattr(scout, "gh_file", Mock(return_value='[project]\nname = "core"\nrequires-python = ">=3.12.1"\n'))
    context = scout.repo_context("acme/core")
    assert context["status"] != "unsupported"
    assert context["packages"][0]["requires_python"] == ">=3.12.1"


def test_due_failure_outside_discovery_window_is_recovered_by_issue_url(monkeypatch):
    old = candidate(created_at="2025-01-01T12:00:00Z", updated_at="2025-01-01T12:00:00Z")
    retry = {"status": "failed", "attempts": 1, "retry_after": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()}
    scout.SEEN.write_text(json.dumps({old["html_url"]: retry}))

    def gh(path, **params):
        if path == "search/issues":
            return {"items": []}
        if path == "repos/acme/core":
            return {"private": False, "archived": False, "disabled": False}
        assert path == "repos/acme/core/issues/1"
        return dict(old)

    api = Mock(side_effect=gh)
    monkeypatch.setattr(scout, "gh", api)
    result = scout.discover({})
    assert [issue["html_url"] for issue in result["candidates"]] == [old["html_url"]]
    assert sum(call.args == ("repos/acme/core/issues/1",) for call in api.call_args_list) == 1


def test_policy_transport_failure_is_cached_without_aborting_other_repository(monkeypatch):
    issues = [candidate(1, "acme/unavailable"), candidate(2, "acme/unavailable"), candidate(3, "acme/valid")]

    def policy(repo):
        if repo == "acme/unavailable":
            raise scout.requests.RequestException("policy transport failed")
        return {"status": "none", "quote": ""}

    policy_api = Mock(side_effect=policy)
    monkeypatch.setattr(scout, "policy", policy_api)
    monkeypatch.setattr(scout, "repo_context", Mock(return_value={"status": "supported", "reason": "Python pytest package", "packages": ["."], "test_examples": ["tests/test_validation.py"]}))
    enrich = Mock(side_effect=lambda issue: dict(issue, thread="", features=scout.issue_features(issue)))
    monkeypatch.setattr(scout, "enrich_issue", enrich)
    result = scout.gate({"candidates": issues})
    assert [issue["repo"] for issue in result["candidates"]] == ["acme/valid"]
    assert sum(call.args == ("acme/unavailable",) for call in policy_api.call_args_list) == 1
    assert enrich.call_count == 1


def test_ranked_pool_keeps_other_repositories_when_one_dominates_scores(monkeypatch):
    monkeypatch.setattr(scout, "SCOUT", scout.SCOUT | {"candidate_limit": 5})
    crowded = [candidate(n, "acme/verbose", title="Pydantic LLM agent streaming validation bug")
               for n in range(1, 31)]
    alternatives = [candidate(31, "acme/other"), candidate(32, "acme/third")]
    result = scout.shortlist({"candidates": crowded + alternatives})
    pool = result["remaining_candidates"]
    assert len(pool) == 5
    assert [issue["repo"] for issue in pool[:3]] == ["acme/verbose", "acme/other", "acme/third"]
    assert [issue["number"] for issue in pool if issue["repo"] == "acme/verbose"] == [1, 2, 3]


def test_unrelated_reports_do_not_consume_ranked_pool_budget(monkeypatch):
    monkeypatch.setattr(scout, "SCOUT", scout.SCOUT | {"candidate_limit": 1})
    unrelated = candidate(1, "acme/unrelated", title="Repository maintenance",
                          body="Steps to reproduce: missing file.", author_association="MEMBER")
    related = candidate(2, title="Pydantic bug", body="")
    result = scout.shortlist({"candidates": [unrelated, related]})
    assert [issue["number"] for issue in result["candidates"]] == [2]


def test_picker_keeps_reproducer_after_long_template(monkeypatch):
    issue = picker_option()
    issue["body"] = "Environment checklist. " * 400 + "\nassert load_prompt(allow_missing=True) is None"
    model = Mock()
    model.invoke.return_value = picker_verdict()
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    assert scout.pick({"candidates": [issue]})["issue"]
    listing = json.loads(model.invoke.call_args.args[0].rsplit("\n\n", 1)[-1])
    assert "assert load_prompt(allow_missing=True) is None" in listing[0]["body"]
    assert len(listing[0]["body"]) <= 6000
    assert "truncated" in listing[0]["body"]


def test_discussion_preserves_early_evidence_and_later_failed_fix(monkeypatch):
    early = comment("Confirmed. Expected behavior is a ValidationError; assert validate(None) raises.",
                    "maint", "MEMBER", days_ago=10)
    fixed = comment("This is fixed by the merged validation patch.", "maint", "MEMBER", days_ago=9)
    chatter = [comment("Thanks for looking into this. " * 80, f"user-{n}", days_ago=8) for n in range(15)]
    latest = comment("Still broken after that fix. The same input returns None.", "reporter", days_ago=0)
    replies = [early, fixed] + chatter + [latest]
    issue = candidate(comments=len(replies))
    monkeypatch.setattr(scout, "gh", fake_issue_api(issue, comments=replies))
    enriched = scout.enrich_issue(issue)
    model = Mock()
    model.invoke.return_value = picker_verdict()
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    assert scout.pick({"candidates": [dict(enriched, feasibility=picker_option()["feasibility"])]})["issue"]
    listing = json.loads(model.invoke.call_args.args[0].rsplit("\n\n", 1)[-1])
    discussion = listing[0]["discussion"]
    assert "Expected behavior is a ValidationError" in discussion
    assert "fixed by the merged validation patch" in discussion
    assert "Still broken after that fix" in discussion
    assert "comments omitted" in discussion
    assert len(discussion) <= 6000


@pytest.mark.parametrize("unmet, suitable", [(["maintainer_approval"], False), ([], False),
                                          (["scope"], True), (["scope", "scope"], False)])
def test_picker_rejections_must_name_actual_unmet_requirements(monkeypatch, unmet, suitable):
    verdict = picker_verdict(index=None)
    verdict["evaluations"][0].update(unmet_requirements=unmet, suitable=suitable)
    model = Mock(invoke=Mock(return_value=verdict))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [picker_option()], "remaining_candidates": [candidate(2)]})
    assert result["issue"] is None
    assert not result["search_more"]


@pytest.mark.parametrize("quote", ["", "This was fixed in a later release.", "Expected a ValidationError"])
def test_already_fixed_rejection_cannot_use_memory_or_unrelated_quote(monkeypatch, quote):
    verdict = picker_verdict(index=None, why="Appears to have been fixed")
    verdict["evaluations"][0].update(unmet_requirements=["unresolved_bug"], resolution_evidence=quote)
    model = Mock(invoke=Mock(return_value=verdict))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [picker_option()], "remaining_candidates": [candidate(2)]})
    assert result["issue"] is None
    assert not result["search_more"]


def test_grounded_resolution_abstention_continues_with_later_candidates(monkeypatch, capsys):
    issue = picker_option()
    quote = "Confirmed fixed in the latest release; the reproducer now passes."
    issue["thread"] = "maint (MEMBER): " + quote
    verdict = picker_verdict(index=None, why="Maintainer confirmed this exact reproducer is fixed")
    verdict["evaluations"][0].update(unmet_requirements=["unresolved_bug"], resolution_evidence=quote)
    model = Mock(invoke=Mock(return_value=verdict))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [issue], "remaining_candidates": [candidate(2)]})
    assert result["issue"] is None
    assert result["search_more"]
    log = capsys.readouterr().out
    assert quote in log
    assert '"unmet_requirements": ["unresolved_bug"]' in log


@pytest.mark.parametrize("wrappers", [('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"), ("`", "`")])
@pytest.mark.parametrize("source", ["body", "discussion_excerpt"])
def test_wrapped_resolution_quote_does_not_block_selected_documentation(monkeypatch, wrappers, source):
    resolved, docs = picker_option(1), picker_option(2)
    quote = ("It looks like #17541 fixed the alias path, and that fix shipped in 3.5.0. "
             "If nobody still sees this on 3.5.0 or later, this issue can probably be closed.")
    resolved[source] = quote
    docs.update(title="Add llms.txt to the documentation", body="Add a list of documentation pages in llms.txt.")
    verdict = picker_verdict(index=1, confidence=0.86, count=2)
    verdict["evaluations"][0].update(unmet_requirements=["unresolved_bug"],
                                     resolution_evidence=wrappers[0] + quote + wrappers[1])
    verdict["evaluations"][1].update(work_type="documentation", language="none",
                                     validation_plan="Check llms.txt links and run the docs build")
    model = Mock(invoke=Mock(return_value=verdict))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))

    result = scout.pick({"candidates": [resolved, docs], "remaining_candidates": [candidate(3)]})

    assert result["issue"]["number"] == docs["number"]
    assert result["issue"]["work_type"] == "documentation"
    assert model.invoke.call_count == 1


@pytest.mark.parametrize("quote, diagnostic", [
    ('"This was fixed in a later release."', "absent from supplied body or discussion"),
    ('"This is not fixed in the current release."', "says the issue remains unresolved"),
    ('"Expected a ValidationError"', "does not confirm a fix"),
])
def test_wrapped_unsupported_resolution_still_stops_with_specific_diagnostic(monkeypatch, capsys, quote, diagnostic):
    issue = picker_option()
    # An invented quote is absent; an unresolved quote is present but cannot prove a fix.
    if "not fixed" in quote:
        issue["thread"] = quote[1:-1]
    verdict = picker_verdict(index=None)
    verdict["evaluations"][0].update(unmet_requirements=["unresolved_bug"], resolution_evidence=quote)
    model = Mock(invoke=Mock(return_value=verdict))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))

    result = scout.pick({"candidates": [issue], "remaining_candidates": [candidate(2)]})

    assert result["issue"] is None
    assert not result["search_more"]
    assert model.invoke.call_count == 2
    assert diagnostic in capsys.readouterr().out


@pytest.mark.parametrize("requirement, quote", [("maintainer_approval", ""),
                                               ("unresolved_bug", "This was fixed in a later release.")])
def test_picker_corrects_unsupported_rejection_once_and_can_select(monkeypatch, requirement, quote):
    rejected = picker_verdict(index=None, why="Unsupported rejection")
    rejected["evaluations"][0].update(unmet_requirements=[requirement], resolution_evidence=quote)
    model = Mock(invoke=Mock(side_effect=[rejected, picker_verdict(why="Clear local validation regression")]))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    issue = picker_option()
    issue["features"] = scout.issue_features(issue)
    issue["thread"] = ""
    result = scout.pick({"candidates": [issue]})
    assert result["issue"]["number"] == issue["number"]
    assert model.invoke.call_count == 2
    assert "Missing maintainer endorsement is not a rejection reason" in model.invoke.call_args.args[0]
    assert "supplied confirming quote" in model.invoke.call_args.args[0]


def test_repeated_unsupported_rejection_stops_after_one_correction(monkeypatch, capsys):
    secret = "gsk_" + "b" * 30
    monkeypatch.setenv("GROQ_API_KEY", secret)
    rejected = picker_verdict(index=None, why="Appears fixed " + secret)
    rejected["evaluations"][0].update(unmet_requirements=["unresolved_bug"], resolution_evidence="")
    model = Mock(invoke=Mock(return_value=rejected))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [picker_option()], "remaining_candidates": [candidate(2)]})
    assert result["issue"] is None
    assert not result["search_more"]
    assert model.invoke.call_count == 2
    assert secret not in capsys.readouterr().out
    assert secret not in model.invoke.call_args.args[0]


def test_corrected_grounded_abstention_advances_to_later_batch(monkeypatch):
    rejected = picker_verdict(index=None)
    rejected["evaluations"][0].update(unmet_requirements=["unresolved_bug"], resolution_evidence="")
    corrected = picker_verdict(index=None, why="Requires unavailable CUDA hardware")
    corrected["evaluations"][0]["unmet_requirements"] = ["local_test"]
    model = Mock(invoke=Mock(side_effect=[rejected, corrected]))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    issue = picker_option()
    issue["body"] += " Reproducing this requires CUDA hardware."
    result = scout.pick({"candidates": [issue], "remaining_candidates": [candidate(2)]})
    assert result["issue"] is None
    assert result["search_more"]
    assert model.invoke.call_count == 2


@pytest.mark.parametrize("quote", ["This is not fixed in the current release.",
                                 "The fix was merged, but the same input still fails.",
                                 "Reopened because that fix does not handle nested values."])
def test_unresolved_status_quote_cannot_justify_already_fixed_rejection(monkeypatch, quote):
    issue = picker_option()
    issue["thread"] = "maint (MEMBER): " + quote
    verdict = picker_verdict(index=None)
    verdict["evaluations"][0].update(unmet_requirements=["unresolved_bug"], resolution_evidence=quote)
    model = Mock(invoke=Mock(return_value=verdict))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [issue], "remaining_candidates": [candidate(2)]})
    assert result["issue"] is None
    assert not result["search_more"]
    assert model.invoke.call_count == 2
