# Picker and solver providers

Choose models independently for policy checks / issue selection (the picker) and
fix generation (the solver). Changing providers does not change discovery,
eligibility checks, Docker validation, regression verification, or delivery.
Scout does not switch providers or start API billing as a fallback after an error.

| Provider value | Authentication | CLI installation |
|---|---|---|
| `groq` | `GROQ_API_KEY` | None |
| `openai` | `OPENAI_API_KEY` | None |
| `anthropic` | `ANTHROPIC_API_KEY` | None |
| `openai-compatible` | `SCOUT_API_KEY`, falling back to `OPENAI_API_KEY` | None |
| `claude-code` | Official Claude Code saved login, `CLAUDE_CODE_OAUTH_TOKEN`, or `ANTHROPIC_API_KEY` | `npm install -g @anthropic-ai/claude-code` |
| `codex` | Official Codex saved login or `CODEX_API_KEY` | `npm install -g @openai/codex` |

All six backends work as either picker or solver. Keep CLI backends current:
Scout uses Claude Code restricted mode and Codex's configuration-isolation flags.
API models must support structured JSON output; Anthropic uses a forced response
tool. Unsupported models fail with a clear error and do not fall back to another
provider. OpenAI-compatible means the Chat Completions API with JSON Schema
support, not every proprietary model endpoint.

## Settings

Use environment variables locally, or repository **Variables** in GitHub Actions.
Put API keys in repository **Secrets**, never Variables or `profile.json`.
`.env.example` lists names; Scout does not automatically load `.env` files.

| Setting | Default / behavior |
|---|---|
| `SCOUT_PICKER_PROVIDER` | `groq` |
| `SCOUT_SOLVER_PROVIDER` | `claude-code` |
| `SCOUT_PICKER_MODEL` | Groq: `SCOUT_MODEL`, then `openai/gpt-oss-120b`; otherwise set explicitly for API backends |
| `SCOUT_SOLVER_MODEL` | Set explicitly for API backends except Groq; CLI backends may use their built-in default |
| `SCOUT_PICKER_BASE_URL`, `SCOUT_SOLVER_BASE_URL` | Official provider URL; set to your proxy's versioned API root for a compatible endpoint |
| `SCOUT_PICKER_API_KEY`, `SCOUT_SOLVER_API_KEY` | Optional per-stage key overriding the provider's usual key |
| `SCOUT_PICKER_AUTH`, `SCOUT_SOLVER_AUTH` | `auto`, `api-key`, or `subscription`; subscription is available only with official CLI backends |
| `SCOUT_PROFILE` | `profile.json` beside `settings.py`; relative paths resolve from this repository |

Custom endpoints must use HTTPS. HTTP is accepted only on `localhost`,
`127.0.0.1`, or `::1` for local services. Embedded credentials, query strings,
fragments, and redirects are rejected. A custom endpoint receives the key you
configure for that stage, so use a key issued for that endpoint.

## Use your Codex subscription for both stages

On your own trusted machine, install and sign in to the official CLI:

```bash
npm install -g @openai/codex
codex login
export SCOUT_PICKER_PROVIDER=codex
export SCOUT_SOLVER_PROVIDER=codex
export SCOUT_PICKER_AUTH=subscription
export SCOUT_SOLVER_AUTH=subscription
python -u scout.py
```

No Groq or Anthropic key is required. Setting `subscription` prevents unrelated
API keys in your environment from selecting API billing. Model access and rate
limits depend on your account. Authentication stays with Codex; Scout does not
implement an OAuth flow or read, export, or upload its authentication cache.

Use `codex login --device-auth` if your environment supports device-code login.
Follow the [official authentication instructions](https://developers.openai.com/codex/auth).
Do not commit CLI authentication files. Saved login must be available to the user
account running Scout, including when you configure a trusted self-hosted runner.

## Use Claude Code subscription login

```bash
export SCOUT_PICKER_PROVIDER=claude-code
export SCOUT_SOLVER_PROVIDER=claude-code
export SCOUT_PICKER_AUTH=subscription
export SCOUT_SOLVER_AUTH=subscription
claude auth login
python -u scout.py
```

Use your own official CLI sign-in. For your own Claude Code workflow, a token
created with `claude setup-token` can be supplied as `CLAUDE_CODE_OAUTH_TOKEN`.
Scout does not provide third-party Claude OAuth sign-in or treat subscription
tokens as API keys. See [Anthropic's authentication and credential rules](https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use).

In `auto` mode, Claude Code uses an explicitly supplied subscription token first,
then a supplied Anthropic API key, then its saved login. Codex uses an explicitly
supplied `CODEX_API_KEY` / per-stage key or its saved login. Choose `subscription`
or `api-key` explicitly when billing choice matters.

## Bring your own API keys

For example, use OpenAI for selection and Anthropic for solving:

```bash
export SCOUT_PICKER_PROVIDER=openai
export SCOUT_PICKER_MODEL='<model available to your API account>'
export SCOUT_SOLVER_PROVIDER=anthropic
export SCOUT_SOLVER_MODEL='<model available to your API account>'
# Set OPENAI_API_KEY and ANTHROPIC_API_KEY securely in your environment.
python -u scout.py
```

For another compatible service, select `openai-compatible`, set the stage's
`BASE_URL` and `MODEL`, and provide its API key using the stage's `API_KEY` secret.
The solver exposes only file listing, bounded text reads, literal search, text
writes, and file deletion. It cannot execute commands. The same protocol powers
Codex solving, with batched file requests rather than unrestricted CLI tools.
There is a 40-turn / 30-minute budget per solve or validation-repair pass, a
500,000-character context limit, and a 256 KB limit per file. Oversized or invalid
responses stop the attempt rather than silently dropping context.

## GitHub Actions

The existing workflow reads `SCOUT_*` settings from repository Variables and keys
from Secrets. Empty model and endpoint Variables use the defaults above. Only
selected CLI backends are installed. Configure and commit your own `profile.json`
before dispatching a workflow. Email delivery uses your profile's `email` field
and the operator's relay; no user mail password is required. See [email delivery](email-delivery.md).

For Codex on GitHub-hosted runners, use API-key authentication: set the selected
stage's provider to `codex`, its auth to `api-key`, and supply `CODEX_API_KEY`
(or a stage-specific key). Do not upload subscription authentication caches into
public-repository workflows. OpenAI recommends API keys for automation; maintaining
account authentication on trusted runners is an advanced, separate setup.
See [non-interactive Codex authentication](https://developers.openai.com/codex/noninteractive#authenticate-in-automation).

## Results and usage

Reports identify the solver provider. Dollar estimates are displayed only when
the backend supplies one; `n/a` / `not reported` does not mean a free run. API and
Codex responses retain reported token usage in the solver result. Each provider
uses your own quota or billing account. Errors never expose upstream response
bodies, CLI output, or keys in the report.
