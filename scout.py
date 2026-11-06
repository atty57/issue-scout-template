"""Issue scout: every day, find one open GitHub issue that fits my profile, fix it with
Claude Code, test the fix in a sandbox, and email me the patch plus a review brief.

LangGraph pipeline: discover -> shortlist -> gate -> pick -> revalidate -> solve -> notify
"""
import configparser
import json
import os
import re
import shlex
import shutil
import smtplib
import ssl
import subprocess
import tempfile
import time
import tomllib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import TypedDict
from urllib.parse import quote
from uuid import uuid4

import requests
from langchain_groq import ChatGroq
from langgraph.graph import END, START, StateGraph
from langsmith import traceable
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from workloads import (LANGUAGE_RUNTIMES, RUNTIME_IMAGES, TEST_PHASE, canonical_language,
                       check_documentation, inspect_languages, manifest_rank, safe_path, validation_script)

import email_html
from github_api import gh, gh_file, http
from prompts import FIX, TASK, correction_prompt, picker_prompt
# CLAIM and skill_matches are re-exported: the tests reach them through scout.
from ranking import (CLAIM, RESOLUTION, UNRESOLVED, active_claims, discussion_excerpt, excerpt,
                     interleave, issue_features, parsed_time, profile_fit, score, score_components,
                     skill_matches)
# forms is re-exported: the tests reach it through scout.
from redaction import forms, outbound, redact, redact_shapes, scan_secrets, scrub
from settings import (AGENT_KEEPS, GITHUB, LANGUAGES, MAX_FIXES, MODEL, PACE, PROFILE, REPORTS,
                      REQUIRED, ROOT, SCOUT, SECRETS, SEEN, WORK_TYPES)

if os.environ.get("LANGSMITH_API_KEY"):  # one secret turns tracing on; nothing else to remember
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    os.environ.setdefault("LANGSMITH_PROJECT", "issue-scout")

class State(TypedDict, total=False):
    candidates: list
    discovery: dict
    remaining_candidates: list
    repo_checks: dict
    search_more: bool
    issue: dict | None
    why: str
    result: dict


# ---------- helpers ----------

def gh_pages(path, max_pages=5):
    """A bounded read must be complete before declaring an issue available."""
    items = []
    for page in range(1, max_pages + 1):
        batch = gh(path, per_page=100, page=page)
        items.extend(batch)
        if len(batch) < 100:
            return items
    raise RuntimeError(f"pagination budget exhausted on {path}; availability unknown")


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def llm(schema):
    # json_schema, not Groq's default function_calling: gpt-oss 400s with "model did not call a tool"
    return ChatGroq(model=MODEL, temperature=0).with_structured_output(schema, method="json_schema")


# ---------- discover / shortlist ----------



def load_history():
    if not SEEN.exists():
        return {}
    try:
        data = json.loads(SEEN.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as e:
        raise ValueError("issue history is unreadable; refusing to rediscover attempted issues") from e
    # Keep prior notifications excluded when upgrading the old URL-only state file.
    if isinstance(data, list) and all(isinstance(url, str) for url in data):
        return {url: {"status": "notified", "attempts": 1} for url in data}
    if not isinstance(data, dict) or any(not isinstance(record, dict) for record in data.values()):
        raise ValueError("issue history must contain URL records or a legacy URL list")
    normalized = {}
    for url, record in data.items():
        record = dict(record)
        if type(record.get("issue_id")) is not int or record["issue_id"] < 1:
            record.pop("issue_id", None)
        language = record.get("language")
        language = canonical_language(language) if isinstance(language, str) else None
        if language in LANGUAGES:
            record["language"] = language
        else:
            record.pop("language", None)
        normalized[url] = record
    return normalized


def can_retry(record, now):
    if not isinstance(record, dict):
        return False
    retry = parsed_time(record.get("retry_after"))
    attempts = record.get("attempts")
    return (record.get("status") == "failed" and retry is not None and retry <= now
            and type(attempts) is int and 0 <= attempts < SCOUT["max_attempts"])


def issue_location(url):
    """Accept only GitHub issue URLs before constructing an API request or identity."""
    if not isinstance(url, str):
        return None
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9][0-9]*)/?", url, re.ASCII | re.I)
    if not match or any(part in {".", ".."} for part in match[1].split("/")):
        return None
    return match[1], int(match[2])


def canonical_issue_url(url):
    location = issue_location(url)
    return f"https://github.com/{location[0].lower()}/issues/{location[1]}" if location else url


class IssueHistory:
    """Match the same historical records for eligibility and attempt accounting."""

    def __init__(self, history):
        self.urls, self.ids = {}, {}
        for url, record in history.items():
            self.urls.setdefault(canonical_issue_url(url), {})[url] = record
            if type(record.get("issue_id")) is int and record["issue_id"] > 0:
                self.ids.setdefault(record["issue_id"], {})[url] = record

    def matching(self, issue, extra_urls=()):
        urls = [issue["html_url"], issue.get("history_url"), *extra_urls]
        if isinstance(issue.get("history_urls"), list):
            urls += issue["history_urls"]
        matches = {}
        for url in urls:
            if isinstance(url, str):
                matches.update(self.urls.get(canonical_issue_url(url), {}))
        if type(issue.get("id")) is int and issue["id"] > 0:
            matches.update(self.ids.get(issue["id"], {}))
        return matches


def discovery_issue(payload):
    """Normalize an API snapshot without modifying it; malformed items stay out of ranking."""
    if not isinstance(payload, dict):
        return None
    location = issue_location(payload.get("html_url"))
    if not location:
        return None
    repo, number = location
    labels = payload.get("labels")
    if (type(payload.get("number")) is not int or payload["number"] != number
            or not isinstance(payload.get("title"), str) or not payload["title"].strip()
            or (payload.get("body") is not None and not isinstance(payload["body"], str))
            or not isinstance(labels, list)
            or any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels)
            or not isinstance(payload.get("assignees", []), list)
            or type(payload.get("locked", False)) is not bool
            or ("author_association" in payload and not isinstance(payload["author_association"], str))
            or ("id" in payload and (type(payload["id"]) is not int or payload["id"] < 1))
            or not isinstance(payload.get("repository_url"), str)
            or payload["repository_url"].lower().rstrip("/") != f"https://api.github.com/repos/{repo}".lower()):
        return None
    result = dict(payload, repo=repo, search_languages=[])
    for name in ("features", "feasibility", "history_url", "history_urls"):
        result.pop(name, None)
    for name in ("created_at", "updated_at"):
        if name in payload:
            timestamp = parsed_time(payload[name]) if isinstance(payload[name], str) else None
            if timestamp is None:
                return None
            result[name] = timestamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return result


def discovery_available(issue):
    excluded = {label.casefold() for label in SCOUT["exclude_labels"]}
    return (issue.get("state") == "open" and not issue.get("locked")
            and not issue.get("assignees") and not issue.get("assignee")
            and "pull_request" not in issue
            and not any(label["name"].casefold() in excluded for label in issue["labels"]))


def load_retry_cursor(path):
    """A cursor controls scheduling only; attempt history still controls eligibility."""
    if not path.exists():
        return None
    cursor = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(cursor, dict) or not issue_location(cursor.get("last_checked_url")):
        raise ValueError("invalid discovery retry cursor")
    retry_after = parsed_time(cursor.get("last_retry_after"))
    if retry_after is None:
        raise ValueError("invalid discovery retry cursor timestamp")
    return retry_after, canonical_issue_url(cursor["last_checked_url"])


