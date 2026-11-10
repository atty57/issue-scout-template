"""Scoring and text heuristics: pure functions over an issue dict, no network, no state."""
import re
from datetime import datetime, timedelta, timezone

from settings import LANGUAGES, PROFILE, SCOUT, WORK_TYPES


def parsed_time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    except (AttributeError, TypeError, ValueError):
        return None

CLAIM = re.compile(
    r"\b(i'?d like to|i'?ll|i will|i'?m|i am|can i|could i|may i|let me)\s+"
    r"(work(ing)? on|tak(e|ing)|pick(ing)?( this| it)? up|fix(ing)?|tackl(e|ing))\b"
    r"|\bassign (this |it )?to me\b|\b(still|currently) working on (this|it|a fix)\b", re.I)
WITHDRAW = re.compile(
    r"\b(unassign me|feel free to (?:take (?:this|it|the issue)|pick (?:this|it|the issue) up)|"
    r"someone else can (?:take (?:this|it)|pick (?:this|it) up)|"
    r"(?:i(?:'m| am) (?:not|no longer)|i(?: can't| cannot| can no longer)) (?:work(?:ing)?|continue)|"
    r"i (?:won't|will not) (?:work|fix|take)|withdraw(?:ing)? my (?:claim|request))\b", re.I)
MAINTAINERS = {"OWNER", "MEMBER", "COLLABORATOR"}
SUPPORT = re.compile(r"\b(confirmed|reproduced|prs? (?:are )?welcome|pull requests? (?:are )?welcome|"
                     r"happy to (?:accept|review)|please (?:send|submit|open) (?:a )?(?:pr|pull request))\b", re.I)
REPRO = re.compile(r"\b(steps to reproduce|minimal reproduc\w*|reproducer|expected behavio[u]?r|"
                   r"expected:|traceback|assert\w*)\b", re.I)
RESOLUTION = re.compile(r"\b(fixed|resolved|merged|released|still (?:fails|broken)|"
                        r"(?:no longer|cannot|can still) reproduce|reopened|not fixed)\b", re.I)
UNRESOLVED = re.compile(r"\b(not (?:yet )?(?:fixed|resolved)|still (?:fails?|failing|broken|reproduces?)|"
                        r"can still reproduce|reopened)\b", re.I)
BLOCKERS = {
    "design decision": r"\b(breaking change|redesign|rfc|needs? (?:a )?(?:design|maintainer) decision)\b",
    "hardware": r"\b(requires? [^.\n]{0,40}\b(?:gpu|cuda|hardware)|gpu.only)\b",
    "external access": r"\b(requires? (?:an? )?(?:api key|credentials|paid (?:account|service)))\b",
}
DEFAULT_ALIASES = {
    "retriev": ["retrieve", "retrieval", "retriever", "retrievers", "retrieving"],
    "rag": ["rag", "retrieval augmented generation", "retrieval-augmented generation"],
    "embedding": ["embedding", "embeddings"], "agent": ["agent", "agents"],
    "tool call": ["tool call", "tool calls", "tool calling"],
    "function call": ["function call", "function calls", "function calling"],
    "async": ["async", "asyncio", "asynchronous"],
    "llm": ["llm", "llms", "large language model", "large language models"],
}
LANGUAGE_TERMS = {
    "python": r"\bpython\b|\.py\b", "javascript": r"\bjavascript\b|\bnode\.?js\b|\.(?:js|jsx|mjs)\b",
    "typescript": r"\btypescript\b|\.(?:ts|tsx)\b", "csharp": r"(?<!\w)c#|\bcsharp\b|\.cs\b|\bdotnet\b|\.net\b",
    "c": r"\b(?:c language|c compiler|c library|c code|gcc|cmake)\b|\w+\.c\b",
    # "go" alone is an ordinary word, so Go needs Go-specific evidence.
    "go": r"\bgolang\b|\bgo (?:test|build|vet|mod|module|modules|toolchain|version)\b|\bgo\.mod\b|\w+\.go\b",
    "rust": r"\brust(?:c|up)?\b|\bcargo\b|\bcrates\.io\b|\w+\.rs\b",
    "java": r"\bjava\b|\bjvm\b|\bjunit\b|\bmaven\b|\bgradle\b|\w+\.java\b",
    "cpp": r"(?<!\w)c\+\+|\bcpp\b|(?<!\w)(?:g|clang)\+\+|\w+\.(?:cpp|cc|cxx|hpp|hh|hxx)\b",
}


def skill_matches(text):
    aliases = PROFILE.get("skill_aliases", {})
    return [skill for skill in PROFILE["skills"]
            if any(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text, re.I)
                   for term in [skill] + DEFAULT_ALIASES.get(skill, []) + aliases.get(skill, []))]


def active_claims(comments, now=None):
    now = now or datetime.now(timezone.utc)
    claims = {}
    for comment in sorted(comments, key=lambda c: c.get("created_at") or ""):
        if comment.get("user", {}).get("type") == "Bot":
            continue
        login = comment.get("user", {}).get("login", "unknown contributor")
        body = re.sub(r"```.*?```", "", comment.get("body") or "", flags=re.S)
        body = re.sub(r"(?m:^\s*>[^\n]*$)", "", body)
        body = body.replace("’", "'")
        if WITHDRAW.search(body):
            claims.pop(login, None)
        elif CLAIM.search(body):
            times = [t for value in (comment.get("created_at"), comment.get("updated_at"))
                     if (t := parsed_time(value)) is not None]
            claims[login] = max(times) if times else None
    return sorted(login for login, timestamp in claims.items()
                  if timestamp is None or now - timestamp <= timedelta(days=SCOUT["claim_days"]))


