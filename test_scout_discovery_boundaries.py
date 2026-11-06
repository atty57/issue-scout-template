"""Discovery boundaries found by adversarial review; all services stay offline."""
import json
from unittest.mock import Mock

import pytest

import scout
from test_scout_discovery import issue, offline, page


def test_duplicates_between_pages_cannot_claim_complete_discovery(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    first = [issue(number) for number in range(1, 101)]
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[
        page(first, total_count=101), page([issue(1)], total_count=101),
    ]))

    result = scout.discover({})

    assert len(result["candidates"]) == 100
    assert result["discovery"]["partial"] is True


@pytest.mark.parametrize("total", [0, 1])
def test_result_count_smaller_than_returned_items_is_partial(monkeypatch, total):
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([issue(1), issue(2)], total_count=total)))

    result = scout.discover({})

    assert len(result["candidates"]) == 2
    assert result["discovery"]["partial"] is True


@pytest.mark.parametrize("completed", [False, True])
def test_late_identity_bridge_merges_groups_and_preserves_history_suppression(monkeypatch, completed):
    monkeypatch.setitem(scout.PROFILE, "languages", ["Python", "JavaScript"])
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    original = issue(updated_at="2026-10-23T12:00:00Z")
    unidentified = {key: value for key, value in original.items() if key != "id"}
    moved = issue(7, "acme/new", id=1)
    if completed:
        scout.SEEN.write_text(json.dumps({original["html_url"]: {"status": "completed"}}), encoding="utf-8")
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[
        page([unidentified, moved]), page([original]),
    ]))

    result = scout.discover({})

    if completed:
        assert result["candidates"] == []
    else:
        assert len(result["candidates"]) == 1
        found = result["candidates"][0]
        assert found["html_url"] == moved["html_url"]
        assert found["search_languages"] == ["javascript", "python"]


@pytest.mark.parametrize("association", [[], {}, 1, True])
def test_malformed_author_association_cannot_break_ranking(monkeypatch, association):
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([
        issue(author_association=association), issue(2),
    ])))

    result = scout.discover({})

    assert [candidate["number"] for candidate in result["candidates"]] == [2]
    assert result["discovery"]["partial"] is True
    assert len(scout.shortlist(result)["candidates"]) == 1


def test_server_payload_cannot_supply_internal_ranking_evidence(monkeypatch):
    item = issue(features={"languages": ["python"]}, feasibility=[])
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([item])))

    result = scout.discover({})

    assert len(result["candidates"]) == 1
    assert "features" not in result["candidates"][0]
    assert "feasibility" not in result["candidates"][0]
    assert len(scout.shortlist(result)["candidates"]) == 1
    assert item["features"] == {"languages": ["python"]}
    assert item["feasibility"] == []


@pytest.mark.parametrize("timestamp", [None, {}, [], 1, True, "not-a-timestamp"])
def test_malformed_timestamps_keep_discovery_output_safe_for_ranking(monkeypatch, timestamp):
    monkeypatch.setattr(scout, "gh", Mock(return_value=page([
        issue(updated_at=timestamp), issue(2, updated_at="not-a-timestamp"),
    ])))

    result = scout.discover({})

    assert result["discovery"]["partial"] is True
    scout.shortlist(result)