def discover(state):
    now = datetime.now(timezone.utc)
    since = now.date() - timedelta(days=min(PROFILE["days"], now.toordinal() - 1))
    updated = now.date() - timedelta(days=min(SCOUT["updated_days"], now.toordinal() - 1))
    base = 'is:issue is:open is:public archived:false no:assignee -linked:pr'
    base += "".join(f' -label:"{label}"' for label in SCOUT["exclude_labels"])
    languages = dict.fromkeys(canonical_language(lang) for lang in PROFILE["languages"])
    queries = [(f'{base} {"updated" if label.casefold() == "help wanted" else "created"}:>='
                f'{updated if label.casefold() == "help wanted" else since} label:"{label}" '
                f'language:"{"C#" if lang == "csharp" else lang}"',
                "updated" if label.casefold() == "help wanted" else "created", lang)
               for lang in languages for label in PROFILE["labels"] if "code" in WORK_TYPES]
    if "documentation" in WORK_TYPES:
        queries += [(f'{base} updated:>={updated} label:"{label}"', "updated", None)
                    for label in PROFILE.get("documentation_labels", ["documentation", "docs"])]
    # Watchlist searches include revived backlog and do not require a particular label or primary language.
    queries += [(f"{base} updated:>={updated} repo:{repo.lower()}", "updated", None) for repo in PROFILE["watchlist"]]
    queries = list(dict.fromkeys(queries))
    history = load_history()
    history_index = IssueHistory(history)
    found = {}
    aliases, blocked = {}, set()

    def collect(issue, language=None, origin=None):
        url = canonical_issue_url(issue["html_url"])
        identities = [("url", url)] + ([("id", issue["id"])] if "id" in issue else [])
        groups = list(dict.fromkeys(aliases[identity] for identity in identities if identity in aliases))
        key = groups[0] if groups else identities[-1]
        previous = [found[group] for group in groups]
        if any(group in blocked for group in groups):
            blocked.add(key)
        if len(groups) > 1:
            for identity, group in list(aliases.items()):
                if group in groups:
                    aliases[identity] = key
            for group in groups[1:]:
                found.pop(group)
        origins = [origin]
        for snapshot in previous:
            origins += [snapshot["html_url"], snapshot.get("history_url"), *snapshot.get("history_urls", [])]
        matched = history_index.matching(issue, origins)
        if any(not can_retry(record, now) for record in matched.values()):
            blocked.add(key)
        languages = {lang for snapshot in previous for lang in snapshot["search_languages"]}
        languages |= {language} if language else set()
        for snapshot in previous:
            old_time, new_time = parsed_time(snapshot.get("updated_at")), parsed_time(issue.get("updated_at"))
            if old_time and new_time and old_time != new_time:
                issue = issue if new_time > old_time else snapshot
            elif not discovery_available(snapshot):
                issue = snapshot
        found[key] = dict(issue, search_languages=sorted(languages))
        if matched:
            found[key]["history_urls"] = sorted(matched)
        current_url = canonical_issue_url(issue["html_url"])
        if prior_url := next((value for value in matched if canonical_issue_url(value) != current_url), None):
            found[key]["history_url"] = prior_url
        for identity in identities:
            aliases[identity] = key
        if origin is not None:
            aliases[("url", canonical_issue_url(origin))] = key
        return key

    diagnostics = {"partial": False, "searches": []}
    for q, sort, language in queries:
        search = {"query": q, "pages": 0, "received": 0, "invalid": 0, "incomplete": False,
                  "truncated": False, "error": None}
        diagnostics["searches"].append(search)
        seen = set()
        for page in range(1, SCOUT["search_pages"] + 1):
            time.sleep(PACE)
            try:
                response = gh("search/issues", q=q, sort=sort, order="desc", per_page=100, page=page)
                if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                    raise ValueError("invalid GitHub search response: expected an items list")
                total = response.get("total_count")
                if "total_count" in response and (type(total) is not int or total < 0):
                    raise ValueError("invalid GitHub search total_count")
                if type(response.get("incomplete_results", False)) is not bool:
                    raise ValueError("invalid GitHub search incomplete_results")
            except (RuntimeError, ValueError, requests.RequestException) as e:
                diagnostics["partial"] = True
                search["error"] = redact_shapes(str(e))[:500]
                print(f"discover: search unavailable ({search['error'][:120]})")
                break
            search["pages"] += 1
            search["received"] += len(response["items"])
            search["incomplete"] |= response.get("incomplete_results", False)
            for payload in response["items"]:
                issue = discovery_issue(payload)
                if issue is None:
                    search["invalid"] += 1
                    diagnostics["partial"] = True
                    continue
                seen.add(("id", issue["id"]) if "id" in issue else ("url", canonical_issue_url(issue["html_url"])))
                collect(issue, language)
            if len(response["items"]) < 100 or (total is not None and page * 100 >= total):
                search["truncated"] = total is not None and len(seen) != total
                break
        else:
            search["truncated"] = True
        diagnostics["partial"] |= search["incomplete"] or search["truncated"]
        search["unique"] = len(seen)
    # A seven-day retry would otherwise fall outside the three-day discovery window.
    due = [url for url, record in history.items()
           if ("url", canonical_issue_url(url)) not in aliases
           and ("id", record.get("issue_id")) not in aliases and can_retry(record, now)]
    pending = [(url, location) for url in due if (location := issue_location(url))]
    pending.sort(key=lambda entry: (parsed_time(history[entry[0]]["retry_after"]), canonical_issue_url(entry[0])))
    # Resume after the last examined retry, including when the candidate limit stopped an earlier run.
    request_budget = 3 * SCOUT["candidate_limit"]
    cursor_path = SEEN.with_name("discovery.json")
    retry_info = {"due": len(due), "checked": 0, "deferred": 0, "invalid": len(due) - len(pending),
                  "errors": [], "cursor_error": None}
    diagnostics["retries"] = retry_info
    if pending:
        try:
            cursor = load_retry_cursor(cursor_path)
        except (ValueError, OSError) as e:
            cursor = None
            retry_info["cursor_error"] = redact_shapes(str(e))[:500]
            diagnostics["partial"] = True
            print("discover: retry cursor unavailable; restarting bounded scan")
        if cursor:
            offset = next((index for index, (url, _) in enumerate(pending)
                           if (parsed_time(history[url]["retry_after"]), canonical_issue_url(url)) > cursor), 0)
            pending = pending[offset:] + pending[:offset]
    repositories, accepted = {}, set()
    processed = 0
    for processed, (url, (repo, number)) in enumerate(pending, 1):
        if ("url", canonical_issue_url(url)) in aliases:
            continue
        if retry_info["checked"] >= request_budget or len(accepted) >= SCOUT["candidate_limit"]:
            processed -= 1
            break
        retry_info["checked"] += 1
        key = None
        try:
            issue = discovery_issue(gh(f"repos/{repo}/issues/{number}"))
            if issue is None:
                raise ValueError("invalid GitHub retry issue")
            key = collect(issue, history[url].get("language") if history[url].get("language") in LANGUAGES else None,
                          origin=url)
            if not discovery_available(issue) or key in blocked:
                continue
            repo_key = issue["repo"].lower()
            if repo_key not in repositories:
                repositories[repo_key] = False
                metadata = gh(f"repos/{issue['repo']}")
                if (not isinstance(metadata, dict) or type(metadata.get("private")) is not bool
                        or type(metadata.get("archived")) is not bool
                        or type(metadata.get("disabled", False)) is not bool):
                    raise ValueError("retry repository visibility or availability is unknown")
                repositories[repo_key] = not (metadata["private"] or metadata["archived"] or metadata.get("disabled"))
            if not repositories[repo_key]:
                blocked.add(key)
                continue
            accepted.add(key)
        except (RuntimeError, ValueError, requests.RequestException) as e:
            if key is not None:
                blocked.add(key)
            error = redact_shapes(str(e))[:500]
            retry_info["errors"].append({"url": url, "error": error})
            diagnostics["partial"] = True
            print(f"discover: retry unavailable {repo}#{number} ({error[:120]})")
        finally:
            try:
                write_atomic(cursor_path, json.dumps({"last_checked_url": canonical_issue_url(url),
                                                      "last_retry_after": history[url]["retry_after"]}, indent=2))
            except OSError as e:
                if retry_info["cursor_error"] is None:
                    print("discover: retry cursor could not be saved; later scans may repeat")
                retry_info["cursor_error"] = redact_shapes(str(e))[:500]
                diagnostics["partial"] = True
    retry_info["deferred"] = len(pending) - processed
    diagnostics["partial"] |= bool(retry_info["deferred"] or retry_info["invalid"])
    candidates = [issue for key, issue in found.items() if key not in blocked and discovery_available(issue)]
    print(f"discover: {len(candidates)} eligible issues")
    return {"candidates": candidates, "discovery": diagnostics}




