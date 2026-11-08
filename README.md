# issue-scout

Find a GitHub issue, draft a validated fix, and email a patch for human review.

issue-scout searches for focused open-source contributions that fit your profile. It selects at most one issue per run, uses your chosen model provider to draft a code or documentation change, validates it, and emails you the patch with a review brief. You claim the issue, review the work, and submit the pull request yourself.

Picker and solver providers are independent: use Claude Code or Codex with their supported CLI login, or bring API keys for Anthropic, OpenAI, Groq, or an OpenAI-compatible endpoint. The defaults remain Groq for selection and Claude Code for solving. See [provider configuration](docs/providers.md) for setup and subscription limitations.

Supported code workloads are Python, C, C#, JavaScript and TypeScript. Documentation-only issues can qualify independently of a repository's code language. AI/ML experience improves ranking without restricting eligibility to that topic.

## Table of Contents

- [Background](#background)
- [Install](#install)
- [Usage](#usage)
- [Configuration](#configuration)
- [How It Works](#how-it-works)
- [Validation](#validation)
- [Reports and Recovery](#reports-and-recovery)
- [Security](#security)
- [Limitations](#limitations)
- [Maintainer](#maintainer)
- [Contributing](#contributing)
- [License](#license)

## Background

The project makes regular open-source contributions easier by handling issue discovery, suitability checks and an initial fix before human review. Availability checks and repository AI policies help avoid claiming someone else's work or preparing an unsuitable contribution.

[LangGraph](https://github.com/langchain-ai/langgraph) coordinates the workflow. Configurable picker and solver providers handle evaluation and editing; Docker isolates repository setup and tests. A Resend relay delivers drafts from `noreply@scouts.email`; users supply only their destination email. Optional LangSmith tracing helps inspect a run.

## Install

### Dependencies

The GitHub Actions workflow installs the scout on Ubuntu with Python 3.12. For a full local run, prepare:

- Python 3.12 and pip.
- Git; Node.js and npm if using a CLI backend.
- Docker with a running daemon and Linux-container support for code validation and optional documentation builds.
- Credentials for your chosen providers. Subscription use requires the corresponding official CLI login; API use requires your own key.
- Your destination email if you want email delivery through the configured service.

Use a Linux environment, such as Ubuntu or WSL, for the local commands below.

```bash
git clone https://github.com/atty57/issue-scout.git
cd issue-scout
python -m pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code
```

Personalize `profile.json` before running, then delete its `"setup_required": true` line; until then Scout stops with `SETUP_REQUIRED` before making any calls. [profile.example.json](profile.example.json) shows a filled-in profile. Unknown fields are rejected, and a blank `summary` is generated from your skills. Alternatively, point `SCOUT_PROFILE` at another complete profile. For API-only backends, skip the npm installation; for Codex, install `@openai/codex` instead. See [provider configuration](docs/providers.md).

For GitHub Actions, copy the repository into your own account; the workflow installs dependencies and only the selected CLIs automatically.

### Credentials

Add these as repository secrets under **Settings → Secrets and variables → Actions**, or set them as environment variables for a local run.

| Name | Purpose |
|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | For Claude Code subscription authentication in your own workflow. Local runs can use the CLI's saved login. |
| `GROQ_API_KEY` | Required only when Groq is selected. Obtain a key from [Groq Console](https://console.groq.com). |
| `ANTHROPIC_API_KEY` | For the Anthropic API backend or Claude Code with API-key authentication. |
| `OPENAI_API_KEY` / `CODEX_API_KEY` | For OpenAI API / Codex API-key authentication respectively. Codex subscription runs use its saved official CLI login. |
| `SCOUT_API_KEY` | Key for a custom OpenAI-compatible endpoint. |
| `SCOUT_PICKER_API_KEY` / `SCOUT_SOLVER_API_KEY` | Optional independent API keys; override the selected provider's usual key for that stage. |
| `LANGSMITH_API_KEY` | Optional tracing key from [LangSmith](https://smith.langchain.com). Adding this key enables tracing. |
| `GH_TOKEN` | The workflow uses the built-in `github.token`. For local authenticated requests or a workflow search returning 403, use a fine-grained, read-only personal access token. If stored as a secret, update the workflow's `GH_TOKEN` mapping to use it. |

For an EU-hosted LangSmith workspace, also set `LANGSMITH_ENDPOINT=https://eu.api.smith.langchain.com` in your local environment or the workflow's environment block.

For email, add `"email": "you@example.com"` to your profile, or set the repository Variable `SCOUT_RECIPIENT_EMAIL`. Users do not provide a mail password or a Resend key. The domain owner configures and hosts the sending relay separately; see [email delivery setup](docs/email-delivery.md).

## Usage

### GitHub Actions

1. Configure the [credentials](#credentials) in your repository.
2. Edit [profile.json](profile.json) with your experience, skills, watchlist and scouting preferences, and remove `"setup_required": true`.
3. Open **Actions → issue-scout → Run workflow** to run it manually.
4. Review the email or download the run's report artifact. Claim the selected issue before preparing your pull request.

#### Schedule

The [workflow](.github/workflows/daily.yml) runs only when started manually. To run it daily, add a `schedule` under `on:` with your start time and [IANA time zone](https://en.wikipedia.org/wiki/List_of_tz_database_time_zones); GitHub handles daylight saving time:

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: "0 9 * * *"             # Scout starts at 09:00 every day
      timezone: "America/New_York"   # keep this nested under the cron line
```

GitHub may start scheduled runs late during busy periods, and the email arrives after the run finishes. The workflow commits updated issue history to `state/seen.json` and retry-scan progress to `state/discovery.json` in your own repository.

### CLI

After setting your credentials and editing the profile, run:

```bash
python -u scout.py
```

When no destination email or relay URL is configured, Scout saves the report and prints an email preview. The shared relay currently authenticates GitHub Actions workflows; local runs with a configured relay preserve their reports if workflow identity is unavailable. Discovery, model calls and solve/validation still run; preview mode is not an offline test.

A run can end without selecting an issue when the candidate pool is exhausted or evidence is insufficient.

### Tracing

Set `LANGSMITH_API_KEY` to trace the graph, picker calls, solver turns, and sandbox test runs. Secrets are scrubbed before upload. The default tracing project is `issue-scout`.

## Configuration

Edit [profile.json](profile.json) to personalize discovery and ranking.

| Field | What it controls |
|---|---|
| `summary`, `skills`, `skill_aliases` | Your experience and matching terms. Aliases support variants such as `retrieval` and `retrievers`; word boundaries prevent `rag` from matching `storage`. |
| `email` | Destination for your draft. The verified sender is managed by the domain owner. |
| `languages` | Enabled code ecosystems. |
| `work_types` | `code`, `documentation`, or both. |
| `labels`, `documentation_labels` | Code search labels and language-independent documentation search labels. |
| `watchlist`, `merged_in` | Repositories to search for updated backlog and repositories where you have already contributed. |
| `days` | Fresh-issue discovery window; the included profile uses 3 days. |
| `scouting` | Search budgets, availability checks, eligibility thresholds and retry rules. |

The included profile enables general issues in every supported language and documentation. Topic matches, maintainers' responses, reproducers and repository familiarity improve ranking; they are preferences rather than mandatory eligibility requirements.

### Scouting Settings

| Setting | Included value | Behavior |
|---|---|---|
| `general_issues` | `true` | Allows supported code or focused documentation work without topic keywords. The fallback default is `false` if omitted. |
| `min_stars` | `200` | Minimum repository stars, including watchlist and retry candidates. An unavailable star count also blocks selection. |
| `updated_days` | `30` | Updated-issue window for help-wanted, documentation and watchlist searches. |
| `search_pages` | `2` | Maximum pages per search. |
| `candidate_limit` | `30` | Maximum ranked candidate pool. |
| `shortlist_size` | `5` | Available choices per picker batch; accepts values from 1 to 8. |
| `max_comment_pages`, `max_timeline_pages` | `5` each | Availability-check budgets. Incomplete checks do not qualify an issue. |
| `claim_days` | `14` | Claim expiry unless renewed; a later withdrawal clears that author's claim. |
| `retry_days`, `max_attempts` | `7`, `2` | Failed-attempt cooldown and total attempt limit. |
| `min_score`, `min_confidence` | `6`, `0.7` | Minimum heuristic score and model selection confidence. |
| `exclude_labels` | `["good first issue"]` | Labels excluded from discovery and retries. |

Count, day-window and star settings must be positive integers. Current assignments and open PR references block selection. Manual links whose targets cannot be verified are skipped conservatively.

## How It Works

```text
discover → shortlist → gate → pick → revalidate → solve → notify
```

| Step | What happens |
|---|---|
| discover | Searches fresh bugs by language, updated help-wanted issues, documentation labels and watchlist backlog. Paginated duplicates merge language evidence. |
| shortlist | Scores topic, language, documentation, reproducer, maintainer, familiarity, scope, freshness and activity evidence. Interleaves categories and repositories before bounding the pool. |
| gate | Scans the pool for available candidates meeting repository and score thresholds. Reads claims, PR references, manifests, documentation and AI-contribution policies; caches repository checks within the run. Unsupported code toolchains can still permit documentation work. |
| pick | Gives Groq the issue, discussion, verified PR merge states, ranking evidence and repository constraints. Rejections must name unmet requirements; already-fixed verdicts require supplied resolution evidence. Unsupported verdicts get one correction attempt. A valid abstention advances to the next batch; unavailable models or persistently invalid verdicts stop before solving. |
| revalidate | Refreshes the selected issue and discussion immediately before solving. Changes to content, availability or referenced PRs send the picker back to the remaining candidates. |
| solve | Claude Code drafts the change and validation plan. Code runs in the selected Docker runtime; documentation gets static checks and an optional build. Validation failures go back to Claude for up to two more tries. Code regression tests also run on the unfixed checkout. |
| notify | Saves the report, patch and outcome, then emails a plain-text brief with an HTML alternative and any safe patch. Retries transient delivery failures and preserves recovery files. |

Selection needs a clear problem, expected outcome, focused scope, enabled workload and credible local validation. Broad features, unresolved design decisions and validation requiring unavailable hardware or credentials do not qualify. External calls that can be mocked locally are acceptable; explicit maintainer approval is not required.

Discussion excerpts prioritize recent replies, earlier maintainer comments, reproducers and resolution updates, marking omitted comments. The picker checks later replies for failed fixes or reopening. An already-fixed quote must match supplied body or discussion text, allowing whitespace differences and one matching pair of enclosing quotation marks or backticks. Logs distinguish absent evidence, quotes that do not confirm a fix, and quotes saying the issue remains unresolved. A closed PR, an old affected version or model memory does not establish a fix.

## Validation

### Supported Runtimes

| Workload | Validation runtime |
|---|---|
| Python | `python312`: Python 3.12, repository setup and targeted pytest checks. |
| JavaScript / TypeScript | `node22`, `node24`, `node26`: repository package manager, lockfile and test scripts. |
| C# | `dotnet8`, `dotnet9`, `dotnet10`: Linux-compatible SDK targets and repository restore/build/test commands. |
| C | `c_gcc14`: GCC 14 and repository tests; extra CMake/Ninja tools can be installed inside the container. |
| Documentation | `documentation`: static text checks; another enabled runtime can run the repository's docs build. |

### Code Changes

Code fixes require a regression test in the affected language's tooling. The solver returns setup and check commands as argument arrays with a repository-relative working directory. They run inside a selected container with a read-only source mount and no credentials; installation and compilation must succeed before tests begin.

Regression tests must print `SCOUT_REGRESSION:<issue number>` in the targeted assertion's failure message. Failure on unfixed code demonstrates the regression only when the test phase starts and this message appears. Dependency or build failures do not count.

### Documentation Changes

Focused typos, missing explanations, incorrect examples and broken links can qualify without a failing code test. Static validation checks changed Markdown/MDX, reStructuredText, AsciiDoc and text files for UTF-8, conflict markers, explicit local Markdown file links and diff whitespace. It rejects source code and build/dependency manifests in documentation-only patches.

Accuracy still needs human review. Remote links, anchors, generated routes and rendered layout require additional checks or a docs build; the static checker does not verify them. Documentation regression results are `null` because code regression is not applicable.

## Reports and Recovery

### Email and Saved Files

The email includes the claim comment, root cause, approach, risks, unverified behavior, reviewer questions, pre-PR checklist and commit/PR details. Its HTML alternative highlights the issue, selection rationale, validation status and reported usage cost. Providers without a dollar estimate display `n/a`; this does not mean usage is free. Issue and brief text are HTML-escaped, and the claim link accepts only HTTPS URLs.

Plain text remains the canonical report and includes validation and regression logs. Every selected issue saves these files before connecting to Gmail:

| File | Contents |
|---|---|
| `reports/report.txt` | Secret-filtered plain-text review brief and logs. |
| `reports/change.patch` | Secret-filtered patch, when available and safe to include. |
| `reports/notification.json` | Delivery status, attempt count and any delivery error. |
| `state/seen.json` | Issue outcomes, workload, language, attempts and retry information. |
| `state/discovery.json` | Last examined retry and its due time, used to resume the next bounded scan. |

There is no separate HTML artifact. Local `reports/` files are ignored by Git.

### Delivery Failures and Retry History

Delivery uses an HTTPS relay backed by Resend. The client retries transient failures up to three times, waiting 2 and 4 seconds between attempts; permanent authentication or recipient errors stop immediately. The relay forwards a stable idempotency key for each workflow run and attempt so Resend can deduplicate retries. Reports and patches are saved before sending.

If delivery fails, the run emits a warning and preserves the report and patch. A verified patch remains `completed` with separate notification status, so email failure does not generate the same fix again. Successful delivery or a local notification preview records it as `notified`. Failed or unverified fixes keep their retry schedule: by default, after 7 days and up to 2 total attempts, even outside the discovery window. Legacy URL-only history files remain supported.

Discovery resumes due retries after the last examined entry, including across multiple runs on the same day. Each scan checks at most `3 × candidate_limit` retry issue URLs and recovers at most `candidate_limit` eligible retries. Closed, malformed or unavailable entries advance the cursor too. Cursor errors mark discovery partial and retain usable candidates; invalid attempt-history JSON still stops discovery to avoid repeating completed work. See [the discovery review fixes](docs/discovery-fixes.md) for the detailed behavior and regression coverage.

GitHub Actions uploads `report.txt`, any `change.patch` and `notification.json` as a `scout-result-<run id>-<attempt>` artifact, retained for 14 days. Upload and history-save steps run even if another step fails. Download the artifact from the workflow run to recover the result.

### Run Logs and Runner Storage

Logs start with the source revision and enabled capabilities. They show pool/category coverage, remaining candidates, score components, workload, language, validation plan, suitability, rejection reasons, resolution evidence and correction attempts. Known secrets are redacted. Long reports retain their beginning and end, and progress is unbuffered.

The hosted Ubuntu runner frees unused Android, .NET and Swift SDKs before installation; workload SDKs run inside Docker. Pip download caching is disabled, temporary clones and regression worktrees are removed, and interrupted Docker clients trigger removal of their own named containers.

## Security

- Target repository setup, builds and tests run in throwaway containers with a read-only source mount and no credentials. Static document checks only parse files on the host.
- Claude Code uses restricted mode with an explicit file-tool allowlist; shell, web, hooks, and MCP tools are unavailable. The target repository's `.claude/` and `.mcp.json` are removed before it starts.
- API and Codex solvers use Scout's bounded repository text tools. Codex reasons in a separate read-only directory with native shell, web search, image tools, and plugins disabled. Scout performs the requested reads and edits after checking paths; it rejects traversal, symlinks, metadata, and credential files. Tests remain inside Docker.
- CLI processes receive only their selected provider credential; other known keys and secret-named variables are removed. Known credentials are redacted from reports, patches, and traces, including base64 and URL-encoded forms. Credential-shaped content that survives filtering causes the patch to be withheld. A withheld patch or remaining credential-shaped HTML makes the email fall back to plain text.
- The Resend key stays on the operator's relay. GitHub Actions authenticates using signed, short-lived OIDC tokens. The relay restricts each enrolled repository ID, owner ID, branch, and workflow to its registered recipient, and sets the sender itself.
- The scout does not claim issues, post comments or submit changes to target repositories. You review and publish the contribution yourself. Its Actions workflow only saves history to its own repository.

## Limitations

- Supported code workloads include packages within multilingual repositories. Windows-only .NET targets and toolchains outside the listed runtimes need another harness.
- Manifest detection suggests feasibility; the actual build and test run verifies it.
- Documentation-only patches support text documents. Image-only changes or docs-tooling source changes need a different validation plan.
- Ranking uses terms, aliases and issue evidence, without embeddings.
- Each run attempts at most one issue; the daily workflow can abstain when evidence or confidence is insufficient.
- Provider access and usage limits depend on your own API account or subscription. OAuth sessions are provider-specific and cannot be used as arbitrary API keys.

## Maintainer

[@atty57](https://github.com/atty57).

## Contributing

For questions, bug reports or proposed improvements, [open an issue](https://github.com/atty57/issue-scout/issues). Pull requests are welcome; keep changes focused and describe the resulting behavior and relevant validation.

Install the development dependencies and run the offline checks before submitting code changes:

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q -p no:cacheprovider
```

The tests need no API keys and shuffle their order each run. To reproduce an order, use `--randomly-seed=<seed>` with the seed printed by pytest. Direct checks are also available through `python test_scout.py`. Include relevant regression coverage for behavior changes and update documentation when setup, configuration or outputs change.

## License

UNLICENSED. No license file is currently included in this repository.
