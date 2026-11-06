# Discovery review fixes

These changes address the four findings from the `testingFeature` branch review: malformed optional history metadata, retry starvation, lost attempt counts after transfers, and an unrelated change to PR eligibility. The resulting branch preserves the existing selection policy while strengthening discovery and its history handling.

| Finding | Previous behavior | Fixed behavior |
| --- | --- | --- |
| Optional history metadata | Lists or objects in `issue_id` or `language` could raise `TypeError` and abort discovery. | Invalid optional values are ignored in memory; valid attempt and status rules continue to apply. |
| Retry fairness | A date-based offset repeatedly visited the same subset when the candidate limit stopped the scan early. | A persistent cursor resumes after the last examined retry on the next run. |
| Legacy transfers | Merging an old issue URL into its newer transferred snapshot could lose the previous attempt count. | All matched history URLs travel with the candidate, and one shared matcher controls eligibility and accounting. |
| PR eligibility scope | Every historical PR reference blocked an issue, including abandoned attempts. | The earlier policy is restored: open PRs block selection; closed PRs supply verified evidence for evaluation. |

## 1. Validate optional history metadata at the read boundary

`load_history()` now normalizes a copy of each record before discovery builds identity indexes or checks supported languages.

- `issue_id` must be a positive integer. Booleans, strings, lists, objects, nulls and negative values are ignored.
- A language must be a string that normalizes to an enabled language. For example, `" Python "` becomes `"python"`; unsupported or incorrectly typed values are ignored.
- Status, attempt counts and retry timestamps retain their existing eligibility rules. Invalid optional metadata cannot turn a completed, notified or unknown-status record into a new candidate.

This fixes both reproduced crashes: using an unhashable issue ID as a dictionary key and using an unhashable language value in a set lookup. Reading discovery history does not rewrite `state/seen.json`. Invalid history JSON or an invalid record structure still stops discovery, because treating damaged attempt history as empty could repeat completed work.

## 2. Resume retries with persistent progress

The old scheduler rotated by `3 × candidate_limit` each day but stopped after accepting only `candidate_limit` retries. With six eligible issues and a limit of one, it returned issues 4 and 1 repeatedly while never examining the other four. Repeated runs on the same day also started at the same offset.

Discovery now stores a small scheduling cursor beside the attempt-history file. In the normal project layout this is `state/discovery.json`:

```json
{
  "last_checked_url": "https://github.com/acme/core/issues/3",
  "last_retry_after": "2026-10-23T12:00:00Z"
}
```

Due entries are ordered by retry time and canonical URL. A new run starts at the first entry after the saved position, wrapping to the beginning when necessary. Saving the ordering key lets the scan continue when the previously checked issue disappears from the queue.

The cursor advances after each attempted issue lookup, including timeouts, malformed responses, closed issues and rejected repository checks. Writes use the existing atomic-file helper. The scan still performs at most `3 × candidate_limit` retry issue lookups and accepts at most `candidate_limit` eligible retry identities. Repository visibility checks remain separately cached within the run.

A missing cursor starts at the beginning. A damaged or unreadable cursor restarts the bounded scan and marks discovery partial. A failed cursor write also marks discovery partial while retaining usable candidates. The cursor controls scheduling; it does not bypass attempt-history exclusions or retry cooldowns.

The workflow's final state-save step now commits both `state/seen.json` and `state/discovery.json` when present, even when no issue was solved. This preserves progress on the next runner. No initial cursor file or migration is required.

## 3. Preserve attempt history through issue transfers

The new `IssueHistory` matcher indexes canonical URLs and valid stable issue IDs. Both discovery and attempt recording use it, replacing two independently maintained sets of matching rules.

When search snapshots merge, discovery collects matching records from the current URL, earlier snapshots, direct-retry origins and already retained historical URLs. All matched source URLs remain attached to the merged candidate. Incoming GitHub payloads cannot supply these internal provenance fields; discovery removes them and constructs its own values.

Attempt recording uses the same matcher and writes the highest valid matching attempt count plus one. Counts are not summed across aliases, because multiple URLs can describe the same attempt history. A legacy record with one prior attempt therefore produces attempt two after a transfer, regardless of which search returns first. When multiple historical URLs contain counts of one and two, the next recorded count is three.

Completed or otherwise non-retryable matching records still suppress the candidate. Existing scalar `history_url` provenance remains supported alongside the complete list of matched URLs.

Legacy URL-only history cannot identify a transfer if neither the old URL nor a matching stable ID is available. The fix preserves every linkage discovery actually observes; it does not infer an identity without evidence.

## 4. Restore the PR policy that preceded the scope change

The blanket historical-PR exclusion introduced by commit `9bcf042` has been backed out in the working tree, without rewriting branch history. A future proposal for that stricter policy can be reviewed separately.

- Referenced PRs are refreshed through GitHub before their state is used.
- An open referenced PR blocks selection.
- A closed PR is retained with its verified merge state, title, URL and merge timestamp for the picker.
- A closed or abandoned reference alone does not prove the issue has been fixed. Existing evidence requirements still apply to an already-fixed verdict.
- Unknown PR state, unverifiable references and unresolved manual links continue to prevent selection.
- Revalidation compares referenced-PR evidence again, so changes after selection invalidate the old verdict.

The README and selection tests now describe and enforce this restored policy.

## Validation

Each reproduced failure was captured in a regression test before its fix. The new `test_scout_discovery_fixes.py` adds 30 cases covering optional metadata, same-day fairness at candidate limits of 1, 2 and 30, failed or stale entries, removed cursor entries, damaged cursor files, failed cursor writes, transfers in both result orders and multiple historical URLs. Selection tests restore closed-PR evidence and changed-PR revalidation coverage.

The complete offline suite passes: **457 tests**. The workflow YAML parses, the changed state-save shell block passes Git Bash syntax validation, and `git diff --check` passes. No live GitHub, model, email or Docker calls were needed for these checks.

Cursor progress assumes sequential runs sharing the same state directory. The existing GitHub Actions concurrency group serializes workflow runs; simultaneous independent local processes would need separate coordination. Persisted review decisions for rejected or unselected issues remain a separate feature from discovery retry scheduling.
