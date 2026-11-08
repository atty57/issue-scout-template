"""Offline coverage for general contributions, ecosystem plans and docs verification."""
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest

import scout
from test_scout_selection import candidate, picker_option, picker_verdict
from workloads import RUNTIME_IMAGES, TEST_PHASE, check_documentation, validation_script


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    monkeypatch.setattr(scout.http, "get", Mock(side_effect=AssertionError("unexpected network call")))
    monkeypatch.setattr(scout.time, "sleep", Mock())
    monkeypatch.setattr(scout, "SEEN", tmp_path / "seen.json")
    monkeypatch.setattr(scout, "REPORTS", tmp_path / "reports")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.delenv("SCOUT_EMAIL_RELAY_URL", raising=False)


def plan(runtime="node24", **changes):
    return {"runtime": runtime, "setup_commands": [["npm", "ci"]],
            "test_commands": [["npm", "test", "--", "test/parse.test.js"]],
            "regression_marker": "SCOUT_REGRESSION:1"} | changes


def brief(runtime="node24", **changes):
    return {"test_cwd": ".", "test_files": ["test/parse.test.js"], "validation": plan(runtime)} | changes


def test_discovery_covers_configured_languages_and_language_independent_docs(monkeypatch):
    api = Mock(return_value={"items": []})
    monkeypatch.setattr(scout, "gh", api)
    scout.discover({})
    queries = [call.kwargs["q"] for call in api.call_args_list]
    assert all(any(f'language:"{language}"' in query for query in queries) for language in scout.PROFILE["languages"])
    assert any('label:"documentation"' in query and "language:" not in query for query in queries)
    assert any('label:"help wanted"' in query and "updated:>=" in query for query in queries)


def test_discovery_merges_language_evidence_across_duplicate_results(monkeypatch):
    issue = candidate(title="Wrong value for empty input", body="Expected zero, returns one.")
    monkeypatch.setattr(scout, "gh", Mock(return_value={"items": [issue]}))
    result = scout.discover({})["candidates"]
    assert len(result) == 1
    assert set(result[0]["search_languages"]) == scout.LANGUAGES
    assert "search_languages" not in issue  # Search snapshots are not mutated.


@pytest.mark.parametrize("minimum, stars, qualifies", [
    (200, 0, False), (200, 199, False), (200, 200, True), (200, 201, True),
    (500, 499, False), (500, 500, True),
])
def test_repository_star_minimum_is_inclusive_and_checked_before_tree(monkeypatch, minimum, stars, qualifies):
    monkeypatch.setitem(scout.SCOUT, "min_stars", minimum)
    metadata = {"default_branch": "main", "stargazers_count": stars}
    tree = {"tree": [{"path": "README.md", "type": "blob"}]}
    api = Mock(side_effect=[metadata, tree])
    monkeypatch.setattr(scout, "gh", api)
    context = scout.repo_context("acme/docs")
    assert (context["status"] == "supported") is qualifies
    assert context["stars"] == stars
    assert api.call_count == (2 if qualifies else 1)
    if not qualifies:
        assert str(stars) in context["reason"] and str(minimum) in context["reason"]


@pytest.mark.parametrize("metadata", [
    {}, {"stargazers_count": None}, {"stargazers_count": "200"},
    {"stargazers_count": True}, {"stargazers_count": -1},
])
def test_unverified_star_count_cannot_reach_selection(monkeypatch, metadata):
    api = Mock(return_value=metadata)
    monkeypatch.setattr(scout, "gh", api)
    context = scout.repo_context("acme/docs")
    assert context["status"] == "unsupported"
    assert context["reason"] == "repository star count is unavailable"
    api.assert_called_once_with("repos/acme/docs")


