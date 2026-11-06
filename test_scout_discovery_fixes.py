"""Regressions for the four findings from the discovery branch review."""
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import scout
from test_scout_discovery import issue, offline, page, retries


@pytest.mark.parametrize("field, value", [
    ("issue_id", []), ("issue_id", {}), ("issue_id", True), ("issue_id", "1"),
    ("issue_id", -1), ("issue_id", None),
    ("language", []), ("language", {}), ("language", True), ("language", 1),
    ("language", None), ("language", "unsupported"),
])
def test_invalid_optional_history_metadata_cannot_crash_or_block_valid_retry(monkeypatch, field, value):
    item = issue()
    record = {"status": "failed", "attempts": 1, "retry_after": "2026-10-23T12:00:00Z", field: value}
    retries(monkeypatch, {item["html_url"]: item}, extra_history={item["html_url"]: record})
    original = scout.SEEN.read_bytes()

    candidates = scout.discover({})["candidates"]

    assert [candidate["number"] for candidate in candidates] == [1]
    assert candidates[0]["search_languages"] == []
    assert scout.SEEN.read_bytes() == original


@pytest.mark.parametrize("status", ["completed", "notified", "unknown"])
def test_invalid_optional_metadata_does_not_make_attempted_issue_eligible(monkeypatch, status):
    item = issue()
    history = {item["html_url"]: {"status": status, "issue_id": [], "language": {}, "attempts": 1}}
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([item])))

    assert scout.discover({})["candidates"] == []


def test_supported_history_language_alias_is_normalized_before_retry(monkeypatch):
    item = issue()
    record = {"status": "failed", "attempts": 1, "retry_after": "2026-10-23T12:00:00Z", "language": " Python "}
    retries(monkeypatch, {item["html_url"]: item}, extra_history={item["html_url"]: record})

    assert scout.discover({})["candidates"][0]["search_languages"] == ["python"]


@pytest.mark.parametrize("limit, count", [(1, 6), (2, 12), (30, 180)])
def test_separate_runs_on_same_day_reach_every_eligible_retry(monkeypatch, limit, count):
    monkeypatch.setitem(scout.SCOUT, "candidate_limit", limit)
    api = retries(monkeypatch, {issue(number)["html_url"]: issue(number) for number in range(1, count + 1)})
    original = scout.SEEN.read_bytes()
    recovered = set()

    for _ in range(count // limit):
        api.reset_mock()
        result = scout.discover({})
        recovered.update(candidate["number"] for candidate in result["candidates"])
        assert len(result["candidates"]) == limit
        assert sum("/issues/" in call.args[0] for call in api.call_args_list) <= 3 * limit

    assert recovered == set(range(1, count + 1))
    assert scout.SEEN.read_bytes() == original


def test_cursor_advances_past_failed_closed_and_malformed_retry_responses(monkeypatch):
    monkeypatch.setitem(scout.SCOUT, "candidate_limit", 1)
    retries(monkeypatch, {issue()["html_url"]: scout.requests.Timeout("unavailable"),
                          issue(2)["html_url"]: issue(2, state="closed"),
                          issue(3)["html_url"]: None,
                          issue(4)["html_url"]: issue(4)})

    first = scout.discover({})
    second = scout.discover({})

    assert first["candidates"] == []
    assert first["discovery"]["retries"]["checked"] == 3
    assert [candidate["number"] for candidate in second["candidates"]] == [4]


def test_removed_cursor_issue_does_not_restart_at_front_of_queue(monkeypatch):
    monkeypatch.setitem(scout.SCOUT, "candidate_limit", 1)
    retries(monkeypatch, {issue(number)["html_url"]: issue(number) for number in range(1, 7)})
    assert scout.discover({})["candidates"][0]["number"] == 1
    assert scout.discover({})["candidates"][0]["number"] == 2
    history = json.loads(scout.SEEN.read_text(encoding="utf-8"))
    del history[issue(2)["html_url"]]
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")

    assert scout.discover({})["candidates"][0]["number"] == 3


@pytest.mark.parametrize("cursor", ["{", "[]", "{}",
                                   '{"last_checked_url": "invalid", "last_retry_after": "2026-10-23T12:00:00Z"}',
                                   '{"last_checked_url": "https://github.com/acme/core/issues/1", "last_retry_after": []}'])
def test_corrupt_cursor_restarts_scheduling_without_discarding_candidates(monkeypatch, cursor):
    item = issue()
    retries(monkeypatch, {item["html_url"]: item})
    scout.SEEN.with_name("discovery.json").write_text(cursor, encoding="utf-8")

    result = scout.discover({})

    assert [candidate["number"] for candidate in result["candidates"]] == [1]
    assert result["discovery"]["partial"] is True
    assert result["discovery"]["retries"]["cursor_error"]


def test_cursor_write_failure_is_visible_without_losing_candidates(monkeypatch):
    item = issue()
    retries(monkeypatch, {item["html_url"]: item})
    replace = Path.replace

    def failing_replace(path, target):
        if path.name == "discovery.json.tmp":
            raise PermissionError("cursor is read only")
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", failing_replace)
    result = scout.discover({})

    assert [candidate["number"] for candidate in result["candidates"]] == [1]
    assert result["discovery"]["partial"] is True
    assert "read only" in result["discovery"]["retries"]["cursor_error"]


@pytest.mark.parametrize("newest_first", [False, True])
def test_legacy_search_transfer_keeps_attempt_count_in_either_result_order(monkeypatch, newest_first, capsys):
    from test_scout_recovery import result_state

    old = issue(updated_at="2026-10-23T12:00:00Z")
    moved = issue(7, "acme/new", id=1)
    history = {old["html_url"]: {"status": "failed", "attempts": 1, "retry_after": "2026-10-23T12:00:00Z"}}
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")
    snapshots = [moved, old] if newest_first else [old, moved]
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[page([snapshot]) for snapshot in snapshots]))

    chosen = scout.discover({})["candidates"][0]
    state = result_state(error="unverified fix", passed=False)
    state["issue"].update(chosen)
    scout.notify(state)
    capsys.readouterr()

    assert chosen["html_url"] == moved["html_url"]
    assert scout.load_history()[moved["html_url"]]["attempts"] == 2
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([moved])))
    assert scout.discover({})["candidates"] == []


def test_legacy_transfer_preserves_highest_count_across_multiple_historical_urls(monkeypatch, capsys):
    from test_scout_recovery import result_state

    monkeypatch.setitem(scout.SCOUT, "max_attempts", 4)
    monkeypatch.setitem(scout.PROFILE, "watchlist", ["acme/watch", "acme/other"])
    old = issue(updated_at="2026-10-22T12:00:00Z")
    middle = issue(5, "acme/middle", id=1, updated_at="2026-10-23T12:00:00Z")
    moved = issue(7, "acme/new", id=1)
    history = {snapshot["html_url"]: {"status": "failed", "attempts": attempts,
                                      "retry_after": "2026-10-23T12:00:00Z"}
               for snapshot, attempts in [(old, 1), (middle, 2)]}
    scout.SEEN.write_text(json.dumps(history), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[page([old]), page([middle]), page([moved])]))

    chosen = scout.discover({})["candidates"][0]
    state = result_state(error="unverified fix", passed=False)
    state["issue"].update(chosen)
    scout.notify(state)
    capsys.readouterr()

    assert scout.load_history()[moved["html_url"]]["attempts"] == 3
