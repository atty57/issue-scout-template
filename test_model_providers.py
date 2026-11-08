"""Provider contracts, credentials, and shell-free editing; no live inference."""
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import model_providers as providers
import scout
from provider_config import ProviderConfig, SECRET_NAMES, load_provider
from repository_tools import RepositoryTools


SMALL_SCHEMA = {"title": "answer", "type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]}


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    for name in SECRET_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RUNNER_ENVIRONMENT", raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setattr(providers.requests, "post", Mock(side_effect=AssertionError("unexpected live model request")))


def test_defaults_and_independent_picker_solver_selection():
    assert load_provider("picker", {}).provider == "groq"
    assert load_provider("solver", {}).provider == "claude-code"
    env = {"SCOUT_PICKER_PROVIDER": "anthropic", "SCOUT_PICKER_MODEL": "chosen-picker",
           "SCOUT_SOLVER_PROVIDER": "codex", "SCOUT_SOLVER_MODEL": "chosen-solver"}
    assert load_provider("picker", env).model == "chosen-picker"
    assert load_provider("solver", env).model == "chosen-solver"
    assert load_provider("picker", {"SCOUT_MODEL": "legacy-model"}).model == "legacy-model"
    assert load_provider("picker", {"SCOUT_PICKER_MODEL": ""}).model == "openai/gpt-oss-120b"
    assert load_provider("picker", {"SCOUT_PICKER_BASE_URL": ""}).base_url == "https://api.groq.com/openai/v1"


@pytest.mark.parametrize("url", ["http://remote.example/v1", "https://user:password@api.example/v1",
                                     "https://api.example/v1?key=secret", "https://api.example/v1#fragment", "file:///tmp/model"])
def test_reject_unsafe_api_endpoints(url):
    with pytest.raises(ValueError, match="BASE_URL"):
        load_provider("solver", {"SCOUT_SOLVER_PROVIDER": "openai-compatible", "SCOUT_SOLVER_BASE_URL": url})


@pytest.mark.parametrize("url", ["https://api.example/v1", "http://127.0.0.1:1234/v1", "http://localhost:8080/v1", "http://[::1]:8080/v1"])
def test_https_and_loopback_api_endpoints(url):
    assert load_provider("solver", {"SCOUT_SOLVER_PROVIDER": "openai-compatible", "SCOUT_SOLVER_BASE_URL": url}).base_url == url


@pytest.mark.parametrize("values", [
    {"SCOUT_SOLVER_PROVIDER": "unknown"}, {"SCOUT_SOLVER_AUTH": "made-up"},
    {"SCOUT_SOLVER_PROVIDER": "openai", "SCOUT_SOLVER_AUTH": "subscription"},
])
def test_invalid_provider_selection_fails_early(values):
    with pytest.raises(ValueError):
        load_provider("solver", values)


def test_provider_credentials_and_models_are_required_only_when_selected(monkeypatch):
    config = ProviderConfig("solver", "openai", "chosen-model", "https://api.openai.com/v1")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        providers.check_provider(config)
    monkeypatch.setenv("SCOUT_SOLVER_API_KEY", "private-solver-key")
    providers.check_provider(config)  # no Groq, Claude, Gmail, or other provider credentials
    assert config.api_key == "private-solver-key"
    with pytest.raises(ValueError, match="MODEL"):
        providers.check_provider(ProviderConfig("picker", "anthropic"))


def test_subscription_modes_never_inherit_api_keys_and_other_secrets(monkeypatch):
    for index, name in enumerate(SECRET_NAMES):
        monkeypatch.setenv(name, f"secret-sentinel-{index}")
    monkeypatch.setenv("UNRELATED_SERVICE_TOKEN", "other-secret-sentinel")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unwanted.example")
    claude = providers.cli_environment(ProviderConfig("solver", "claude-code", auth="subscription"))
    codex = providers.cli_environment(ProviderConfig("solver", "codex", auth="subscription"))
    assert set(SECRET_NAMES) & claude.keys() == {"CLAUDE_CODE_OAUTH_TOKEN"}
    assert not set(SECRET_NAMES) & codex.keys()
    for env in (claude, codex):
        assert "UNRELATED_SERVICE_TOKEN" not in env and "ANTHROPIC_BASE_URL" not in env


def test_codex_api_key_is_mapped_without_leaking_picker_credentials(monkeypatch):
    monkeypatch.setenv("SCOUT_SOLVER_API_KEY", "private-solver-key")
    monkeypatch.setenv("GROQ_API_KEY", "private-picker-key")
    env = providers.cli_environment(ProviderConfig("solver", "codex", auth="api-key"))
    assert env["CODEX_API_KEY"] == "private-solver-key"
    assert "SCOUT_SOLVER_API_KEY" not in env and "GROQ_API_KEY" not in env


def test_codex_subscription_cannot_be_used_as_generic_api_auth(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", Mock(return_value="codex"))
    monkeypatch.setenv("RUNNER_ENVIRONMENT", "github-hosted")
    with pytest.raises(ValueError, match="trusted runner"):
        providers.check_provider(ProviderConfig("solver", "codex", auth="subscription"))


def test_openai_compatible_transport_enforces_schema_and_does_not_follow_redirects(monkeypatch):
    monkeypatch.setenv("SCOUT_SOLVER_API_KEY", "private-api-key")
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": '{"answer":"ok"}'}}],
                                  "usage": {"prompt_tokens": 12, "completion_tokens": 4}}
    post = Mock(return_value=response)
    monkeypatch.setattr(providers.requests, "post", post)
    backend = providers.JsonBackend(ProviderConfig("solver", "openai-compatible", "chosen-model", "https://model.example/v1"))
    assert backend.request("Question", SMALL_SCHEMA) == {"answer": "ok"}
    args, kwargs = post.call_args
    assert args == ("https://model.example/v1/chat/completions",)
    assert kwargs["headers"]["Authorization"] == "Bearer private-api-key"
    assert kwargs["allow_redirects"] is False
    assert kwargs["json"]["response_format"]["json_schema"]["schema"]["additionalProperties"] is False
    assert backend.usage == {"prompt_tokens": 12, "completion_tokens": 4}


def test_anthropic_transport_uses_api_key_and_forced_structured_response(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-api-key")
    response = Mock(status_code=200)
    response.json.return_value = {"stop_reason": "tool_use", "content": [
        {"type": "tool_use", "name": "scout_response", "input": {"answer": "ok"}}]}
    post = Mock(return_value=response)
    monkeypatch.setattr(providers.requests, "post", post)
    backend = providers.JsonBackend(ProviderConfig("picker", "anthropic", "chosen-model", "https://api.anthropic.com/v1"))
    assert backend.request("Question", SMALL_SCHEMA) == {"answer": "ok"}
    assert post.call_args.args == ("https://api.anthropic.com/v1/messages",)
    assert post.call_args.kwargs["headers"]["x-api-key"] == "private-api-key"
    assert post.call_args.kwargs["json"]["tool_choice"]["name"] == "scout_response"


@pytest.mark.parametrize("status", [301, 401, 429, 500])
def test_http_errors_do_not_echo_credentials_or_response_bodies(monkeypatch, status):
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-key")
    response = Mock(status_code=status, text="private-api-key in an upstream error")
    monkeypatch.setattr(providers.requests, "post", Mock(return_value=response))
    backend = providers.JsonBackend(ProviderConfig("picker", "openai", "chosen-model", "https://api.openai.com/v1"))
    with pytest.raises(RuntimeError) as error:
        backend.request("Question", SMALL_SCHEMA)
    assert "private-api-key" not in str(error.value) and str(status) in str(error.value)


@pytest.mark.parametrize("message", [
    {"finish_reason": "length", "message": {"content": '{"answer":"partial"}'}},
    {"finish_reason": "stop", "message": {"content": "not JSON"}},
    {"finish_reason": "stop", "message": {"refusal": "cannot comply", "content": None}},
])
def test_reject_incomplete_or_refused_api_output(monkeypatch, message):
    monkeypatch.setenv("OPENAI_API_KEY", "private-api-key")
    response = Mock(status_code=200)
    response.json.return_value = {"choices": [message]}
    monkeypatch.setattr(providers.requests, "post", Mock(return_value=response))
    backend = providers.JsonBackend(ProviderConfig("picker", "openai", "chosen-model", "https://api.openai.com/v1"))
    with pytest.raises(RuntimeError, match="incomplete or invalid"):
        backend.request("Question", SMALL_SCHEMA)


def test_schema_validation_rejects_bad_types_and_does_not_echo_output():
    with pytest.raises(ValueError) as error:
        providers.validate_output({"answer": {"secret": "private-api-key"}}, SMALL_SCHEMA)
    assert "private-api-key" not in str(error.value)


def test_codex_uses_saved_login_in_isolated_toolless_read_only_process(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", Mock(return_value="codex"))
    monkeypatch.setenv("CODEX_HOME", "saved-cli-home")
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-picker-key")
    events = [
        {"type": "thread.started", "thread_id": "test-thread"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": '{"answer":"ok"}'}},
        {"type": "turn.completed", "usage": {"input_tokens": 9, "output_tokens": 3}},
    ]
    captured = {}

    def run(command, **kwargs):
        captured.update(command=command, **kwargs)
        assert Path(command[command.index("--output-schema") + 1]).is_file()
        return SimpleNamespace(returncode=0, stdout="\n".join(map(json.dumps, events)))

    monkeypatch.setattr(providers.subprocess, "run", run)
    backend = providers.JsonBackend(ProviderConfig("picker", "codex", auth="subscription"))
    assert backend.request("Question", SMALL_SCHEMA) == {"answer": "ok"}
    command = captured["command"]
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert "--ignore-user-config" in command and "--ignore-rules" in command
    assert "shell_tool" in command and 'web_search="disabled"' in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert captured["input"] == "Question" and captured["env"]["CODEX_HOME"] == "saved-cli-home"
    assert "OPENAI_API_KEY" not in captured["env"] and "CODEX_API_KEY" not in captured["env"]
    assert not Path(captured["cwd"]).exists()  # temporary model files are removed


def test_claude_picker_has_no_tools_or_persistent_session(monkeypatch):
    monkeypatch.setattr(providers.shutil, "which", Mock(return_value="claude"))
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout='{"structured_output":{"answer":"ok"}}'))
    monkeypatch.setattr(providers.subprocess, "run", run)
    assert providers.StructuredModel(ProviderConfig("picker", "claude-code"), SMALL_SCHEMA).invoke("Question") == {"answer": "ok"}
    command = run.call_args.args[0]
    assert command[command.index("--tools") + 1] == ""
    assert "--no-session-persistence" in command and "--restricted" in command
    assert "--safe-mode" in command and "--bare" not in command  # bare mode disables saved subscription login


@pytest.mark.parametrize("path", ["../outside", "/absolute", "C:/outside", r"..\outside", ".git/config",
                                      ".codex/config.toml", ".claude/settings.json", ".env", ".env.production",
                                      ".git./config", "folder /file", "NUL", "COM1.txt"])
def test_repository_tools_reject_escape_and_credential_paths(tmp_path, path):
    tools = RepositoryTools(tmp_path)
    with pytest.raises(ValueError):
        tools.call("write_file", path, "forbidden")


def test_repository_tools_support_bounded_read_search_write_and_delete(tmp_path):
    tools = RepositoryTools(tmp_path)
    tools.call("write_file", "src/file.py", "first\nneedle\nthird\n")
    assert tools.call("read_file", "src/file.py", start_line=2)["text"] == "needle\nthird\n"
    assert tools.call("search_files", ".", "NEEDLE")["matches"][0]["line"] == 2
    assert tools.call("list_files")["files"] == ["src/file.py"]
    tools.call("delete_file", "src/file.py")
    assert not (tmp_path / "src/file.py").exists()
    with pytest.raises(ValueError):
        tools.call("run_command", ".", "echo unsafe")


def test_repository_tools_reject_symlinks(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "repo"
    root.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires privileges on this platform")
    with pytest.raises(ValueError, match="Symlinks"):
        RepositoryTools(root).call("write_file", "link/file", "forbidden")


def test_solver_edits_and_continues_after_validation_feedback_without_running_commands(monkeypatch, tmp_path):
    (tmp_path / "calc.py").write_text("return wrong\n")
    brief = {"answer": "fixed"}
    answers = [
        {"calls": [{"tool": "read_file", "path": "calc.py", "text": "", "start_line": 1}], "brief": None},
        {"calls": [{"tool": "write_file", "path": "calc.py", "text": "return correct\n", "start_line": 1}], "brief": None},
        {"calls": [], "brief": brief},
        {"calls": [{"tool": "write_file", "path": "test_calc.py", "text": "assert correct\n", "start_line": 1}], "brief": None},
        {"calls": [], "brief": brief},
    ]
    request = Mock(side_effect=answers)
    monkeypatch.setattr(providers.JsonBackend, "request", request)
    monkeypatch.setattr(providers.subprocess, "run", Mock(side_effect=AssertionError("no host commands")))
    config = ProviderConfig("solver", "openai", "chosen-model", "https://api.openai.com/v1")
    first = providers.solve_with_backend(config, "Fix calc", tmp_path, SMALL_SCHEMA)
    assert (tmp_path / "calc.py").read_text() == "return correct\n"
    second = providers.solve_with_backend(config, "Validation failed: add regression test", tmp_path, SMALL_SCHEMA, first["session_id"])
    assert (tmp_path / "test_calc.py").read_text() == "assert correct\n"
    assert second["structured_output"] == brief and second["total_cost_usd"] is None
    assert "Validation failed" in request.call_args.args[0] and "return wrong" in request.call_args.args[0]


def test_solver_bounds_work_and_rejects_simultaneous_finish_and_edits(monkeypatch, tmp_path):
    call = {"calls": [{"tool": "list_files", "path": ".", "text": "", "start_line": 1}], "brief": None}
    monkeypatch.setattr(providers.JsonBackend, "request", Mock(return_value=call))
    config = ProviderConfig("solver", "codex")
    with pytest.raises(RuntimeError, match="2-turn"):
        providers.solve_with_backend(config, "Fix", tmp_path, SMALL_SCHEMA, max_turns=2)
    call["brief"] = {"answer": "done"}
    with pytest.raises(ValueError, match="finish file operations"):
        providers.solve_with_backend(config, "Fix", tmp_path, SMALL_SCHEMA)


def test_picker_and_solver_dispatch_independently(monkeypatch, tmp_path):
    picker = ProviderConfig("picker", "codex")
    solver = ProviderConfig("solver", "anthropic", "chosen-model", "https://api.anthropic.com/v1")
    monkeypatch.setattr(scout, "PICKER", picker)
    monkeypatch.setattr(scout, "SOLVER", solver)
    factory = Mock(return_value="selected-model")
    solve = Mock(return_value={"structured_output": {}})
    monkeypatch.setattr(providers, "StructuredModel", factory)
    monkeypatch.setattr(providers, "solve_with_backend", solve)
    assert scout.llm(SMALL_SCHEMA) == "selected-model"
    factory.assert_called_once_with(picker, SMALL_SCHEMA)
    scout.run_solver("Fix", str(tmp_path))
    solve.assert_called_once_with(solver, "Fix", str(tmp_path), scout.BRIEF_SCHEMA, None)


def test_reports_handle_unreported_cost_without_claiming_zero():
    from test_scout_recovery import result_state

    report = result_state()
    report["result"].update(cost=None, provider="codex")
    text = "\n".join(scout.report_lines(report))
    assert "Provider: codex" in text and "Usage cost: not reported" in text
    html = scout.email_html.render(report, 10)
    assert "n/a" in html and "Claude est." not in html


def test_openai_project_and_service_account_keys_are_detected_even_when_not_configured():
    for prefix in ("sk-proj-", "sk-svcacct-", "sk-"):
        assert "openai key" in scout.scan_secrets(prefix + "a_b-c" * 12)