@pytest.mark.parametrize("watchlisted", [False, True])
def test_star_gate_backfills_docs_and_caches_repository_checks(monkeypatch, capsys, watchlisted):
    monkeypatch.setitem(scout.PROFILE, "watchlist", ["acme/low"] if watchlisted else [])
    monkeypatch.setitem(scout.SCOUT, "shortlist_size", 1)
    monkeypatch.setattr(scout, "policy", Mock(return_value={"status": "none", "quote": ""}))
    tree = {"tree": [{"path": "README.md", "type": "blob"}]}

    def github(path, **params):
        if "/git/trees/" in path:
            assert path.startswith("repos/acme/qualified/")
            return tree
        return {"default_branch": "main", "stargazers_count": 199 if path == "repos/acme/low" else 200}

    api = Mock(side_effect=github)
    enrich = Mock(side_effect=lambda issue: dict(issue, thread="", features=scout.issue_features(issue)))
    monkeypatch.setattr(scout, "gh", api)
    monkeypatch.setattr(scout, "enrich_issue", enrich)
    issues = [candidate(number, repo, title="Typo in README installation command", body="Use the corrected command.",
                        labels=[{"name": "documentation"}])
              for number, repo in [(1, "acme/low"), (2, "acme/low"), (3, "acme/qualified"), (4, "acme/qualified")]]
    first = scout.gate({"candidates": issues})
    second = scout.gate(first)
    assert [item["number"] for item in first["candidates"]] == [3]
    assert [item["number"] for item in second["candidates"]] == [4]
    assert [call.args[0]["repo"] for call in enrich.call_args_list] == ["acme/qualified"] * 2
    assert sum(call.args == ("repos/acme/low",) for call in api.call_args_list) == 1
    assert sum(call.args == ("repos/acme/qualified",) for call in api.call_args_list) == 1
    assert "repository has 199 stars; minimum is 200" in capsys.readouterr().out


@pytest.mark.parametrize("language, files, contents", [
    ("javascript", ["package.json", "src/parser.js"], {"package.json": '{"scripts":{"test":"node --test"}}'}),
    ("typescript", ["package.json", "src/parser.ts"], {"package.json": '{"scripts":{"test":"vitest run"}}'}),
    ("csharp", ["Core.csproj", "Parser.cs"], {"Core.csproj": '<Project><PropertyGroup><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>'}),
    ("c", ["CMakeLists.txt", "src/parser.c"], {}),
    ("documentation", ["README.md", "docs/setup.rst"], {}),
])
def test_repository_context_detects_non_python_workloads(monkeypatch, language, files, contents):
    tree = {"tree": [{"path": name, "type": "blob"} for name in files], "truncated": False}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[{"default_branch": "main", "stargazers_count": 200}, tree]))
    monkeypatch.setattr(scout, "gh_file", Mock(side_effect=lambda repo, path: contents.get(path, "")))
    context = scout.repo_context("acme/core")
    assert context["status"] == "supported"
    assert context["languages"][language]["status"] == "supported"
    assert context["languages"]["python"]["status"] == "unsupported"


def test_windows_only_csharp_does_not_block_documentation(monkeypatch):
    tree = {"tree": [{"path": name, "type": "blob"} for name in ["App.csproj", "README.md"]]}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[{"default_branch": "main", "stargazers_count": 200}, tree]))
    monkeypatch.setattr(scout, "gh_file", Mock(return_value='<Project><TargetFramework>net8.0-windows</TargetFramework></Project>'))
    context = scout.repo_context("acme/core")
    assert context["status"] == "supported"
    assert context["languages"]["csharp"]["status"] == "unsupported"
    assert context["languages"]["documentation"]["status"] == "supported"


def test_netstandard_library_needs_test_plan_instead_of_being_rejected(monkeypatch):
    tree = {"tree": [{"path": "Core.csproj", "type": "blob"}]}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[{"default_branch": "main", "stargazers_count": 200}, tree]))
    monkeypatch.setattr(scout, "gh_file", Mock(return_value='<Project><TargetFramework>netstandard2.0</TargetFramework></Project>'))
    context = scout.repo_context("acme/core")
    assert context["status"] == "unknown"
    assert context["languages"]["csharp"]["status"] == "unknown"


