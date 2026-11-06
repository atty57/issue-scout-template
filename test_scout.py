"""Runnable checks, no network/Docker/API keys: python test_scout.py (or pytest)."""
import contextlib
import email
import email.policy
import inspect
import io
import json
import os
import re
import tempfile

import pytest
from pathlib import Path
from unittest.mock import patch as mock_patch

import email_html
import scout
import settings

SENTINEL = "sentinel-abcdefghijklmnop"


@contextlib.contextmanager
def env(values):
    """Set env vars for the block (a None value unsets), then restore."""
    def apply(items):
        for name, value in items.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    saved = {name: os.environ.get(name) for name in values}
    apply(values)
    try:
        yield
    finally:
        apply(saved)

BUGGY, FIXED = "def add(a, b):\n    return a - b\n", "def add(a, b):\n    return a + b\n"
TEST = "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"


def issue(repo, number, comments=1):
    return {"title": f"add() returns wrong result in {repo}", "body": "pydantic agent bug, add(2, 3) gives -1",
            "state": "open", "assignees": [],
            "author_association": "MEMBER", "labels": [{"name": "bug"}], "number": number, "comments": comments,
            "repository_url": f"https://api.github.com/repos/{repo}",
            "html_url": f"https://github.com/{repo}/issues/{number}"}


def test_claim_regex():
    for s in ("I'd like to work on this", "Can I take this?", "please assign it to me", "I'm working on a fix",
              "I'll pick this up"):
        assert scout.CLAIM.search(s), s
    for s in ("I can reproduce on 3.12", "This affects me too", "Is anyone working on this?"):
        assert not scout.CLAIM.search(s), s


def test_ai_paragraphs_and_paths():
    text = "Use black.\n\nWe do not accept AI-generated PRs.\n\nThe maintainers said hi."
    assert scout.ai_paragraphs(text) == "We do not accept AI-generated PRs."
    assert all(scout.safe_path(p) for p in (".", "tests/test_x.py", "python/packages/core"))
    assert not any(scout.safe_path(p) for p in ("", "/etc/passwd", "../x.py", "--rootdir=/", "a/../../b"))


def test_redact_every_secret_and_its_rewrites():
    with env({name: f"{SENTINEL}-{i}" for i, name in enumerate(scout.SECRETS)}):
        for name in scout.SECRETS:
            for form in scout.forms(os.environ[name]):
                assert scout.redact(f"log line {form} end") == "log line [redacted] end", name
    # unset secrets leave the text alone, and a short value must not eat it character by character
    with env(dict.fromkeys(scout.SECRETS) | {"GROQ_API_KEY": "a"}):
        assert scout.redact("a patch about aardvarks") == "a patch about aardvarks"


def test_workflow_passes_no_secret_redact_has_never_heard_of():
    workflow = (scout.ROOT / ".github" / "workflows" / "daily.yml").read_text()
    passed = set(re.findall(r"^\s+([A-Z][A-Z_]+):\s*\$\{\{", workflow, re.M))
    assert passed, "no env block found; the regex or the workflow moved"
    not_a_credential = {"GMAIL_ADDRESS"}  # my own inbox: it is the To: line, there is nothing to redact
    assert passed <= set(scout.SECRETS) | not_a_credential, passed - set(scout.SECRETS)


def test_scan_secrets_catches_shapes_and_leaves_our_own_files_alone():
    leaks = ["ghp_" + "a" * 36, "sk-ant-api03-" + "x" * 40, "gsk_" + "b" * 52, "xoxb-123456789012-abcdef",
             "AKIA" + "Z" * 16, "-----BEGIN RSA PRIVATE KEY-----", "Authorization: Bearer " + "c" * 40,
             'API_KEY = "' + "d" * 24 + '"', "GMAIL_APP_PASSWORD=abcd efgh ijkl mnop"]
    for leak in leaks:
        assert scout.scan_secrets(f"diff --git a/x b/x\n+{leak}\n"), leak
    clean = [(scout.ROOT / "README.md").read_text(), (scout.ROOT / "scout.py").read_text(),
             (scout.ROOT / ".github" / "workflows" / "daily.yml").read_text(),
             "GROQ_API_KEY=\nAPI_KEY=sk-xxxxx\nTOKEN=<your-token-here>",
             "when they came home the tests were green",
             "see 9f1a2b3c4d5e6f708192a3b4c5d6e7f8091a2b3c, uuid 3f2504e0-4f89-11d3-9a0c-0305e82c3301"]
    for text in clean:
        assert not scout.scan_secrets(text), (scout.scan_secrets(text), text[:80])