def shortlist(state):
    ranked = sorted((i for i in state["candidates"] if profile_fit(i)),
                    key=lambda i: (score(i), i.get("updated_at", "")), reverse=True)
    # Interleave repositories before applying the budget. Verbose reports from one repo
    # must not crowd every other project out of the pool or the first picker batch.
    categories = {}
    for issue in ranked:
        features = issue.get("features") or issue_features(issue)
        category = "documentation" if features.get("documentation") else next(iter(features.get("languages", [])), "unknown")
        categories.setdefault(category, {}).setdefault(issue["repo"], []).append(issue)
    batches = [list(interleave(list(repos.values()))) for repos in categories.values()]
    pool = list(interleave(batches))[:SCOUT["candidate_limit"]]
    print(f"shortlist: ranked {len(pool)} of {len(state['candidates'])} discovered issues "
          f"across {len({i['repo'] for i in pool})} repositories; "
          f"{len(categories)} work categories; {len(state['candidates']) - len(ranked)} had no profile fit")
    return {"candidates": pool, "remaining_candidates": pool, "repo_checks": {}}




def enrich_issue(issue):
    """Refresh authoritative state and read the full discussion before considering a fix."""
    endpoint = f"repos/{issue['repo']}/issues/{issue['number']}"
    fresh = gh(endpoint)
    if fresh.get("state") != "open" or fresh.get("pull_request") or fresh.get("locked"):
        raise ValueError("issue is closed, locked, or is a pull request")
    if fresh.get("assignees") or fresh.get("assignee"):
        raise ValueError("issue has an assignee")
    comments = gh_pages(endpoint + "/comments", SCOUT["max_comment_pages"]) if fresh.get("comments") else []
    claims = active_claims(comments)
    if claims:
        raise ValueError("active claim by " + ", ".join(claims))
    timeline = gh_pages(endpoint + "/timeline", SCOUT["max_timeline_pages"])
    connections = 0
    checked_prs = set()
    referenced_prs = []
    for event in timeline:
        if event.get("event") == "connected":
            connections += 1
        elif event.get("event") == "disconnected":
            connections -= 1
        source = event.get("source", {}).get("issue", {})
        if event.get("event") == "cross-referenced" and source.get("pull_request"):
            # Conservatively leave open referenced work to its author.
            pr_url = source["pull_request"].get("url")
            if not pr_url or not pr_url.startswith("https://api.github.com/repos/"):
                raise RuntimeError("cannot verify a referenced pull request")
            if pr_url in checked_prs:
                continue
            checked_prs.add(pr_url)
            pr = gh(pr_url.removeprefix("https://api.github.com/"))
            if pr.get("state") not in ("open", "closed"):
                raise RuntimeError("referenced pull request state is unknown")
            if pr.get("state") == "open":
                raise ValueError(f"open referenced PR: {pr.get('html_url', pr_url)}")
            referenced_prs.append({"url": pr.get("html_url", pr_url), "title": pr.get("title", ""),
                                   "state": pr["state"], "merged": pr.get("merged"),
                                   "merged_at": pr.get("merged_at")})
    if connections > 0:
        # REST connection events omit the linked target, so its availability cannot be proven.
        raise ValueError("unresolved manual link to another issue or pull request")
    refreshed = dict(issue) | fresh
    refreshed["repo"] = issue["repo"]
    refreshed["features"] = issue_features(refreshed, comments)
    refreshed["thread"] = "\n\n".join(
        f"{c['user']['login']} ({c.get('author_association', 'NONE')}, {c.get('created_at', '')}): {c.get('body') or ''}"
        for c in comments)
    refreshed["discussion_excerpt"] = discussion_excerpt(comments)
    refreshed["referenced_prs"] = referenced_prs
    return refreshed


def parse_pyproject(text):
    """-> (installable, requires_python, uncertain). Raises TOMLDecodeError on malformed input."""
    config = tomllib.loads(text)
    project, tool = config.get("project", {}), config.get("tool", {})
    uncertain = not isinstance(project, dict) or not isinstance(tool, dict)
    project = project if isinstance(project, dict) else {}
    tool = tool if isinstance(tool, dict) else {}
    installable = bool(project or isinstance(config.get("build-system"), dict)
                       or isinstance(tool.get("poetry"), dict))
    return installable, project.get("requires-python", ""), uncertain


def parse_setup_cfg(text):
    """-> (installable, requires_python, uncertain). Raises configparser.Error on malformed input."""
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(text)
    return (config.has_section("metadata") or config.has_section("options"),
            config.get("options", "python_requires", fallback=""), False)


def python_312_support(requirement):
    """-> (compatible, uncertain). None means undecidable, which keeps the repo at 'unknown'."""
    try:
        compatible = "3.12" in SpecifierSet(requirement) if requirement else None
    except InvalidSpecifier:
        return None, True
    # The Docker tag floats across patch releases; patch-specific constraints need confirmation.
    return (None if re.search(r"\b3\.12\.\d+", requirement) else compatible), False


def python_packages(repo, package_files):
    """Installable Python packages, and whether any manifest could not be read.

    Metadata only: nothing here installs or executes a candidate repository.
    """
    packages, uncertain = [], False
    for filename in package_files[:5]:
        installable, requirement = filename.endswith("setup.py"), ""
        try:
            if filename.endswith("pyproject.toml"):
                installable, requirement, flagged = parse_pyproject(gh_file(repo, filename))
                uncertain |= flagged
            elif filename.endswith("setup.cfg"):
                installable, requirement, flagged = parse_setup_cfg(gh_file(repo, filename))
                uncertain |= flagged
        except (tomllib.TOMLDecodeError, configparser.Error):
            uncertain = True
            continue
        if not installable:
            continue
        if not isinstance(requirement, str):
            uncertain = True
            requirement = ""
        compatible, flagged = python_312_support(requirement)
        uncertain |= flagged
        packages.append({"path": filename.rsplit("/", 1)[0] if "/" in filename else ".",
                         "requires_python": requirement, "python_312": compatible})
    return packages, uncertain


