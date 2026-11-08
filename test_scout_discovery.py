"""Offline discovery contracts: GitHub responses, history, and bounded searches."""
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest

import scout


NOW = datetime(2026, 10, 24, 12, tzinfo=timezone.utc)


def issue(number=1, repo="acme/core", **changes):
    return {
        "id": number,
        "number": number,
        "title": "Wrong result for empty input",
        "body": "Expected zero, received one.",
        "state": "open",
        "assignees": [],
        "labels": [{"name": "bug"}],
        "created_at": "2026-10-23T12:00:00Z",
        "updated_at": "2026-10-24T12:00:00Z",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "html_url": f"https://github.com/{repo}/issues/{number}",
    } | changes


def page(items=(), **metadata):
    return {"items": list(items), "total_count": len(items), "incomplete_results": False} | metadata


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz else NOW.replace(tzinfo=None)

    monkeypatch.setattr(scout, "datetime", Clock)
    monkeypatch.setattr(scout, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(scout, "REPORTS", tmp_path / "reports")
    monkeypatch.setattr(scout, "PROFILE", scout.PROFILE | {
        "languages": ["Python"], "labels": ["bug"], "watchlist": ["acme/watch"],
    })
    monkeypatch.setattr(scout, "SCOUT", scout.SCOUT | {"search_pages": 2})
    monkeypatch.setattr(scout, "WORK_TYPES", {"code"})
    monkeypatch.setattr(scout.http, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(scout.time, "sleep", Mock())
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("SCOUT_RECIPIENT_EMAIL", "scout@example.com")
    monkeypatch.delenv("SCOUT_EMAIL_RELAY_URL", raising=False)


def test_failed_search_keeps_previous_results_and_continues_other_queries(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "watchlist", ["acme/broken", "acme/watch"])
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[page([issue(1)]),
                                                       scout.requests.Timeout("timed out"),
                                                       page([issue(2)])]))
    result = scout.discover({})
    assert [candidate["number"] for candidate in result["candidates"]] == [1, 2]
    assert result["discovery"]["partial"] is True


@pytest.mark.parametrize("count, budget, expected_count, partial", [
    (0, 2, 0, False), (99, 2, 99, False), (100, 1, 100, False),
    (101, 2, 101, False), (201, 2, 200, True),
])
def test_pagination_obeys_budget_and_reports_truncation(monkeypatch, count, budget, expected_count, partial):
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    monkeypatch.setitem(scout.SCOUT, "search_pages", budget)
    items = [issue(number) for number in range(1, count + 1)]

    def search(path, **params):
        start = (params["page"] - 1) * 100
        return page(items[start:start + 100], total_count=count)

    monkeypatch.setattr(scout, "gh", search)
    result = scout.discover({})
    assert len(result["candidates"]) == expected_count
    assert result["discovery"]["partial"] is partial


def test_incomplete_search_retains_candidates_and_reports_partial_results(monkeypatch):
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([issue()], incomplete_results=True)))
    result = scout.discover({})
    assert len(result["candidates"]) == 1
    assert result["discovery"]["partial"] is True


@pytest.mark.parametrize("response", [None, [], {}, {"items": None}, {"items": {}},
                                           {"items": [], "total_count": -1},
                                           {"items": [], "incomplete_results": "false"}])
def test_malformed_search_response_does_not_block_other_queries(monkeypatch, response):
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[response, page([issue()])]))
    result = scout.discover({})
    assert len(result["candidates"]) == 1
    assert result["discovery"]["partial"] is True


def test_failure_on_later_page_retains_earlier_page(monkeypatch):
    items = [issue(number) for number in range(1, 101)]
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[page(items, total_count=101),
                                                       RuntimeError("GitHub unavailable"), page()]))
    result = scout.discover({})
    assert len(result["candidates"]) == 100
    assert result["discovery"]["partial"] is True


@pytest.mark.parametrize("history", ["", "{", "null", "42", '"history"',
                                          '[42]', '{"url": null}', '{"url": []}'])
def test_corrupt_history_stops_discovery_before_network_calls(monkeypatch, history):
    scout.SEEN.write_text(history, encoding="utf-8")
    api = Mock(side_effect=AssertionError("history must be checked first"))
    monkeypatch.setattr(scout, "gh", api)
    with pytest.raises(ValueError, match="history"):
        scout.discover({})
    assert scout.SEEN.read_text(encoding="utf-8") == history