def test_nothing_leaves_the_process_without_going_through_outbound():
    """notify() dropped this gate twice in rewrites; assert the wiring, not just the function."""
    body, patch = scout.outbound("brief", "diff --git a/x b/x\n+ghp_" + "a" * 36 + "\n")
    assert "PATCH WITHHELD: github token" in body and patch == ""
    assert scout.outbound("brief", "diff --git a/x b/x\n+return a + b\n")[1]  # clean patch survives
    compose = inspect.getsource(scout.compose)
    assert "outbound(" in compose, "compose stopped using outbound(); the shape-based gate is bypassed"
    assert "set_content(redact(" not in compose, "compose redacts without the fail-closed scan"
    assert "compose(" in inspect.getsource(scout.notify), "notify stopped routing the mail through compose()"


def test_settings_reject_unusable_profile_values():
    """Validation is a function now, so a bad profile can be checked without reimporting."""
    assert settings.load_settings({}) == settings.DEFAULTS
    assert settings.load_settings({"scouting": {"min_stars": 1}})["min_stars"] == 1
    for bad, message in [({"min_stars": 0}, "scouting.min_stars must be a positive integer"),
                         ({"search_pages": "two"}, "scouting.search_pages must be a positive integer"),
                         ({"max_attempts": True}, "scouting.max_attempts must be a positive integer"),
                         ({"shortlist_size": 9}, "scouting.shortlist_size must be at most 8 (picker context budget)")]:
        with pytest.raises(ValueError, match=re.escape(message)):
            settings.load_settings({"scouting": bad})
    assert settings.load_workloads({"languages": ["Python"]}) == ({"python"}, {"code"})
    for bad in ({"languages": ["cobol"]}, {"languages": ["python"], "work_types": []},
                {"languages": ["python"], "work_types": ["code", "art"]}):
        with pytest.raises(ValueError, match="unsupported profile language or work type"):
            settings.load_workloads(bad)


def test_agent_runs_with_no_shell_no_network_and_no_spare_secrets():
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        raise RuntimeError("stop before launching anything")

    saved, scout.subprocess.run = scout.subprocess.run, fake_run
    try:
        with env({name: f"{SENTINEL}-{i}" for i, name in enumerate(scout.SECRETS)}):
            with contextlib.suppress(RuntimeError):
                scout.claude("prompt", "/repo")
    finally:
        scout.subprocess.run = saved

    cmd = seen["cmd"]
    allowed = set(cmd[cmd.index("--allowedTools") + 1].split(","))
    assert allowed <= {"Read", "Edit", "Write", "Glob", "Grep"}, allowed
    assert {"Bash", "WebFetch", "WebSearch"} <= set(cmd[cmd.index("--disallowedTools") + 1].split(","))
    assert seen["cwd"] == "/repo"
    assert [n for n in scout.SECRETS if n in seen["env"]] == list(scout.AGENT_KEEPS)


def test_llm_asks_groq_to_enforce_the_schema_itself():
    """Groq's default (function_calling) 400s on gpt-oss: 'model did not call a tool', and gate fails closed."""
    with env({"GROQ_API_KEY": "gsk_not_a_real_key"}):
        for schema in (scout.POLICY_SCHEMA, scout.PICK_SCHEMA):
            fmt = scout.llm(schema).first.kwargs["response_format"]
            assert fmt["type"] == "json_schema" and fmt["json_schema"]["name"] == schema["title"]


def test_traces_leave_the_process_without_secrets():
    with env({name: f"{SENTINEL}-{i}" for i, name in enumerate(scout.SECRETS)}):
        traced = scout.scrub({"prompt": f"log {os.environ['GROQ_API_KEY']}", "log": (True, os.environ["GH_TOKEN"])})
    assert SENTINEL not in json.dumps(traced)
    assert scout.claude.__wrapped__ and scout.run_tests.__wrapped__  # still traced at all


def test_sandbox_is_throwaway_read_only_and_secretless():
    argv = scout.sandbox("/repo", "true")
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "-e"] == ["SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0", "PIP_NO_CACHE_DIR=1"]
    assert "--rm" in argv and [a for a in argv if a.startswith("/") and ":" in a] == ["/repo:/src:ro"]
    assert not any(a == "--privileged" or "docker.sock" in a or a == "--network=host" for a in argv)