def repo_context(repo):
    """Read package/test metadata; never install or execute a candidate repo during scouting."""
    metadata = gh(f"repos/{repo}")
    if metadata.get("archived") or metadata.get("disabled"):
        return {"status": "unsupported", "reason": "repository is archived or disabled"}
    stars = metadata.get("stargazers_count")
    if type(stars) is not int or stars < 0:
        return {"status": "unsupported", "reason": "repository star count is unavailable"}
    if stars < SCOUT["min_stars"]:
        return {"status": "unsupported", "reason": f"repository has {stars} stars; minimum is {SCOUT['min_stars']}",
                "stars": stars}
    branch = quote(metadata["default_branch"], safe="")
    tree = gh(f"repos/{repo}/git/trees/{branch}", recursive=1)
    paths = [item["path"] for item in tree["tree"] if item["type"] == "blob"]
    package_files = sorted((p for p in paths if p.rsplit("/", 1)[-1] in {"pyproject.toml", "setup.py", "setup.cfg"}),
                           key=manifest_rank)
    tests = [p for p in paths if p.endswith(".py")
             and (p.rsplit("/", 1)[-1].startswith("test_") or p.endswith("_test.py"))]
    packages, metadata_uncertain = python_packages(repo, package_files)
    status = "supported" if packages and tests and not metadata_uncertain else "unknown"
    reason = "Python package and test files found" if status == "supported" else "pip/pytest compatibility needs confirmation"
    if (packages and all(p["python_312"] is False for p in packages)
            and len(package_files) <= 5 and not tree.get("truncated")):
        status, reason = "unsupported", "all inspected packages require a different Python version"
    if not package_files and not tests and not tree.get("truncated"):
        status, reason = "unsupported", "no Python package or Python tests found"
    python_context = {"status": status, "reason": reason, "packages": packages, "test_examples": tests[:8],
                      "runtimes": ["python312"]}
    languages = inspect_languages(paths, lambda path: gh_file(repo, path), python_context, tree.get("truncated", False))
    enabled = [value for key, value in languages.items() if key in LANGUAGES
               or (key == "documentation" and "documentation" in WORK_TYPES)]
    status = "supported" if any(c["status"] == "supported" for c in enabled) else "unknown" if (
        tree.get("truncated") or any(c["status"] == "unknown" for c in enabled)) else "unsupported"
    return {"status": status, "reason": "Available workload metadata: " + ", ".join(
        key for key, value in languages.items() if value["status"] != "unsupported"),
        "languages": languages, "packages": packages, "test_examples": tests[:8], "pushed_at": metadata.get("pushed_at"),
        "truncated": bool(tree.get("truncated")), "stars": stars}


# ---------- AI-contribution policy gate (code decides, the model only reads) ----------

POLICY_FILES = ("AI_POLICY.md", ".github/AI_POLICY.md", "AGENTS.md", "CONTRIBUTING.md",
                ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md", ".github/PULL_REQUEST_TEMPLATE.md")
AI_TERMS = re.compile(r"\b(AI|LLMs?|GenAI)\b|(?i:\b(generative|copilot|chatgpt|claude|codex|coding agents?|machine[- ]generated|vibe[- ]cod))")
POLICY_SCHEMA = {
    "title": "policy", "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["banned", "disclosure", "allowed"]},
        "quote": {"type": "string", "description": "the sentence(s) that decide it, verbatim"}},
    "required": ["status", "quote"]}


def ai_paragraphs(text):
    return "\n\n".join(p.strip() for p in re.split(r"\n\s*\n", text) if AI_TERMS.search(p))


def policy(repo):
    excerpt = ai_paragraphs("\n\n".join(gh_file(repo, f) for f in POLICY_FILES))[:6000]
    if not excerpt:
        return {"status": "none", "quote": ""}
    history = "already has merged PRs in this repo" if repo in PROFILE["merged_in"] else "has never contributed to this repo"
    prompt = (
        f"Below are excerpts from the contribution docs of {repo} that mention AI. Classify the project's rule for "
        f"a pull request whose code was written with an AI coding tool, then reviewed, tested and submitted by a human "
        f"who {history}.\n"
        "banned: not accepted from this contributor (includes bans on first-time contributors using AI, on autonomous "
        "agents, or on vibe-coded work). If it is unclear whether a ban applies, answer banned.\n"
        "disclosure: accepted if AI use is disclosed or stated conditions are met.\n"
        "allowed: accepted with no special requirement.\n\n" + excerpt)
    try:
        return llm(POLICY_SCHEMA).invoke(prompt)
    except Exception as e:  # fail closed: no verdict means no contribution
        return {"status": "banned", "quote": f"policy check failed ({e})"}


def gate(state):
    checks = state.get("repo_checks", {})
    rules, contexts = dict(checks.get("policies", {})), dict(checks.get("contexts", {}))
    unavailable_repos = set(checks.get("unavailable", []))
    pool = state.get("remaining_candidates", state["candidates"])
    processed = 0
    keep = []
    for processed, issue in enumerate(pool, 1):
        repo = issue["repo"]
        if repo in unavailable_repos or not profile_fit(issue):
            continue
        try:
            if repo not in rules:
                rules[repo] = policy(repo)
            if rules[repo]["status"] == "banned":
                print(f"gate: skip {repo} ({rules[repo]['quote'][:120]})")
                continue
            if repo not in contexts:
                contexts[repo] = repo_context(repo)
            if contexts[repo]["status"] == "unsupported":
                print(f"gate: skip {repo} ({contexts[repo]['reason']})")
                continue
        except (RuntimeError, ValueError, requests.RequestException) as e:
            unavailable_repos.add(repo)
            print(f"gate: repo check unavailable {repo} ({redact(str(e))[:160]})")
            continue
        try:
            enriched = enrich_issue(issue)
            enriched["policy"] = rules[repo]
            enriched["feasibility"] = contexts[repo]
            if not profile_fit(enriched) or score(enriched) < SCOUT["min_score"]:
                print(f"gate: skip {repo}#{issue['number']} (skill fit or score below {SCOUT['min_score']})")
                continue
            keep.append(enriched)
            if len(keep) == SCOUT["shortlist_size"]:
                break
        except (RuntimeError, ValueError, requests.RequestException) as e:
            print(f"gate: unavailable {repo}#{issue['number']} ({redact(str(e))[:160]})")
    remaining = pool[processed:]
    print(f"gate: {len(keep)} available candidates; {len(remaining)} ranked issues remain")
    return {"candidates": sorted(keep, key=score, reverse=True), "remaining_candidates": remaining,
            "repo_checks": {"policies": rules, "contexts": contexts, "unavailable": sorted(unavailable_repos)}}


# ---------- pick ----------

PICK_REQUIREMENTS = ("behavior", "scope", "language", "skill_fit", "local_test", "unresolved_bug")
PICK_SCHEMA = {
    "title": "pick", "type": "object",
    "properties": {"index": {"type": ["integer", "null"], "minimum": 0},
                   "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                   "why": {"type": "string"},
                   "evaluations": {"type": "array", "description": "One evaluation for EVERY listed candidate",
                                   "items": {"type": "object", "properties": {
                                       "index": {"type": "integer", "minimum": 0},
                                       "suitable": {"type": "boolean"},
                                       "work_type": {"type": "string", "enum": ["code", "documentation"]},
                                       "language": {"type": "string", "enum": [*LANGUAGE_RUNTIMES, "none", "other", "unknown"]},
                                       "validation_plan": {"type": "string", "description": "Credible local code test or documentation checks; no regression test required for docs"},
                                       "reason": {"type": "string", "description": "Evidence for suitability or a specific unmet requirement"},
                                       "unmet_requirements": {"type": "array", "items": {"type": "string", "enum": list(PICK_REQUIREMENTS)},
                                                              "description": "Empty when suitable; otherwise name each failed hard requirement"},
                                       "resolution_evidence": {"type": "string", "description": "Exact quote from supplied body or discussion proving already fixed; empty otherwise"}},
                                       "required": ["index", "suitable", "work_type", "language", "validation_plan", "reason", "unmet_requirements", "resolution_evidence"]}}},
    "required": ["index", "confidence", "why", "evaluations"]}


class SelectionEvidenceError(ValueError):
    """A complete verdict needs to be grounded again in the supplied evidence."""