def issue_features(issue, comments=()):
    text = f"{issue['title']} {issue.get('body') or ''}"
    support = [{"author": c["user"]["login"], "quote": (c.get("body") or "")[:1000],
                "created_at": c.get("created_at", "")}
               for c in comments if c.get("author_association") in MAINTAINERS
               and SUPPORT.search(c.get("body") or "")]
    labels = {label["name"].lower() for label in issue.get("labels", [])}
    languages = set(issue.get("search_languages", [])) | {
        lang for lang, pattern in LANGUAGE_TERMS.items() if re.search(pattern, text, re.I)}
    docs = bool(labels & {label.lower() for label in PROFILE.get("documentation_labels", ["documentation", "docs"])}
                or re.search(r"\b(documentation|docs|readme|typos?|tutorial)\b", text, re.I))
    return {"matched_skills": skill_matches(text), "title_skills": skill_matches(issue["title"]),
            "languages": sorted(languages & LANGUAGES), "documentation": docs,
            "difficulty": "hard" if labels & {"epic", "complex", "architecture", "hard"} else "unknown",
            "maintainer_support": support[-3:], "reproducible": bool(REPRO.search(text)),
            "blockers": [name for name, pattern in BLOCKERS.items() if re.search(pattern, text, re.I)]}


def profile_fit(issue):
    evidence = issue.get("features") or issue_features(issue)
    return bool(evidence["matched_skills"] or (SCOUT["general_issues"] and
                (evidence.get("languages") or issue["repo"] in PROFILE["watchlist"]
                 or issue["repo"] in PROFILE["merged_in"]
                 or ("documentation" in WORK_TYPES and evidence.get("documentation")))))


def score_components(issue):
    evidence = issue.get("features") or issue_features(issue)
    now = datetime.now(timezone.utc)
    updated = parsed_time(issue.get("updated_at"))
    pushed = parsed_time(issue.get("feasibility", {}).get("pushed_at"))
    return {"topic_fit": 3 * min(len(evidence["matched_skills"]), 5) + 2 * min(len(evidence["title_skills"]), 3),
            "language_fit": 4 * bool(evidence.get("languages")) if SCOUT["general_issues"] else 0,
            "documentation": 6 * bool(evidence.get("documentation") and "documentation" in WORK_TYPES),
            "reproducer": 2 * bool(evidence["reproducible"]),
            "bug_label": 2 * any(label["name"].lower() == "bug" for label in issue.get("labels", [])),
            "maintainer_signal": 4 * bool(evidence["maintainer_support"]) + (issue.get("author_association") in MAINTAINERS),
            "familiarity": 2 * (issue["repo"] in PROFILE["watchlist"]) + 2 * (issue["repo"] in PROFILE["merged_in"]),
            "scope": -3 * len(evidence["blockers"]) - 2 * (evidence.get("difficulty") == "hard"),
            "freshness": 1 if updated and now - updated <= timedelta(days=SCOUT["updated_days"]) else 0,
            "repo_activity": 1 if pushed and now - pushed <= timedelta(days=90) else 0}


def score(issue):
    return sum(score_components(issue).values())


def interleave(groups):
    for position in range(max(map(len, groups), default=0)):
        for group in groups:
            if position < len(group):
                yield group[position]

def excerpt(text, limit):
    """Keep both ends of long reports: templates often put the reproducer at the end."""
    if len(text) <= limit:
        return text
    marker = "\n[... excerpt truncated ...]\n"
    head = (limit - len(marker)) * 2 // 3
    tail = limit - len(marker) - head
    return text[:head] + marker + text[-tail:]


def discussion_excerpt(comments, limit=6000):
    """Keep recent updates plus relevant earlier evidence, within the picker budget."""
    if not comments:
        return ""
    rendered = [f"{c['user']['login']} ({c.get('author_association', 'NONE')}, "
                f"{c.get('created_at', '')}): {excerpt(c.get('body') or '', 1400)}" for c in comments]

    def priority(n):
        comment = comments[n]
        body = comment.get("body") or ""
        return (4 * bool(RESOLUTION.search(body)) + 3 * (comment.get("author_association") in MAINTAINERS)
                + 2 * bool(REPRO.search(body)), n)

    # Reserve space for the newest replies, which can contradict an earlier resolution.
    order = list(range(max(0, len(comments) - 2), len(comments)))[::-1]
    order += sorted(range(len(comments)), key=priority, reverse=True)
    selected, used = set(), 0
    for n in order:
        if n not in selected and used + len(rendered[n]) + 2 <= limit - 100:
            selected.add(n)
            used += len(rendered[n]) + 2
    result = "\n\n".join(rendered[n] for n in sorted(selected))
    if len(selected) < len(comments):
        result += f"\n[Discussion excerpt: {len(comments) - len(selected)} comments omitted.]"
    return result
