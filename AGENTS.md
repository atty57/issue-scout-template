# AGENTS.md

Guidance for coding agents (Claude Code, Codex and others) and contributors.

## What this repository is

The Issue Scout engine. The planned desktop app creates a private copy of this repository for each user, so they get the same Issue Scout workflow the maintainer runs, without manual setup. Write docs and setup flows for those app users, not for developers forking a template.

Pipeline (LangGraph, in `scout.py`): `discover → shortlist → gate → pick → revalidate → solve → notify`.

| File | Role |
|---|---|
| `scout.py` | Pipeline and CLI entry point (`python -u scout.py`) |
| `settings.py` | Loads and validates `profile.json` at import |
| `prompts.py`, `ranking.py` | Picker and solver prompts; candidate scoring |
| `model_providers.py`, `provider_config.py` | The six picker/solver backends and their credentials |
| `repository_tools.py`, `workloads.py` | Bounded file tools for solvers; Docker validation runtimes |
| `github_api.py` | GitHub REST access |
| `email_delivery.py`, `email_html.py` | Relay client and email body |
| `email_relay.py` | Operator-side relay service; deployed separately, holds the Resend key |
| `redaction.py` | Secret filtering for reports, patches and traces |
| `profile.json` | Shipped placeholder with `"setup_required": true`; `profile.example.json` is a filled-in fictional example |
| `.github/workflows/daily.yml` | Each user's workflow; manual dispatch only by default |

## Commands

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider   # offline: no API keys, network or Docker; random order
```

Tests load `profile.example.json` (set in `conftest.py`), never a personal profile.

## Rules

- **Never commit to `main`.** Work on a branch; merging into `main` is the maintainer's call.
- **Never commit secrets.** Keys belong in GitHub Secrets or on the relay host; example env files keep their values empty. The Resend key never goes in this repository, the app, profiles or workflow secrets.
- **Never commit `plan.md` or `docs/`.** They are the maintainer's private local files.
- Don't change the maintainer's private personal Scout repository.
- Scout must never claim issues, comment or open pull requests on target projects.
- Change behaviour test-first, testing through public interfaces, and keep tests offline.
- A run must stop before any paid call when setup is incomplete or configuration is invalid. Never fall back to another provider or billing method after an error.

## Milestones

**After each milestone, update this section, the README's "What's next", and `CLAUDE.md` if its notes change.**

| # | Milestone | Status |
|---|---|---|
| 1 | Engine setup: independent providers, recipient-only email client and relay code, setup guard, profile validation, manual-only workflow, MIT license, public repository | Done |
| 2 | Test run on a fresh private copy with a different profile and providers | Next |
| 3 | Live email delivery: domain verification, relay hosting, real send and failure recovery | Not started |
| 4 | Release details: `scout.json` version manifest, release notes, security policy | Not started |
| 5 | Desktop app MVP (separate repository): GitHub connection, private copy, profile/email/model setup, schedule, Run now, status, reports | Not started |
| 6 | Updates and installers: app auto-update, engine updates that keep user data, signing | Not started |

Open decisions: relay host and URL; recipient email verification; GitHub App or OAuth (editing the schedule needs the `workflow` permission); subscription logins on hosted runners; the engine update contract; installer platforms.