def validate_pick(choice, listing):
    """Check a picker verdict against the evidence it was shown; -> {index: evaluation}.

    Two failure modes, and pick() treats them differently:

      ValueError              the verdict is malformed. pick() abstains, no retry.
      SelectionEvidenceError  the verdict is well formed but not grounded in the
                              supplied evidence. pick() re-prompts ONCE with the
                              message appended, then abstains.

    SelectionEvidenceError subclasses ValueError, so an `except ValueError` added
    anywhere above this call would swallow the retryable case and silently cost a
    verdict that one correction would have fixed.

    Malformed -> ValueError:
      confidence is not a number in [0, 1], or `why` is missing or blank
      evaluations is not a list, or is not exactly one per candidate
      an evaluation has a duplicate or out-of-range index, a non-bool `suitable`,
        or a blank `reason`
      index is None while some evaluation claims suitable
      index is set but out of range, below min_confidence, or points at an
        evaluation that says unsuitable
      work_type or language falls outside the schema's enums
      unmet_requirements is not a list of unique strings
      resolution_evidence is not a string

    Ungrounded -> SelectionEvidenceError:
      a suitable row selects a work type or language this profile has not enabled
      a suitable row carries no concrete validation plan
      a suitable row names a workload the repository has no compatible evidence for
      unmet_requirements disagrees with `suitable`, or names something outside
        PICK_REQUIREMENTS (maintainer approval is a preference, never a requirement)
      `unresolved_bug` is claimed without a resolution quote found in the supplied
        body or discussion (whitespace and enclosing quotation marks are ignored)
      resolution_evidence is supplied without an already-fixed verdict
    """
    index, confidence = choice["index"], choice["confidence"]
    if (type(confidence) not in (int, float) or not 0 <= confidence <= 1
            or not isinstance(choice["why"], str) or not choice["why"].strip()):
        raise ValueError("invalid confidence or explanation")
    evaluations = choice["evaluations"]
    if not isinstance(evaluations, list) or len(evaluations) != len(listing):
        raise ValueError("picker must evaluate every candidate")
    reviewed = {}
    for evaluation in evaluations:
        n = evaluation["index"]
        if (type(n) is not int or not 0 <= n < len(listing) or n in reviewed
                or type(evaluation["suitable"]) is not bool
                or not isinstance(evaluation["reason"], str) or not evaluation["reason"].strip()):
            raise ValueError("invalid candidate evaluation")
        reviewed[n] = evaluation
    if index is None and any(e["suitable"] for e in evaluations):
        raise ValueError("abstention contradicts a suitable candidate evaluation")
    if index is not None and (type(index) is not int or not 0 <= index < len(listing)
                              or confidence < SCOUT["min_confidence"] or not reviewed[index]["suitable"]):
        raise ValueError("invalid index, insufficient confidence or unsuitable selection")
    for n, evaluation in reviewed.items():
        kind, language = evaluation["work_type"], evaluation["language"]
        if kind not in {"code", "documentation"} or language not in {*LANGUAGE_RUNTIMES, "none", "other", "unknown"}:
            raise ValueError("invalid workload classification")
        if evaluation["suitable"]:
            if kind not in WORK_TYPES or (kind == "code" and language not in LANGUAGES) or (
                    kind == "documentation" and language != "none"):
                raise SelectionEvidenceError(f"candidate {n}: selected work type or language is not enabled")
            if not isinstance(evaluation["validation_plan"], str) or not evaluation["validation_plan"].strip():
                raise SelectionEvidenceError(f"candidate {n}: suitable work needs a concrete validation plan")
            contexts = listing[n]["test_feasibility"].get("languages")
            workload = "documentation" if kind == "documentation" else language
            default_status = "unknown" if listing[n]["test_feasibility"].get("truncated") else "unsupported"
            if contexts is not None and contexts.get(workload, {}).get("status", default_status) == "unsupported":
                raise SelectionEvidenceError(f"candidate {n}: selected workload has no compatible repository evidence")
        unmet = evaluation["unmet_requirements"]
        if (not isinstance(unmet, list) or any(not isinstance(r, str) for r in unmet)
                or len(set(unmet)) != len(unmet)):
            raise ValueError("invalid unmet requirements")
        if any(r not in PICK_REQUIREMENTS for r in unmet) or bool(unmet) == evaluation["suitable"]:
            raise SelectionEvidenceError(f"candidate {n}: suitability must agree with failed hard requirements; "
                                         "maintainer approval is a preference")
        resolution = evaluation["resolution_evidence"]
        if not isinstance(resolution, str):
            raise ValueError("invalid resolution evidence")
        if "unresolved_bug" in unmet:
            quote_text = " ".join(resolution.split())
            # Models sometimes wrap a copied sentence in literal quotation marks.
            # Remove only a matching outer pair; the content must still match a source.
            if len(quote_text) >= 2 and (quote_text[0], quote_text[-1]) in {
                    ('"', '"'), ("'", "'"), ("\u201c", "\u201d"), ("\u2018", "\u2019"), ("`", "`")}:
                quote_text = quote_text[1:-1].strip()
            sources = [listing[n]["body"], listing[n]["discussion"]]
            if len(quote_text) < 12 or not RESOLUTION.search(quote_text):
                raise SelectionEvidenceError(f"candidate {n}: resolution quote does not confirm a fix")
            if UNRESOLVED.search(quote_text):
                raise SelectionEvidenceError(f"candidate {n}: resolution quote says the issue remains unresolved")
            if not any(quote_text in " ".join(source.split()) for source in sources):
                raise SelectionEvidenceError(f"candidate {n}: resolution quote is absent from supplied body or discussion")
        elif resolution:
            raise SelectionEvidenceError(f"candidate {n}: resolution evidence without an already-fixed verdict")
    return reviewed


def candidate_listing(options):
    """What the picker is allowed to see: excerpted text plus the heuristics behind each score."""
    return [{"index": n, "repo": i["repo"], "number": i["number"], "title": i["title"],
             "body": excerpt(i.get("body") or "", 6000),
             "discussion": i.get("discussion_excerpt", excerpt(i.get("thread", ""), 6000)),
             "referenced_prs": i.get("referenced_prs", [])[-8:],
             "evidence": i.get("features") or issue_features(i), "heuristic_score": score(i),
             "score_components": score_components(i),
             "test_feasibility": i["feasibility"], "ai_policy": i.get("policy", {})}
            for n, i in enumerate(options)]


def log_candidates(options, listing, reviewed):
    """One scrubbed line per candidate, so a rejected verdict can be audited after the run."""
    for n, issue in enumerate(options):
        print("pick: candidate " + json.dumps(scrub({
            "index": n, "url": issue["html_url"], "title": issue["title"], "score": score(issue),
            "maintainer_support": bool(listing[n]["evidence"]["maintainer_support"]),
            "test_feasibility": issue["feasibility"]["status"],
            "score_components": score_components(issue), "work_type": reviewed[n]["work_type"],
            "language": reviewed[n]["language"], "validation_plan": reviewed[n]["validation_plan"],
            "suitable": reviewed[n]["suitable"], "reason": reviewed[n]["reason"],
            "unmet_requirements": reviewed[n]["unmet_requirements"],
            "resolution_evidence": reviewed[n]["resolution_evidence"]}), ensure_ascii=False))