def test_diff_never_stages_the_repos_agent_config():
    calls = []

    def fake_git(cwd, *args):
        calls.append(args)
        return ""

    saved, scout.git = scout.git, fake_git
    try:
        scout.diff("/repo")
    finally:
        scout.git = saved
    assert {":(exclude).claude", ":(exclude).mcp.json"} <= set(calls[0])


def test_pipeline_end_to_end():
    tmp = Path(tempfile.mkdtemp())
    # a "GitHub" with one buggy repo whose tracked .claude/ must never reach the patch
    origin = tmp / "acme" / "calc.git"
    origin.mkdir(parents=True)
    (origin / "calc.py").write_text(BUGGY)
    (origin / ".claude").mkdir()
    (origin / ".claude" / "settings.json").write_text('{"hooks": {}}')
    for args in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"]):
        scout.git(origin, *args)

    issues = [issue("acme/calc", 1), issue("acme/claimed", 2), issue("acme/strict", 3)]
    comments = {"acme/calc": [{"user": {"login": "maint"}, "author_association": "MEMBER",
                              "body": "Confirmed, PRs welcome."}],
                "acme/claimed": [{"user": {"login": "dev"}, "body": "Can I work on this?"}],
                "acme/strict": []}
    calls = []

    def fake_gh(path, **params):
        if path == "search/issues":
            return {"items": [dict(i) for i in issues]}
        if "/git/trees/" in path:
            return {"tree": [{"path": "pyproject.toml", "type": "blob"},
                             {"path": "tests/test_calc.py", "type": "blob"}]}
        if path.endswith("/timeline"):
            return []
        if path.endswith("/comments"):
            return comments[path.split("/issues/")[0].removeprefix("repos/")]
        if "/issues/" in path:
            repo, number = path.removeprefix("repos/").split("/issues/")
            return next(dict(i) for i in issues if i["number"] == int(number) and i["repository_url"].endswith(repo))
        return {"default_branch": "main", "archived": False, "disabled": False, "stargazers_count": 200}

    def fake_file(repo, path):
        if path == "pyproject.toml":
            return '[project]\nname = "calc"\nrequires-python = ">=3.10"\n'
        return "We do not accept AI-generated contributions." if repo == "acme/strict" and path == "CONTRIBUTING.md" else ""

    class FakeLLM:
        def __init__(self, schema):
            self.schema = schema

        def invoke(self, prompt):
            if self.schema["title"] == "policy":
                raise RuntimeError("no key")  # must fail closed
            assert "Confirmed, PRs welcome." in prompt
            return {"index": 0, "confidence": 0.9, "why": "maintainer confirmed, small fix",
                    "evaluations": [{"index": 0, "suitable": True, "reason": "maintainer confirmed, small fix",
                                     "work_type": "code", "language": "python", "validation_plan": "pytest addition regression",
                                     "unmet_requirements": [], "resolution_evidence": ""}]}

    def fake_claude(prompt, cwd, session=None):
        calls.append(session)
        Path(cwd, "tests").mkdir(exist_ok=True)
        Path(cwd, "tests", "test_calc.py").write_text(TEST.replace("== 5", "== 5, 'SCOUT_REGRESSION:1'"))
        if session:  # first attempt forgets the fix, so the retry loop has to kick in
            Path(cwd, "calc.py").write_text(FIXED)
        brief = {k: f"{k} {os.environ['GROQ_API_KEY']}" for k in scout.BRIEF_SCHEMA["required"]}
        brief.update(test_cwd=".", test_files=["tests/test_calc.py"], maintainer_questions=["Why?"], checklist=["Sign CLA"])
        brief["validation"] = {"runtime": "python312", "setup_commands": [["pip", "install", "pytest"]],
                               "test_commands": [["python", "-m", "pytest", "tests/test_calc.py"]],
                               "regression_marker": "SCOUT_REGRESSION:1"}
        return {"structured_output": brief, "session_id": "s1", "total_cost_usd": 0.42}

    def fake_run_tests(repo_dir, cwd, files):
        """Stands in for the Docker adapter at run_tests' own seam: no container on a dev box."""
        assert cwd == "." and files == ["tests/test_calc.py"], (cwd, files)
        fixed = FIXED.strip() in Path(repo_dir, "calc.py").read_text()
        return fixed, "" if fixed else "E       assert add(2, 3) == 5"

    def fake_run_validation(repo_dir, brief, work_type="code", language="python"):
        assert work_type == "code" and language == "python"
        assert brief["validation"]["runtime"] == "python312"
        passed, log = fake_run_tests(repo_dir, brief["test_cwd"], brief["test_files"])
        return passed, scout.TEST_PHASE + "\n" + ("" if passed else "SCOUT_REGRESSION:1\n") + log

    out = io.StringIO()
    with env({"GROQ_API_KEY": "sekret-1234567890", "GMAIL_APP_PASSWORD": None, "LANGSMITH_TRACING": "false"}), \
            mock_patch.multiple(scout, gh=fake_gh, gh_file=fake_file, llm=FakeLLM, claude=fake_claude,
                           run_validation=fake_run_validation, GITHUB=f"file://{tmp}", SEEN=tmp / "state" / "seen.json",
                           REPORTS=tmp / "reports"), \
            mock_patch.object(scout.time, "sleep"), contextlib.redirect_stdout(out):
        scout.build().invoke({})
    log, raw = out.getvalue().split("Subject:", 1)  # dry run prints progress, then the email
    msg = email.message_from_string("Subject:" + raw, policy=email.policy.default)
    body, patch = msg.get_body(preferencelist=("plain",)).get_content(), next(msg.iter_attachments()).get_content()

    assert "gate: skip acme/strict" in log                        # banned repo skipped (fail closed)
    assert "active claim by dev" in log and "acme/claimed" not in body  # claimed issue skipped
    assert calls == [None, "s1"]                                  # retried once via --resume
    assert "Tests: pass" in body and "unfixed code: yes" in body  # green, and the test is red without the fix
    assert "Sign CLA" in body and "sekret-1234567890" not in body + patch  # checklist present, secrets redacted
    assert "+    return a + b" in patch and ".claude" not in patch  # fix attached, repo's .claude never in it
    history = json.loads((tmp / "state" / "seen.json").read_text())
    assert list(history) == ["https://github.com/acme/calc/issues/1"]
    assert history["https://github.com/acme/calc/issues/1"]["status"] == "notified"