@pytest.mark.parametrize("record", [
    {}, {"status": "notified"}, {"status": "completed"}, {"status": "unknown"},
    {"status": "failed", "attempts": "1", "retry_after": "2026-10-23T12:00:00Z"},
    {"status": "failed", "attempts": True, "retry_after": "2026-10-23T12:00:00Z"},
    {"status": "failed", "attempts": -1, "retry_after": "2026-10-23T12:00:00Z"},
    {"status": "failed", "retry_after": "2026-10-23T12:00:00Z"},
    {"status": "failed", "attempts": 1, "retry_after": "invalid"},
    {"status": "failed", "attempts": 1, "retry_after": "2026-10-24T12:00:01Z"},
    {"status": "failed", "attempts": 2, "retry_after": "2026-10-23T12:00:00Z"},
])
def test_history_blocks_repeated_attempts_unless_retry_is_explicitly_valid(monkeypatch, record):
    item = issue()
    scout.SEEN.write_text(json.dumps({item["html_url"]: record}), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([item])))
    assert scout.discover({})["candidates"] == []


@pytest.mark.parametrize("timestamp", ["2026-10-24T12:00:00Z", "2026-10-24T12:00:00",
                                             "2026-10-24T08:00:00-04:00"])
def test_due_retry_includes_exact_boundary_and_timezone_variants(monkeypatch, timestamp):
    item = issue()
    history = {item["html_url"]: {"status": "failed", "attempts": 1, "retry_after": timestamp}}
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([item])))
    assert [candidate["number"] for candidate in scout.discover({})["candidates"]] == [1]


@pytest.mark.parametrize("changes", [
    {"state": "closed"}, {"state": "unknown"}, {"locked": True},
    {"assignees": [{"login": "contributor"}]}, {"assignee": {"login": "contributor"}},
    {"pull_request": {"url": "https://api.github.com/repos/acme/core/pulls/1"}},
    {"labels": [{"name": "GOOD FIRST ISSUE"}]},
    {"title": None}, {"body": []}, {"labels": ["bug"]}, {"labels": [{"name": None}]},
    {"labels": None}, {"assignees": "unknown"}, {"number": True}, {"number": 0},
    {"id": True}, {"id": -1},
    {"html_url": "https://github.com.evil/acme/core/issues/1"},
    {"html_url": "http://github.com/acme/core/issues/1"},
    {"html_url": "https://github.com/acme/core/issues/9"},
    {"repository_url": "https://api.github.com/repos/other/project"},
    {"repository_url": "https://api.github.com.evil/repos/acme/core"},
])
def test_unavailable_or_malformed_search_items_do_not_reach_candidates(monkeypatch, changes):
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([issue(**changes), issue(2)])))
    assert [candidate["number"] for candidate in scout.discover({})["candidates"]] == [2]


def test_nonobject_search_items_do_not_discard_valid_siblings(monkeypatch):
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([None, "issue", {}, issue()])))
    assert len(scout.discover({})["candidates"]) == 1


def test_remote_language_evidence_is_not_trusted_and_payload_is_not_mutated(monkeypatch):
    item = issue(search_languages=["c", "typescript"])
    original = json.loads(json.dumps(item))
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([item])))
    assert scout.discover({})["candidates"][0]["search_languages"] == ["python"]
    assert item == original


def test_stable_issue_id_merges_transfers_and_uses_newest_snapshot(monkeypatch):
    original = issue(updated_at="2026-10-23T12:00:00Z")
    moved = issue(7, "acme/new", id=1, title="Clarified issue", updated_at="2026-10-24T12:00:00Z")
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[page([moved]), page([original])]))
    candidates = scout.discover({})["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["repo"] == "acme/new"
    assert candidates[0]["number"] == 7
    assert candidates[0]["search_languages"] == ["python"]


def test_newer_unavailable_snapshot_removes_previously_eligible_result(monkeypatch):
    old = issue(updated_at="2026-10-23T12:00:00Z")
    updated = issue(assignees=[{"login": "contributor"}])
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[page([old]), page([updated])]))
    assert scout.discover({})["candidates"] == []