def pick(state):
    options = [i for i in state["candidates"] if score(i) >= SCOUT["min_score"]
               and profile_fit(i)
               and i.get("feasibility", {}).get("status") in ("supported", "unknown")]
    if not options:
        print("pick: no eligible candidates in this batch")
        return {"issue": None, "search_more": bool(state.get("remaining_candidates"))}
    listing = candidate_listing(options)
    prompt = picker_prompt(listing)
    try:
        model = llm(PICK_SCHEMA)
        for attempt in range(2):
            choice = model.invoke(prompt)
            try:
                reviewed = validate_pick(choice, listing)
                break
            except SelectionEvidenceError as e:
                print("pick: unsupported rejection " + json.dumps(scrub(choice), ensure_ascii=False))
                if attempt:
                    raise
                print(f"pick: requesting one corrected verdict ({redact(str(e))})")
                prompt += correction_prompt(e, choice)
        index, confidence = choice["index"], choice["confidence"]
        log_candidates(options, listing, reviewed)
        if index is None:
            print(f"pick: abstained ({redact(choice['why'])})")
            return {"issue": None, "why": choice["why"], "search_more": bool(state.get("remaining_candidates"))}
        issue, why = dict(options[index]), choice["why"]
        issue["selection_confidence"] = confidence
        issue.update({key: reviewed[index][key] for key in ("work_type", "language", "validation_plan")})
    except Exception as e:
        print(f"pick: skipped ({redact(str(e))[:160]})")
        return {"issue": None, "why": "no reliable ranking verdict", "search_more": False}
    print(f"pick: {issue['html_url']}")
    return {"issue": issue, "why": why, "search_more": False}


def revalidate(state):
    issue = state["issue"]
    remaining = [i for i in state["candidates"] if i["html_url"] != issue["html_url"]]
    try:
        refreshed = enrich_issue(issue)
        if any((refreshed.get(field) or "") != (issue.get(field) or "")
               for field in ("title", "body", "thread", "referenced_prs")):
            raise ValueError("issue or discussion changed since selection; ranking verdict is stale")
        if not profile_fit(refreshed) or score(refreshed) < SCOUT["min_score"]:
            raise ValueError("issue no longer meets the suitability threshold")
        return {"issue": refreshed, "candidates": remaining}
    except (RuntimeError, ValueError, requests.RequestException) as e:
        print(f"revalidate: skip {issue['repo']}#{issue['number']} ({redact(str(e))[:160]})")
        return {"issue": None, "candidates": remaining}


# ---------- solve: Claude Code edits, Docker runs the repo's code ----------

BRIEF_SCHEMA = {
    "type": "object",
    "properties": {
        "root_cause": {"type": "string"},
        "approach": {"type": "string", "description": "what changed and why this over alternatives"},
        "risks": {"type": "string"},
        "not_verified": {"type": "string"},
        "maintainer_questions": {"type": "array", "items": {"type": "string"},
                                 "description": "3-5 questions a reviewer is likely to ask, each with a short answer"},
        "test_cwd": {"type": "string", "description": "repo-relative working directory for setup and validation commands"},
        "test_files": {"type": "array", "items": {"type": "string"},
                       "description": "repo-relative regression tests and necessary test/build wiring; empty for docs-only changes"},
        "validation": {"type": "object", "properties": {
            "runtime": {"type": "string", "enum": list(RUNTIME_IMAGES)},
            "setup_commands": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                               "description": "Install dependencies and compile/build BEFORE testing; argv arrays, never host commands"},
            "test_commands": {"type": "array", "items": {"type": "array", "items": {"type": "string"}},
                              "description": "Actual targeted test/check commands as argv arrays; empty for static documentation validation"},
            "regression_marker": {"type": "string", "description": "SCOUT_REGRESSION:<issue number> assertion failure message; empty for documentation"}},
            "required": ["runtime", "setup_commands", "test_commands", "regression_marker"]},
        "checklist": {"type": "array", "items": {"type": "string"},
                      "description": "what the contributor must do before opening the PR per the repo's guidelines"},
        "commit_message": {"type": "string"},
        "pr_title": {"type": "string"},
        "pr_body": {"type": "string"},
        "claim_comment": {"type": "string", "description": "short, polite comment asking to work on the issue"}},
    "required": ["root_cause", "approach", "risks", "not_verified", "maintainer_questions", "test_cwd",
                 "test_files", "validation", "checklist", "commit_message", "pr_title", "pr_body", "claim_comment"]}


@traceable(run_type="llm", name="claude-code", process_inputs=scrub, process_outputs=scrub)
def claude(prompt, cwd, session=None):
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--json-schema", json.dumps(BRIEF_SCHEMA),
           "--permission-mode", "dontAsk", "--permission-prompts", "none",
           "--allowedTools", "Read,Edit,Write,Glob,Grep", "--disallowedTools", "Bash,WebFetch,WebSearch"]
    if session:
        cmd += ["--resume", session]
    # the agent can read any absolute path, so don't leave the other secrets in its environment
    env = {k: v for k, v in os.environ.items() if k not in SECRETS or k in AGENT_KEEPS}
    out = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=1800)
    try:
        data = json.loads(out.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"claude exited {out.returncode}: {(out.stderr or out.stdout)[-1500:]}")
    if out.returncode or data.get("is_error") or not data.get("structured_output"):
        raise RuntimeError(f"claude run failed: {str(data.get('result'))[-1500:]}")
    return data


def clone(repo):
    path = tempfile.mkdtemp()
    try:
        git(path, "clone", "--depth", "1", f"{GITHUB}/{repo}.git", ".")
        # claude -p runs a repo's own hooks and MCP servers without asking; strip them first
        shutil.rmtree(Path(path, ".claude"), ignore_errors=True)
        Path(path, ".mcp.json").unlink(missing_ok=True)
        return path
    except BaseException:
        shutil.rmtree(path, ignore_errors=True)
        raise


def diff(repo_dir):
    git(repo_dir, "add", "-A", "--", ".", ":(exclude).claude", ":(exclude).mcp.json")
    return git(repo_dir, "diff", "--cached")


def sandbox(repo_dir, script, runtime="python312"):
    """Repo code only ever runs here: a throwaway container with no secrets and the repo mounted read-only."""
    return ["docker", "run", "--rm", "-v", f"{repo_dir}:/src:ro",
            "-e", "SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0", "-e", "PIP_NO_CACHE_DIR=1",
            RUNTIME_IMAGES[runtime], "sh", "-c", script]


def run_container(repo_dir, script, runtime="python312"):
    # Killing the Docker client on a timeout does not stop its container.
    name = f"issue-scout-{uuid4().hex}"
    command = sandbox(repo_dir, script, runtime)
    command[3:3] = ["--name", name]
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=1500)
    except BaseException:
        try:
            cleanup = subprocess.run(["docker", "rm", "--force", name], capture_output=True, text=True, timeout=30)
            if cleanup.returncode:
                print(f"solve: could not remove interrupted container {name}")
        except (OSError, subprocess.SubprocessError):
            print(f"solve: could not remove interrupted container {name}")
        raise


@traceable(name="pytest", process_outputs=scrub)
def run_tests(repo_dir, cwd, files):
    files = [f for f in files if f.endswith(".py") and safe_path(f)]
    if not files or not safe_path(cwd):
        return False, f"rejected test_cwd={cwd!r} test_files={files!r}"
    script = (f"W=$(mktemp -d) && cp -r /src/. $W && cd $W/{shlex.quote(cwd)} && "
              "pip install -q -e '.[test,tests,dev]'; pip install -q pytest; "
              f"cd $W && python -m pytest -x -q {' '.join(map(shlex.quote, files))}")
    try:
        run = run_container(repo_dir, script)
    except subprocess.TimeoutExpired:
        return False, "Validation timed out after 1500 seconds; no regression verdict. Container cleanup was requested."
    return run.returncode == 0, (run.stdout + run.stderr)[-4000:]


