"""Go, Rust, Java and C++: detected from repository files, searched under GitHub's language names, validated in
their own runtimes, accepted in profiles, and listed in scout.json for the desktop app. Offline: no GitHub or Docker."""
import json
import subprocess
from unittest.mock import Mock

import pytest

import ranking
import scout
from settings import ROOT, load_workloads
from test_scout_selection import candidate
from workloads import LANGUAGE_RUNTIMES, RUNTIME_IMAGES, TEST_PHASE, canonical_language

NEW = ["go", "rust", "java", "cpp"]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(scout.http, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(scout.time, "sleep", Mock())
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


def enable(monkeypatch, *languages):
    enabled = set(scout.LANGUAGES) | set(languages)
    monkeypatch.setattr(scout, "LANGUAGES", enabled)
    monkeypatch.setattr(ranking, "LANGUAGES", enabled)


def context(monkeypatch, files, contents=None):
    tree = {"tree": [{"path": name, "type": "blob"} for name in files], "truncated": False}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[{"default_branch": "main", "stargazers_count": 200}, tree]))
    monkeypatch.setattr(scout, "gh_file", Mock(side_effect=lambda repo, path: (contents or {}).get(path, "")))
    return scout.repo_context("acme/core")


@pytest.mark.parametrize("language, files, runtimes", [
    ("go", ["go.mod", "internal/parse.go"], ["go126", "go127"]),
    ("rust", ["Cargo.toml", "src/lib.rs"], ["rust1"]),
    ("java", ["pom.xml", "src/main/java/Parse.java"], ["java21_maven", "java25_maven"]),
    ("java", ["settings.gradle.kts", "build.gradle.kts", "gradlew", "src/main/java/Parse.java"], ["java21_gradle", "java25_gradle"]),
    ("cpp", ["CMakeLists.txt", "src/parse.cpp", "include/parse.hpp"], ["cpp_gcc14"]),
])
def test_repository_context_detects_new_languages_from_their_build_files(monkeypatch, language, files, runtimes):
    enable(monkeypatch, language)
    found = context(monkeypatch, files)
    assert found["status"] == "supported"
    assert found["languages"][language]["status"] == "supported"
    assert found["languages"][language]["runtimes"] == runtimes
    assert found["languages"]["python"]["status"] == "unsupported"
    assert "c" not in found["languages"]  # C++ sources and CMake alone are not C


@pytest.mark.parametrize("language, files", [
    ("go", ["cmd/tool/main.go"]), ("rust", ["src/main.rs"]), ("java", ["src/Parse.java"]), ("cpp", ["parse.cc"]),
])
def test_sources_without_build_files_need_confirmation(monkeypatch, language, files):
    enable(monkeypatch, language)
    found = context(monkeypatch, files)
    assert found["languages"][language]["status"] == "unknown"
    assert found["status"] == "unknown"


def test_a_repository_with_maven_and_gradle_builds_offers_both(monkeypatch):
    enable(monkeypatch, "java")
    found = context(monkeypatch, ["pom.xml", "build.gradle", "src/main/java/Parse.java"])
    assert found["languages"]["java"]["runtimes"] == ["java21_maven", "java25_maven", "java21_gradle", "java25_gradle"]
    assert found["languages"]["java"]["build_files"] == ["build.gradle", "pom.xml"]


@pytest.mark.parametrize("language, runtime, image", [
    ("go", "go127", "golang:1.27"), ("rust", "rust1", "rust:1"),
    ("java", "java21_gradle", "gradle:jdk21"), ("cpp", "cpp_gcc14", "gcc:14"),
])
def test_new_language_plans_run_only_in_their_own_runtime(monkeypatch, language, runtime, image):
    commands = []

    def docker(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=f"{TEST_PHASE}\n1 passed", stderr="")

    monkeypatch.setattr(scout.subprocess, "run", Mock(side_effect=docker))
    plan = {"runtime": runtime, "setup_commands": [], "test_commands": [["make", "test"]], "regression_marker": "SCOUT_REGRESSION:1"}
    brief = {"test_cwd": ".", "test_files": ["tests/parse_test"], "validation": plan}
    passed, _ = scout.run_validation("/checkout", brief, "code", language)
    assert passed and image in commands[0] and "-v" in commands[0] and "/checkout:/src:ro" in commands[0]
    other = "rust1" if language != "rust" else "go127"
    assert scout.run_validation("/checkout", brief | {"validation": plan | {"runtime": other}}, "code", language) == (
        False, "validation rejected or failed: validation runtime does not match the selected language")
    assert len(commands) == 1  # a mismatched runtime never starts a container


def test_discovery_searches_new_languages_under_githubs_names(monkeypatch):
    monkeypatch.setitem(scout.PROFILE, "languages", ["Go", "golang", "rust", "Java", "C++", "cpp"])
    monkeypatch.setitem(scout.PROFILE, "labels", ["bug"])
    monkeypatch.setitem(scout.PROFILE, "watchlist", [])
    api = Mock(return_value={"items": []})
    monkeypatch.setattr(scout, "gh", api)
    scout.discover({})
    queries = [call.kwargs["q"] for call in api.call_args_list if "language:" in call.kwargs["q"]]
    assert sorted(query.rsplit("language:", 1)[1] for query in queries) == ['"C++"', '"go"', '"java"', '"rust"']


def test_profiles_accept_the_new_languages_and_their_aliases_and_still_reject_others():
    assert load_workloads({"languages": ["Go", "golang", "Rust", "java", "C++", "cpp", " CPP "]}) == (set(NEW), {"code"})
    assert [canonical_language(name) for name in ["c++", "C Plus Plus", "golang", "Java"]] == ["cpp", "cpp", "go", "java"]
    for unsupported in (["kotlin"], ["go", "swift"]):
        with pytest.raises(ValueError):
            load_workloads({"languages": unsupported})


@pytest.mark.parametrize("language, body", [
    ("go", "panic in internal/parse.go when go test runs"),
    ("rust", "cargo test fails for src/lib.rs on stable rustc"),
    ("java", "NullPointerException in Parse.java under JUnit 5 and Maven"),
    ("cpp", "Crash in parse.cpp when built with g++ in C++17 mode"),
])
def test_issue_text_counts_as_evidence_for_enabled_new_languages(monkeypatch, language, body):
    enable(monkeypatch, *NEW)
    assert language in scout.issue_features(candidate(body=body))["languages"]


def test_common_words_and_c_files_are_not_mistaken_for_new_languages(monkeypatch):
    enable(monkeypatch, *NEW, "c")
    languages = scout.issue_features(candidate(title="Let's go ahead", body="We should go ahead with the fix in parse.c"))["languages"]
    assert "go" not in languages and "cpp" not in languages and "c" in languages


def test_scout_json_lists_exactly_the_languages_and_work_types_the_engine_runs():
    manifest = json.loads((ROOT / "scout.json").read_text(encoding="utf-8"))
    assert manifest == {"manifest_version": 1, "languages": list(LANGUAGE_RUNTIMES), "work_types": ["code", "documentation"]}
    assert all(runtime in RUNTIME_IMAGES for runtimes in LANGUAGE_RUNTIMES.values() for runtime in runtimes)