def _mail_state(**brief):
    b = {"claim_comment": "I'd like to take this.", "root_cause": "rc", "approach": "ap", "risks": "ri",
         "not_verified": "nv", "maintainer_questions": ["q1"], "checklist": ["Sign CLA"],
         "commit_message": "fix: x", "pr_title": "Fix x", "pr_body": "body"} | brief
    issue = {"repo": "acme/calc", "number": 1, "title": "<script>alert(1)</script> add() wrong",
             "html_url": "https://github.com/acme/calc/issues/1", "policy": {"status": "none", "quote": ""}}
    return {"issue": issue, "why": "fits", "result": {"brief": b, "patch": "diff --git a/x b/x\n+ok\n",
            "passed": True, "reproduces": True, "log": "", "regression_log": "", "cost": 0.5}}


def test_html_email_escapes_and_rejects_non_https_links():
    page = email_html.render(_mail_state(), 7)
    assert "<script>" not in page and "&lt;script&gt;" in page
    state = _mail_state(); state["issue"]["html_url"] = "javascript:alert(1)"
    assert "javascript:" not in email_html.render(state, 7)
    failed = _mail_state(); failed["result"] = {"error": "RuntimeError: boom"}
    assert "No fix this time" in email_html.render(failed, 7)


def test_html_part_is_dropped_when_anything_credential_shaped_survives(monkeypatch):
    monkeypatch.setattr(scout, "score", lambda issue: 7)
    msg, body, _ = scout.compose(_mail_state())
    assert msg.get_body(preferencelist=("html",)) is not None   # clean brief: HTML alternative attached
    msg, body, _ = scout.compose(_mail_state(pr_body="ghp_" + "a" * 36))
    assert msg.get_body(preferencelist=("html",)) is None       # fail closed: text-only
    assert "ghp_" + "a" * 36 not in msg.as_string()

if __name__ == "__main__":
    test_claim_regex()
    test_ai_paragraphs_and_paths()
    test_redact_every_secret_and_its_rewrites()
    test_workflow_passes_no_secret_redact_has_never_heard_of()
    test_scan_secrets_catches_shapes_and_leaves_our_own_files_alone()
    test_llm_asks_groq_to_enforce_the_schema_itself()
    test_nothing_leaves_the_process_without_going_through_outbound()
    test_traces_leave_the_process_without_secrets()
    test_settings_reject_unusable_profile_values()
    test_agent_runs_with_no_shell_no_network_and_no_spare_secrets()
    test_sandbox_is_throwaway_read_only_and_secretless()
    test_diff_never_stages_the_repos_agent_config()
    test_pipeline_end_to_end()
    print("all checks passed")