def test_older_unavailable_snapshot_cannot_remove_newer_available_result(monkeypatch):
    old = issue(state="closed", updated_at="2026-10-23T12:00:00Z")
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[page([issue()]), page([old])]))
    assert len(scout.discover({})["candidates"]) == 1


def test_history_matches_case_variants_and_empty_records(monkeypatch):
    scout.SEEN.write_text(json.dumps({"https://github.com/ACME/CORE/issues/1/": {}}), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([issue()])))
    assert scout.discover({})["candidates"] == []


def test_stable_history_identity_blocks_renamed_issue_even_with_retryable_alias(monkeypatch):
    moved = issue(7, "acme/new", id=1)
    history = {
        issue()["html_url"]: {"status": "completed", "issue_id": 1},
        moved["html_url"]: {"status": "failed", "attempts": 1, "retry_after": "2026-10-23T12:00:00Z"},
    }
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([moved])))
    assert scout.discover({})["candidates"] == []


def retries(monkeypatch, items, extra_history=None, metadata=None):
    history = {url: {"status": "failed", "attempts": 1, "retry_after": "2026-10-23T12:00:00Z"}
               for url in items}
    history.update(extra_history or {})
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")
    endpoints = {f"repos/{issue_location[0]}/issues/{issue_location[1]}": value
                 for url, value in items.items() if (issue_location := scout.issue_location(url))}

    def api(path, **params):
        if path == "search/issues":
            return page()
        if "/issues/" not in path:
            return {"private": False, "archived": False, "disabled": False} if metadata is None else metadata
        value = endpoints[path]
        if isinstance(value, Exception):
            raise value
        return value

    mock = Mock(side_effect=api)
    monkeypatch.setattr(scout, "gh", mock)
    return mock


def test_stale_and_invalid_retries_cannot_starve_later_valid_retry(monkeypatch):
    monkeypatch.setitem(scout.SCOUT, "candidate_limit", 1)
    retries(monkeypatch, {"invalid-url": None, issue()["html_url"]: issue(state="closed"),
                          issue(2)["html_url"]: issue(2)})
    assert [candidate["number"] for candidate in scout.discover({})["candidates"]] == [2]


@pytest.mark.parametrize("changes", [{"state": "closed"}, {"locked": True}, {"assignee": {"login": "other"}},
                                      {"labels": [{"name": "GOOD FIRST ISSUE"}]}, {"pull_request": {}},
                                      {"body": []}, {"labels": None}])
def test_direct_retries_apply_the_same_payload_and_availability_rules(monkeypatch, changes):
    item = issue(**changes)
    retries(monkeypatch, {item["html_url"]: item})
    assert scout.discover({})["candidates"] == []


@pytest.mark.parametrize("metadata", [{"private": True, "archived": False},
                                       {"private": False, "archived": True},
                                       {"private": False, "archived": False, "disabled": True},
                                       {}, None, []])
def test_direct_retry_requires_verified_public_active_repository(monkeypatch, metadata):
    item = issue()
    api = retries(monkeypatch, {item["html_url"]: item})
    original = api.side_effect
    api.side_effect = lambda path, **params: metadata if path == "repos/acme/core" else original(path, **params)
    assert scout.discover({})["candidates"] == []


def test_retry_failure_does_not_block_other_due_issues(monkeypatch):
    retries(monkeypatch, {issue()["html_url"]: scout.requests.Timeout("unavailable"),
                          issue(2)["html_url"]: issue(2)})
    result = scout.discover({})
    assert [candidate["number"] for candidate in result["candidates"]] == [2]
    assert result["discovery"]["partial"] is True


def test_retry_transfer_uses_returned_location_and_preserves_api_payload(monkeypatch):
    moved = issue(7, "acme/new", id=1)
    original = json.loads(json.dumps(moved))
    retries(monkeypatch, {issue()["html_url"]: moved})
    candidate = scout.discover({})["candidates"][0]
    assert (candidate["repo"], candidate["number"]) == ("acme/new", 7)
    assert moved == original


