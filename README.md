# Issue Scout

Issue Scout finds an open-source issue that suits you, drafts a fix, tests it, and sends you the result to review. It never claims issues, comments or opens pull requests for you: you review the work and submit it yourself.

> **Status:** the Issue Scout desktop app is in development and not released yet. Until it ships, you can [set Scout up by hand](#manual-setup). See [What's next](#whats-next) for the roadmap.

## How it works

Scout runs in your own private GitHub repository on GitHub Actions, with your GitHub account and the AI models you choose. Each run:

1. **Finds** open issues that match your languages, skills and preferred labels, plus projects on your watchlist.
2. **Checks** that each issue is still available (not claimed, no open pull request) and that the project allows AI-assisted contributions.
3. **Picks** the strongest issue with your chosen model, or none if nothing qualifies.
4. **Fixes and tests** it in a throwaway container. A code fix must include a regression test that fails before the fix and passes after it.
5. **Delivers** a review brief and patch by email from Issue Scout's noreply address if you turn email on, and always saves them with the workflow run.

You then claim the issue, review the change and open the pull request.

Scout handles code in Python, JavaScript, TypeScript, C#, C, Go, Rust, Java and C++, and documentation fixes in any project.

## With the app

Once released, the app sets everything up without a terminal:

1. Connect your GitHub account. The app creates your private Scout repository.
2. Fill in your profile: skills, languages, the kind of work you want and, if you want reports by email, your address. The app emails you a code to confirm it.
3. Choose a model for picking issues and one for writing fixes (the same provider or different ones), and enter only the credentials those need.
4. Choose a daily start time, or press **Run now**.
5. Read drafts by email, and see run status, reports and patches in the app.

## What you need and who pays

| Item | Who pays |
|---|---|
| A GitHub account. Runs use your GitHub Actions minutes and storage. | You |
| An AI model: an API key for Groq, OpenAI, Anthropic or an OpenAI-compatible service, or a supported Claude Code or Codex subscription | You |
| Email delivery from Issue Scout's noreply address | The Issue Scout operator, within a shared quota. You only give and confirm your address. |

The report shows a run's model cost when the provider reports one; `n/a` means unknown, not free. Codex on GitHub-hosted runners needs an API key; its subscription login works only on a machine you control.

## Privacy and safety

- Your profile, history and results stay in your own private repository.
- Model credentials are stored as encrypted repository secrets. Each model process receives only the credential it needs, and known secrets are removed from reports, patches and logs.
- Project setup and tests run in throwaway containers with no credentials and a read-only copy of the source.
- Scout never posts to the projects it scouts. You review and publish every contribution.
- Email goes through a relay that verifies your workflow's GitHub identity, and only to an address you confirmed with a code. You never handle a mail password or sending key.

## Manual setup

Until the app is released:

1. On this repository's GitHub page, choose **Use this template → Create a new repository**, and make it **private**.
2. In your new repository, edit [`profile.json`](profile.json) with your summary, skills, languages, labels and watchlist, then delete the `"setup_required": true` line. Until you do, Scout stops with `SETUP_REQUIRED` before making any calls. [`profile.example.json`](profile.example.json) shows a filled-in profile.
3. Add credentials for your models under **Settings → Secrets and variables → Actions → Secrets**:

   | Secret | When it's needed |
   |---|---|
   | `GROQ_API_KEY` | Groq is selected. By default, Groq picks issues. |
   | `CLAUDE_CODE_OAUTH_TOKEN` | Claude Code with a subscription. By default, Claude Code writes fixes. |
   | `ANTHROPIC_API_KEY` | The Anthropic API, or Claude Code with an API key. |
   | `OPENAI_API_KEY` / `CODEX_API_KEY` | The OpenAI API / Codex with an API key. |
   | `SCOUT_API_KEY` | A custom OpenAI-compatible endpoint. |
   | `SCOUT_PICKER_API_KEY` / `SCOUT_SOLVER_API_KEY` | Optional: a separate key for one stage. |
   | `LANGSMITH_API_KEY` | Optional: LangSmith tracing. |

   To use other models, set the `SCOUT_PICKER_PROVIDER` and `SCOUT_SOLVER_PROVIDER` repository **Variables** to `groq`, `openai`, `anthropic`, `openai-compatible`, `claude-code` or `codex`. `openai`, `anthropic` and `openai-compatible` also need a model name in `SCOUT_PICKER_MODEL` / `SCOUT_SOLVER_MODEL`; for `openai-compatible`, set your endpoint in `SCOUT_PICKER_BASE_URL` / `SCOUT_SOLVER_BASE_URL`.
4. Open **Actions → issue-scout → Run workflow**.
5. Download the run's `scout-result-…` artifact (kept for 14 days) for the report and patch. Shared email delivery isn't live yet. Once it is, turn it on in the Issue Scout app, which confirms your address with a code. An address in `profile.json` never sends email unless `email_enabled` is `true`.

### Daily schedule

The workflow runs only when you start it. To run it daily, add a `schedule` under `on:` in [`.github/workflows/daily.yml`](.github/workflows/daily.yml) with your start time and [IANA time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones); GitHub handles daylight saving time:

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "0 9 * * *"             # Scout starts at 09:00 every day
      timezone: "America/New_York"   # keep this nested under the cron line
```

GitHub may start scheduled runs late when it's busy, and email arrives after the run finishes.

To run Scout on your own computer instead, use Linux or WSL with Python 3.12 and Docker: run `python -m pip install -r requirements.txt` (plus the Claude Code or Codex CLI if you use one), set the same credentials as environment variables, and run `python -u scout.py`.

## What's next

**Done**

- Choose the picker and solver models independently, from six providers.
- Email needs only your address: no mail password or sending key.
- Safe defaults for new copies: Scout stops until the profile is set up, rejects unknown profile fields, and runs only when started.
- Public release under the MIT license.

**Coming up, in order**

- **Test run on a fresh copy:** set up a new private copy with a different profile and models, and run it end to end.
- **Live email delivery:** confirm a real code, report, resend and quota-limited failure end to end. The relay is deployed, and the code, workflow and desktop setup are done.
- **Release details:** a version manifest, release notes and a security policy.
- **Desktop app:** GitHub connection, private repository creation, profile, email and model setup, schedule, Run now, status and reports.
- **Updates and installers:** app auto-update, engine updates that keep your profile and history, and signed installers.

## Limitations

- Each run attempts at most one issue and may pick none when evidence or confidence is low.
- Model access and usage limits depend on your own API account or subscription.
- Code fixes are limited to what Scout can test in Linux containers: Python 3.12, Node.js 22–26, .NET 8–10 and GCC 14.

## Contributing

Questions, bug reports and suggestions are welcome as [issues](https://github.com/atty57/issue-scout-template/issues). For code changes, run the offline tests (no API keys needed):

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
```

## License

[MIT](LICENSE). Maintained by [@atty57](https://github.com/atty57).