@traceable(name="validate-change", process_inputs=scrub, process_outputs=scrub)
def run_validation(repo_dir, brief, work_type="code", language="python"):
    plan = brief.get("validation")
    if plan is None and work_type == "code" and language == "python":
        return run_tests(repo_dir, brief["test_cwd"] or ".", brief["test_files"])
    try:
        if work_type not in {"code", "documentation"} or not isinstance(plan, dict) or plan.get("runtime") not in RUNTIME_IMAGES:
            raise ValueError("invalid validation plan")
        cwd = brief["test_cwd"] or "."
        if not safe_path(cwd) or any(not safe_path(f) for f in brief["test_files"]):
            raise ValueError("invalid validation paths")
        runtime = plan["runtime"]
        if work_type == "code":
            if runtime not in LANGUAGE_RUNTIMES.get(language, []):
                raise ValueError("validation runtime does not match the selected language")
            if not brief["test_files"] or not isinstance(plan["regression_marker"], str) or not re.fullmatch(
                    r"SCOUT_REGRESSION:\d+", plan["regression_marker"]):
                raise ValueError("code fixes require regression files and a SCOUT_REGRESSION:<issue number> failure message")
        docs_log = ""
        if work_type == "documentation":
            diff(repo_dir)
            whitespace = git(repo_dir, "diff", "--cached", "--check")
            files = git(repo_dir, "diff", "--cached", "--name-only", "-z").strip("\x00").split("\x00")
            passed, docs_log = check_documentation(repo_dir, files if files != [""] else [])
            if not passed:
                return False, docs_log
            if runtime == "documentation":
                if plan["setup_commands"] or plan["test_commands"] or plan["regression_marker"]:
                    raise ValueError("static documentation validation needs empty commands and regression marker")
                return True, docs_log + whitespace
        script = validation_script(plan, cwd)
        run = run_container(repo_dir, script, runtime)
        output = run.stdout + run.stderr
        if TEST_PHASE not in output:
            return False, "Setup/build failed before validation started.\n" + output[-4000:]
        # Retain the phase marker even when verbose test output is truncated.
        return run.returncode == 0, docs_log + "\n" + TEST_PHASE + "\n" + output[-4000:]
    except subprocess.TimeoutExpired:
        return False, "Validation timed out after 1500 seconds; no regression verdict. Container cleanup was requested."
    except (ValueError, KeyError, TypeError, subprocess.CalledProcessError) as e:
        return False, f"validation rejected or failed: {e}"


def fails_without_fix(repo_dir, patch, brief):
    """Apply only the test hunks to a clean checkout: a real regression test has to fail there."""
    if not brief["test_files"] or any(not safe_path(f) for f in brief["test_files"]):
        return False, "invalid regression test paths"
    clean = tempfile.mkdtemp()
    descriptor, filename = tempfile.mkstemp(suffix=".patch")
    os.close(descriptor)
    patch_file = Path(filename)
    try:
        patch_file.write_text(patch, encoding="utf-8")
        git(repo_dir, "worktree", "add", "--detach", clean, "HEAD")
        git(clean, "apply", *[f"--include={f}" for f in brief["test_files"]], str(patch_file))
        if not all(Path(clean, f).is_file() for f in brief["test_files"]):
            return False, "test files are not in the patch"
        plan = brief.get("validation")
        if plan is None:
            passed, log = run_tests(clean, brief["test_cwd"] or ".", brief["test_files"])
            return bool(not passed and not log.startswith("Validation timed out")), log
        language = next((lang for lang, runtimes in LANGUAGE_RUNTIMES.items() if plan["runtime"] in runtimes), "unknown")
        passed, log = run_validation(clean, brief, "code", language)
        return bool(not passed and TEST_PHASE in log and plan["regression_marker"] in log), log
    except subprocess.CalledProcessError as e:
        return False, f"couldn't apply the test hunks: {e.stderr}"
    finally:
        try:
            git(repo_dir, "worktree", "remove", "--force", clean)
        except subprocess.CalledProcessError:
            pass
        finally:
            shutil.rmtree(clean, ignore_errors=True)
            patch_file.unlink(missing_ok=True)


def task_prompt(issue):
    pol = issue["policy"]
    disclosure = ""
    if pol["status"] == "disclosure":
        disclosure = (f"\n5. This repository requires disclosing AI assistance. Its policy says: \"{pol['quote']}\". "
                      "Put the disclosure in pr_body (and commit_message if it asks for a trailer) exactly as it specifies.")
    kind, language = issue.get("work_type", "code"), issue.get("language", "python")
    instructions = ("Make the smallest documentation correction. Check the documented behavior against source "
                    "and existing examples. Return empty test_files and regression_marker. Use runtime=documentation "
                    "with empty command lists for static UTF-8/conflict/local-link checks, or choose an available "
                    "runtime with commands for the existing docs build. State manual accuracy and rendered-layout "
                    "review needs; do not invent a code regression test." if kind == "documentation" else
                    f"Write a targeted regression test, then the smallest code fix. The test's assertion failure "
                    f"must print SCOUT_REGRESSION:{issue['number']}; set validation.regression_marker to exactly that. "
                    "Put dependency installation and compilation in setup_commands, actual test execution in "
                    "test_commands. Include the test and required build/test wiring in test_files so they can be "
                    "applied to the unfixed checkout. A setup or compilation failure does not prove the bug.")
    return TASK.format(url=issue["html_url"], title=issue["title"], body=excerpt(issue.get("body") or "", 8000),
                       thread=issue.get("discussion_excerpt", excerpt(issue.get("thread", ""), 6000)) or "(none)",
                       disclosure=disclosure, work_type=kind, language=language, validation_instructions=instructions,
                       execution_context=json.dumps({"repository": issue.get("feasibility", {}),
                                                     "runtime_images": RUNTIME_IMAGES,
                                                     "setup_note": "Install required extra build tools inside the container; do not assume CMake, Ninja or a package manager beyond npm is preinstalled."}, ensure_ascii=False))


def solve(state):
    issue = state["issue"]
    repo_dir = None
    try:
        repo_dir = clone(issue["repo"])
        data = claude(task_prompt(issue), repo_dir)
        for attempt in range(MAX_FIXES + 1):
            brief = data["structured_output"]
            kind, language = issue.get("work_type", "code"), issue.get("language", "python")
            passed, log = run_validation(repo_dir, brief, kind, language)
            print(f"solve: attempt {attempt + 1} validation {'pass' if passed else 'fail'}")
            if passed or attempt == MAX_FIXES:
                break
            data = claude(FIX.format(cwd=brief["test_cwd"], files=brief["test_files"], log=log,
                                     runtime=brief.get("validation", {}).get("runtime", "python312")), repo_dir, data["session_id"])
        patch = diff(repo_dir)
        reproduces, regression_log = fails_without_fix(repo_dir, patch, brief) if passed and kind == "code" else (None, "not applicable to documentation")
        return {"result": {"brief": brief, "patch": patch, "passed": passed, "log": log,
                           "reproduces": reproduces, "regression_log": regression_log,
                           "documentation_verified": bool(passed and kind == "documentation"),
                           "cost": data.get("total_cost_usd", 0.0)}}
    except Exception as e:  # still email the pick: the claim matters even without a fix
        return {"result": {"error": f"{type(e).__name__}: {e}"}}
    finally:
        if repo_dir is not None:
            shutil.rmtree(repo_dir, ignore_errors=True)


# ---------- notify ----------

def write_atomic(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_suffix(path.suffix + ".tmp")
    pending.write_text(text, encoding="utf-8")
    pending.replace(path)


def send_email(msg):
    """Retry transient failures on fresh, verified TLS connections; never discard the report."""
    for attempt in range(1, 4):
        smtp = None
        try:
            smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30, context=ssl.create_default_context())
            smtp.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
            refused = smtp.send_message(msg)
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
        except (smtplib.SMTPException, OSError) as e:
            error = redact(f"{type(e).__name__}: {e}")[:500]
            if scan_secrets(error):
                error = f"{type(e).__name__}: credential-shaped response withheld"
            transient = (isinstance(e, smtplib.SMTPServerDisconnected)
                         or (isinstance(e, smtplib.SMTPResponseException) and 400 <= e.smtp_code < 500)
                         or (isinstance(e, smtplib.SMTPRecipientsRefused) and bool(e.recipients)
                             and all(400 <= code < 500 for code, _ in e.recipients.values()))
                         or (isinstance(e, OSError) and not isinstance(e, smtplib.SMTPException)))
            print(f"notify: email attempt {attempt} failed ({error})")
            if not transient or attempt == 3:
                return {"status": "failed", "attempts": attempt, "error": error}
        else:
            print(f"notify: email sent on attempt {attempt}")
            return {"status": "sent", "attempts": attempt}
        finally:
            if smtp is not None:
                # A QUIT failure after acceptance must not trigger another copy of the email.
                try:
                    smtp.close()
                except OSError:
                    pass
        time.sleep(2 ** attempt)


