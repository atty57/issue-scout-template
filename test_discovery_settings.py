"""Configuration boundaries for discovery, without network calls."""
import pytest

from settings import load_settings, load_workloads


@pytest.mark.parametrize("value", [0, -1, True, False, 1.5, "3", None, 1_000_000_000])
def test_discovery_rejects_unusable_creation_lookback(value):
    with pytest.raises(ValueError, match="days"):
        load_settings({"days": value})


@pytest.mark.parametrize("value", [1, 3, 999_999_999])
def test_discovery_accepts_representable_creation_lookback(value):
    assert load_settings({"days": value})["search_pages"] == 2


@pytest.mark.parametrize("overrides, message", [
    ({"updated_days": 1_000_000_000}, "updated_days"),
    ({"search_pages": 11}, "search_pages"),
    ({"general_issues": "false"}, "general_issues"),
    ({"general_issues": 1}, "general_issues"),
])
def test_discovery_rejects_search_settings_that_cannot_be_used(overrides, message):
    with pytest.raises(ValueError, match=message):
        load_settings({"scouting": overrides})


def test_discovery_accepts_search_cap_and_boolean_toggle():
    settings = load_settings({"scouting": {"search_pages": 10, "updated_days": 999_999_999,
                                            "general_issues": False}})
    assert settings["search_pages"] == 10
    assert settings["general_issues"] is False


@pytest.mark.parametrize("name", ["labels", "documentation_labels", "languages", "watchlist"])
@pytest.mark.parametrize("value", ["bug", None, True, 1, {}, [""], ["   "], [False], [None], ["bug\nrepo:other/project"]])
def test_discovery_requires_lists_of_nonempty_search_values(name, value):
    with pytest.raises(ValueError, match=name):
        load_settings({name: value})


@pytest.mark.parametrize("value", ["bug", None, {}, [""], [False]])
def test_discovery_requires_excluded_labels_to_be_a_string_list(value):
    with pytest.raises(ValueError, match="exclude_labels"):
        load_settings({"scouting": {"exclude_labels": value}})


def test_discovery_allows_empty_optional_search_lists_and_punctuation():
    values = {"labels": [], "documentation_labels": ["bug: parsing / I/O", "🐛 bug"],
              "languages": [], "watchlist": [], "scouting": {"exclude_labels": []}}
    assert load_settings(values)["exclude_labels"] == []


@pytest.mark.parametrize("name", ["labels", "documentation_labels", "exclude_labels"])
@pytest.mark.parametrize("label", ['bug" repo:other/project', r"bug\tag", "bug\tlabel"])
def test_discovery_rejects_unsupported_query_label_syntax(name, label):
    profile = {"scouting": {name: [label]}} if name == "exclude_labels" else {name: [label]}
    with pytest.raises(ValueError, match=name):
        load_settings(profile)


@pytest.mark.parametrize("repo", ["acme", "acme/repo/issue", "https://github.com/acme/repo", "/repo",
                                  "acme/", "../repo", "acme/..", "acme/repo is:closed", "acme/repo?x=y"])
def test_discovery_rejects_malformed_watchlist_repositories(repo):
    with pytest.raises(ValueError, match="watchlist"):
        load_settings({"watchlist": [repo]})


def test_discovery_accepts_case_dots_dashes_and_underscores_in_repository_names():
    assert load_settings({"watchlist": ["Acme-Org/project.v2_name"]})["search_pages"] == 2


@pytest.mark.parametrize("profile", [None, [], "profile", True, {"scouting": None}, {"scouting": []}])
def test_discovery_settings_reject_malformed_profile_objects(profile):
    with pytest.raises(ValueError, match="profile|scouting"):
        load_settings(profile)


@pytest.mark.parametrize("profile", [
    {"languages": "python"}, {"languages": [False]}, {"languages": [None]},
    {"languages": [" "]}, {"languages": [] , "work_types": "documentation"},
    {"languages": ["python"], "work_types": [None]},
])
def test_discovery_workload_configuration_rejects_malformed_lists(profile):
    with pytest.raises(ValueError, match="languages|work_types"):
        load_workloads(profile)


def test_discovery_allows_documentation_only_and_canonical_language_aliases():
    assert load_workloads({"languages": [], "work_types": ["documentation"]}) == (set(), {"documentation"})
    assert load_workloads({"languages": ["C#", "csharp", " JS ", "python", "Python"]}) == (
        {"csharp", "javascript", "python"}, {"code"})