def test_pinned_dotnet_sdk_is_evidence_not_assumed_container_compatibility(monkeypatch):
    tree = {"tree": [{"path": name, "type": "blob"} for name in ["Core.csproj", "global.json"]]}
    monkeypatch.setattr(scout, "gh", Mock(side_effect=[{"default_branch": "main", "stargazers_count": 200}, tree]))
    files = {"Core.csproj": '<Project><TargetFramework>net8.0</TargetFramework></Project>',
             "global.json": '{"sdk":{"version":"8.0.100"}}'}
    monkeypatch.setattr(scout, "gh_file", Mock(side_effect=lambda repo, path: files[path]))
    context = scout.repo_context("acme/core")
    assert context["languages"]["csharp"]["status"] == "unknown"
    assert context["languages"]["csharp"]["sdk_selection"]["version"] == "8.0.100"


@pytest.mark.parametrize("kind, language, title, labels", [
    ("code", "javascript", "Wrong result for empty input", [{"name": "bug"}]),
    ("code", "typescript", "Wrong result for empty input", [{"name": "bug"}]),
    ("code", "csharp", "Wrong result for empty input", [{"name": "bug"}]),
    ("code", "c", "Wrong result for empty input", [{"name": "bug"}]),
    ("documentation", "none", "Typo in the README installation command", [{"name": "documentation"}]),
])
def test_general_issues_can_be_selected_without_topic_keywords(monkeypatch, kind, language, title, labels):
    issue = candidate(title=title, body="Expected the documented result, but the current result is incorrect.", labels=labels)
    issue["search_languages"] = [language] if kind == "code" else []
    assert not scout.issue_features(issue)["matched_skills"]
    issue["feasibility"] = {"status": "supported", "languages": {
        language if kind == "code" else kind: {"status": "supported"}}}
    verdict = picker_verdict()
    verdict["evaluations"][0].update(work_type=kind, language=language,
                                    validation_plan={"javascript": "node --test", "typescript": "vitest run",
                                                     "csharp": "dotnet build and dotnet test --no-build", "c": "gcc build and execute regression binary"}.get(
                                                         language, "Static document and local link checks"))
    model = Mock(invoke=Mock(return_value=verdict))
    monkeypatch.setattr(scout, "llm", Mock(return_value=model))
    result = scout.pick({"candidates": [issue]})
    assert result["issue"]["work_type"] == kind
    assert result["issue"]["language"] == language
    assert "a failing code regression test is NOT required" in model.invoke.call_args.args[0]


def test_watchlist_general_issue_can_reach_repository_detection_without_topic_words():
    issue = candidate(repo=scout.PROFILE["watchlist"][0], title="Incorrect parser result",
                      body="Expected zero for empty input; actual result is one. Steps to reproduce: pass an empty string.")
    assert not scout.issue_features(issue)["matched_skills"]
    assert scout.profile_fit(issue)
    assert scout.shortlist({"candidates": [issue]})["candidates"] == [issue]


def test_pool_preserves_language_and_documentation_coverage(monkeypatch):
    monkeypatch.setattr(scout, "SCOUT", scout.SCOUT | {"candidate_limit": 3})
    python = [candidate(n, f"acme/python-{n}") for n in range(1, 20)]
    javascript = candidate(20, "acme/javascript", title="Incorrect result", body="", search_languages=["javascript"])
    docs = candidate(21, "acme/manual", title="README typo", body="", labels=[{"name": "documentation"}])
    pool = scout.shortlist({"candidates": python + [javascript, docs]})["candidates"]
    assert len(pool) == 3
    assert javascript in pool and docs in pool