def report_lines(state):
    """The human-readable review brief, before any redaction."""
    issue, r = state["issue"], state["result"]
    pol = issue["policy"]
    lines = [issue["title"], issue["html_url"], "", f"Why this one: {state['why']}",
             f"Work type: {issue.get('work_type', 'code')} | language: {issue.get('language', 'python')}",
             f"Scout score: {score(issue)} | suitability confidence: {issue.get('selection_confidence', 'unknown')}",
             f"Validation feasibility: {issue.get('feasibility', {}).get('reason', 'unknown')}",
             f"AI policy: {pol['status']}" + (f" - \"{pol['quote']}\"" if pol["quote"] else ""), ""]
    if "error" in r:
        lines.append(f"No fix this time: {r['error']}")
        return lines
    b = r["brief"]
    validation_summary = (f"Documentation checks: {'pass' if r['passed'] else 'FAIL'} | code regression: not applicable\n"
                          + r.get("log", "") if issue.get("work_type") == "documentation" else
                          f"Tests: {'pass' if r['passed'] else 'FAIL'} | new test fails on the unfixed code: "
                          f"{'yes' if r['reproduces'] else 'not shown'}")
    lines += [validation_summary + f" | Claude usage est. ${r['cost']:.2f}", "",
        "1) Claim it now. Comment on the issue:", b["claim_comment"], "",
        "Root cause:", b["root_cause"], "", "Approach:", b["approach"], "", "Risks:", b["risks"], "",
        "Not verified:", b["not_verified"], "",
        "Questions a maintainer may ask:", *[f"- {q}" for q in b["maintainer_questions"]], "",
        "Before opening the PR:", *[f"- {c}" for c in b["checklist"]], "",
        "Commit message:", b["commit_message"], "", f"PR title: {b['pr_title']}", "", "PR body:", b["pr_body"]]
    if r.get("log"):
        lines += ["", "Validation log:", r["log"]]
    if r.get("regression_log"):
        lines += ["", "Regression verification log:", r["regression_log"]]
    return lines


def compose(state):
    """-> (message, body, patch). No side effects: everything outbound passes through outbound()."""
    issue, r = state["issue"], state["result"]
    msg = EmailMessage()
    subject = redact_shapes(f"[scout] {issue['repo']}#{issue['number']}: {issue['title']}")
    msg["Subject"] = re.sub(r"[\r\n]+", " ", subject)[:180]
    msg["From"] = msg["To"] = os.environ.get("GMAIL_ADDRESS", "dry-run@localhost")
    body, patch = outbound("\n".join(report_lines(state)), r.get("patch"))
    msg.set_content(body)
    page = redact(email_html.render(state, score(issue)))
    if "PATCH WITHHELD" not in body and not scan_secrets(page):  # fail closed: text-only mail
        msg.add_alternative(page, subtype="html")
    if patch:
        msg.add_attachment(patch.encode(), maintype="text", subtype="x-diff",
                           filename=f"{issue['repo'].replace('/', '-')}-{issue['number']}.patch")
    return msg, body, patch


def save_report(body, patch):
    """Land the brief on disk before the email is attempted, so a failed send loses nothing."""
    write_atomic(REPORTS / "report.txt", body)
    patch_path = REPORTS / "change.patch"
    if patch:
        write_atomic(patch_path, patch)
    else:
        patch_path.unlink(missing_ok=True)
    print(f"notify: report saved to {REPORTS}")


def attempt_record(state, patch, history):
    """-> (record, successful). A verified patch still needs delivery before it counts as notified."""
    issue, r = state["issue"], state["result"]
    now = datetime.now(timezone.utc)
    is_docs = issue.get("work_type") == "documentation"
    successful = bool(r.get("passed") and patch and
                      (r.get("documentation_verified") if is_docs else r.get("reproduces")))
    prior = IssueHistory(history).matching(issue).values()
    attempts = max((record["attempts"] for record in prior
                    if type(record.get("attempts")) is int and record["attempts"] >= 0), default=0)
    record = {"status": "completed" if successful else "failed", "attempted_at": now.isoformat(),
              "attempts": attempts + 1,
              "tests_passed": bool(r.get("passed")), "reproduces": None if is_docs else bool(r.get("reproduces")),
              "work_type": issue.get("work_type", "code"), "language": issue.get("language", "python"),
              "documentation_verified": bool(r.get("documentation_verified")),
              "score": score(issue), "why": redact_shapes(state["why"]),
              "reason": redact_shapes(r.get("error") or ("verified patch" if successful else "fix not verified")),
              "notification": {"status": "pending", "attempts": 0}}
    if type(issue.get("id")) is int and issue["id"] > 0:
        record["issue_id"] = issue["id"]
    if not successful:
        record["retry_after"] = (now + timedelta(days=SCOUT["retry_days"])).isoformat()
    return record, successful


def persist(history, notification):
    write_atomic(REPORTS / "notification.json", json.dumps(notification, indent=2))
    write_atomic(SEEN, json.dumps(history, indent=2))


def notify(state):
    msg, body, patch = compose(state)
    save_report(body, patch)
    history = load_history()
    record, successful = attempt_record(state, patch, history)
    # Record the attempt before sending: a crash mid-send must not look like an untried issue.
    history[state["issue"]["html_url"]] = record
    persist(history, record["notification"])

    delivery = send_email(msg) if os.environ.get("GMAIL_APP_PASSWORD") else {"status": "dry_run", "attempts": 0}
    record["notification"] = delivery          # record is the object inside history, so this updates both
    if successful and delivery["status"] in {"sent", "dry_run"}:
        record["status"] = "notified"
    persist(history, delivery)
    if delivery["status"] == "failed":
        print("notify: email unavailable; report and patch preserved for download")
        if os.environ.get("CI"):
            print("::warning::Email notification failed. Download the scout-result artifact for the report and patch.")
    elif delivery["status"] == "dry_run":
        print(msg)
    return {}


def build():
    g = StateGraph(State)
    g.add_sequence([discover, shortlist, gate, pick])
    g.add_node("revalidate", revalidate)
    g.add_sequence([solve, notify])
    g.add_edge(START, "discover")
    g.add_conditional_edges("pick", lambda s: "revalidate" if s.get("issue") else "gate" if s.get("search_more") else END)
    g.add_conditional_edges("revalidate", lambda s: "solve" if s.get("issue") else "pick")
    # Each candidate can pass through gate/pick/revalidate; allow the entire bounded pool to be checked.
    return g.compile().with_config(recursion_limit=4 * SCOUT["candidate_limit"] + 10)


if __name__ == "__main__":
    print(f"scout: revision={os.environ.get('GITHUB_SHA', 'local')} "
          f"languages={','.join(sorted(LANGUAGES))} work_types={','.join(sorted(WORK_TYPES))} "
          f"general_issues={SCOUT['general_issues']} min_stars={SCOUT['min_stars']}")
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing and os.environ.get("CI"):
        raise SystemExit(f"Empty secrets: {', '.join(missing)}. Add each one under Settings > Secrets and variables > "
                         "Actions > Secrets tab > New repository secret (not Variables, not an environment).")
    build().invoke({})