def test_retry_repository_check_is_cached_for_multiple_issues(monkeypatch):
    api = retries(monkeypatch, {issue()["html_url"]: issue(), issue(2)["html_url"]: issue(2)})
    assert len(scout.discover({})["candidates"]) == 2
    assert sum(call.args == ("repos/acme/core",) for call in api.call_args_list) == 1


def test_failure_after_successful_retry_preserves_the_success(monkeypatch):
    retries(monkeypatch, {issue()["html_url"]: issue(),
                          issue(2)["html_url"]: scout.requests.Timeout("unavailable")})
    assert [candidate["number"] for candidate in scout.discover({})["candidates"]] == [1]


def test_saved_attempt_stays_excluded_after_repository_rename(monkeypatch):
    from test_scout_recovery import result_state

    state = result_state()
    state["issue"]["id"] = 1
    scout.notify(state)
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([issue(7, "acme/new", id=1)])))
    assert scout.discover({})["candidates"] == []


def test_transferred_retry_keeps_total_attempt_limit(monkeypatch):
    from test_scout_recovery import result_state

    old = issue()
    history = {old["html_url"]: {"status": "failed", "issue_id": 1, "attempts": 1,
                                 "retry_after": "2026-10-23T12:00:00Z"}}
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([issue(7, "acme/new", id=1)])))
    chosen = scout.discover({})["candidates"][0]
    state = result_state(error="unverified fix", passed=False)
    state["issue"].update(chosen)
    scout.notify(state)
    assert scout.load_history()[chosen["html_url"]]["attempts"] == 2


def test_language_aliases_generate_canonical_deduplicated_searches(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "languages", ["Python", "python", " JS ", "csharp", "C#"])
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    api = Mock(return_value=page())
    monkeypatch.setattr(scout, "gh", api)
    scout.discover({})
    queries = [call.kwargs["q"] for call in api.call_args_list]
    assert len(queries) == 3
    assert {query.rsplit('language:', 1)[1] for query in queries} == {'"python"', '"javascript"', '"C#"'}


def test_help_wanted_date_policy_is_case_insensitive(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "labels", ["Help Wanted"])
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    api = Mock(return_value=page())
    monkeypatch.setattr(scout, "gh", api)
    scout.discover({})
    assert 'updated:>=2026-09-24' in api.call_args.kwargs["q"]
    assert api.call_args.kwargs["sort"] == "updated"


def test_large_valid_lookbacks_clamp_at_first_calendar_day(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "days", 999_999_999)
    monkeypatch.setitem(scout.SCOUT, "updated_days", 999_999_999)
    api = Mock(return_value=page())
    monkeypatch.setattr(scout, "gh", api)
    scout.discover({})
    assert all(':>=0001-01-01' in call.kwargs["q"] for call in api.call_args_list)


def test_documentation_only_searches_are_language_independent(monkeypatch):
    monkeypatch.setattr(scout, "WORK_TYPES", {"documentation"})
    monkeypatch.setitem(scout.PROFILE, "languages", [])
    api = Mock(return_value=page())
    monkeypatch.setattr(scout, "gh", api)
    scout.discover({})
    queries = [call.kwargs["q"] for call in api.call_args_list]
    assert queries and all("language:" not in query for query in queries)
    assert any('label:"documentation"' in query for query in queries)


def test_empty_search_configuration_returns_complete_empty_result(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "languages", [])
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    monkeypatch.setattr(scout, "gh", Mock(side_effect=AssertionError("no queries expected")))
    result = scout.discover({})
    assert result["candidates"] == []
    assert result["discovery"]["partial"] is False


def test_retry_cursor_bounds_requests_and_eventually_reaches_backlog_across_days(monkeypatch):
    monkeypatch.setitem(scout.SCOUT, "candidate_limit", 1)
    items = {issue(number)["html_url"]: issue(number, state="closed" if number < 10 else "open")
             for number in range(1, 11)}
    api = retries(monkeypatch, items)
    recovered = set()
    for day in range(4):
        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW + timedelta(days=day)

        monkeypatch.setattr(scout, "datetime", Clock)
        api.reset_mock()
        result = scout.discover({})
        recovered.update(candidate["number"] for candidate in result["candidates"])
        assert sum("/issues/" in call.args[0] for call in api.call_args_list) <= 3
        assert result["discovery"]["partial"] is True
    assert recovered == {10}