def test_score_components_are_explainable_and_activity_is_a_preference():
    issue = candidate()
    assert sum(scout.score_components(issue).values()) == scout.score(issue)
    active = dict(issue, feasibility={"pushed_at": datetime.now(timezone.utc).isoformat()})
    assert scout.score_components(active)["repo_activity"] > scout.score_components(issue)["repo_activity"]
    assert scout.score(issue) >= scout.SCOUT["min_score"]


@pytest.mark.parametrize("runtime, language", [("node22", "javascript"), ("node24", "typescript"),
                                             ("node26", "javascript"), ("dotnet8", "csharp"),
                                             ("dotnet10", "csharp"), ("c_gcc14", "c"), ("python312", "python")])
def test_validation_uses_selected_container_with_no_host_command_execution(monkeypatch, runtime, language):
    execute = Mock(return_value=Mock(returncode=0, stdout=TEST_PHASE + "\nPassed", stderr=""))
    monkeypatch.setattr(scout.subprocess, "run", execute)
    passed, log = scout.run_validation("/checkout", brief(runtime), "code", language)
    assert passed and TEST_PHASE in log
    argv = execute.call_args.args[0]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert RUNTIME_IMAGES[runtime] in argv
    assert "/checkout:/src:ro" in argv
    assert not execute.call_args.kwargs.get("shell")


def test_command_arguments_are_quoted_and_setup_precedes_test_phase():
    script = validation_script(plan(test_commands=[["node", "--test", "test name; $(touch /oops).js"]]), ".")
    assert "'test name; $(touch /oops).js'" in script
    assert script.index("npm ci") < script.index(TEST_PHASE) < script.index("node --test")


@pytest.mark.parametrize("bad", [
    {"runtime": "arbitrary/image"}, {"test_commands": []}, {"test_commands": ["npm test"]},
    {"setup_commands": [["npm", None]]},
])
def test_invalid_validation_plan_never_starts_container(monkeypatch, bad):
    execute = Mock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(scout.subprocess, "run", execute)
    assert not scout.run_validation("/checkout", brief(validation=plan(**bad)), "code", "javascript")[0]
    execute.assert_not_called()


def test_wrong_ecosystem_runtime_is_rejected(monkeypatch):
    execute = Mock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(scout.subprocess, "run", execute)
    passed, log = scout.run_validation("/checkout", brief("python312"), "code", "csharp")
    assert not passed and "does not match" in log
    execute.assert_not_called()


def test_dependency_failure_is_distinguished_from_a_test_failure(monkeypatch):
    monkeypatch.setattr(scout.subprocess, "run", Mock(return_value=Mock(returncode=1, stdout="npm ci failed", stderr="")))
    passed, log = scout.run_validation("/checkout", brief(), "code", "javascript")
    assert not passed and "before validation started" in log
    assert TEST_PHASE not in log


@pytest.mark.parametrize("name", ["../test.js", "C:/test.js", "test\\file.js", "*.js", "/test.js"])
def test_unsafe_regression_paths_are_rejected_before_running(monkeypatch, name):
    execute = Mock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(scout.subprocess, "run", execute)
    assert not scout.run_validation("/checkout", brief(test_files=[name]), "code", "javascript")[0]
    execute.assert_not_called()


@pytest.mark.parametrize("failed_log, expected", [("Compiler failed SCOUT_REGRESSION:1", False),
                                                 (TEST_PHASE + "\nassert failed elsewhere", False),
                                                 (TEST_PHASE + "\nSCOUT_REGRESSION:1", True)])
def test_regression_proof_needs_test_execution_and_target_assertion(monkeypatch, tmp_path, failed_log, expected):
    def git(cwd, *args):
        if args[0] == "apply":
            path = Path(cwd, "test/parse.test.js")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("assert(value === 0, 'SCOUT_REGRESSION:1')")
        return ""
    monkeypatch.setattr(scout, "git", git)
    monkeypatch.setattr(scout, "run_validation", Mock(return_value=(False, failed_log)))
    assert scout.fails_without_fix(str(tmp_path), "test patch", brief())[0] is expected


def test_static_docs_checks_links_and_accepts_rst_heading(tmp_path):
    (tmp_path / "guide.rst").write_text("Heading\n=======\nText.\n")
    (tmp_path / "README.md").write_text("[Guide](guide.rst)\n")
    passed, log = check_documentation(tmp_path, ["README.md", "guide.rst"])
    assert passed and "Human review" in log
    (tmp_path / "README.md").write_text("[Guide](missing.rst)\n")
    assert not check_documentation(tmp_path, ["README.md"])[0]


def test_docs_checks_reject_code_changes_and_conflict_markers(tmp_path):
    (tmp_path / "README.md").write_text("<<<<<<< HEAD\nUnresolved text\n>>>>>>> branch\n")
    assert not check_documentation(tmp_path, ["README.md"])[0]
    assert not check_documentation(tmp_path, ["src/parser.js"])[0]
    assert not check_documentation(tmp_path, ["CMakeLists.txt"])[0]
    assert not check_documentation(tmp_path, ["requirements.txt"])[0]


def test_docs_checks_ignore_code_examples_and_generated_routes(tmp_path):
    (tmp_path / "README.md").write_text("```md\n[Example](missing.md)\n```\n[API](api/page.html)\n[Route](/guide/)\n")
    assert check_documentation(tmp_path, ["README.md"])[0]


def test_documentation_solve_validates_patch_without_inventing_regression(monkeypatch, tmp_path, capsys):
    repo = tmp_path / "manual"
    repo.mkdir()
    (repo / "README.md").write_text("Install the packge.\n")
    scout.git(repo, "init", "-q")
    scout.git(repo, "add", ".")
    scout.git(repo, "-c", "user.email=test@example.invalid", "-c", "user.name=test", "commit", "-qm", "initial")
    issue = picker_option()
    issue.update(work_type="documentation", language="none", title="Fix README typo")
    review = {key: "review detail" for key in scout.BRIEF_SCHEMA["required"]}
    review.update(test_cwd=".", test_files=[], maintainer_questions=["Confirmed against source"], checklist=["Review accuracy"],
                  validation=plan("documentation", setup_commands=[], test_commands=[], regression_marker=""))

    def claude(prompt, cwd, session=None):
        assert "do not invent a code regression test" in prompt
        (Path(cwd) / "README.md").write_text("Install the package.\n")
        return {"structured_output": review, "session_id": "doc-session"}

    monkeypatch.setattr(scout, "clone", Mock(return_value=str(repo)))
    monkeypatch.setattr(scout, "claude", claude)
    regression = Mock(side_effect=AssertionError("docs must not require a code regression"))
    monkeypatch.setattr(scout, "fails_without_fix", regression)
    result = scout.solve({"issue": issue})["result"]
    assert result["passed"] and result["documentation_verified"]
    assert result["reproduces"] is None
    assert "+Install the package." in result["patch"]
    regression.assert_not_called()
    scout.notify({"issue": issue, "result": result, "why": "Precise documentation correction"})
    assert "code regression: not applicable" in capsys.readouterr().out
    record = scout.load_history()[issue["html_url"]]
    assert record["status"] == "notified" and record["documentation_verified"]
    assert record["reproduces"] is None and "retry_after" not in record


def test_docs_notification_without_verified_document_checks_stays_retryable(capsys):
    issue = picker_option()
    issue.update(work_type="documentation", language="none")
    review = {key: "review detail" for key in scout.BRIEF_SCHEMA["required"]}
    review.update(maintainer_questions=[], checklist=[])
    result = {"brief": review, "patch": "+Documentation change\n", "passed": True,
              "reproduces": None, "documentation_verified": False, "cost": 0, "log": ""}
    scout.notify({"issue": issue, "result": result, "why": "Focused documentation change"})
    capsys.readouterr()
    record = scout.load_history()[issue["html_url"]]
    assert record["status"] == "failed" and "retry_after" in record
