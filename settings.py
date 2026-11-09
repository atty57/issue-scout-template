"""Profile, tuning knobs and process-wide constants, validated once at import."""
import json
import os
import re
from datetime import timedelta
from pathlib import Path

from workloads import LANGUAGE_RUNTIMES, canonical_language
from provider_config import SECRET_NAMES, load_provider

ROOT = Path(__file__).parent
PROFILE_PATH = Path(os.environ.get("SCOUT_PROFILE", ROOT / "profile.json"))
if not PROFILE_PATH.is_absolute():
    PROFILE_PATH = ROOT / PROFILE_PATH
PROFILE = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
SEEN = ROOT / "state" / "seen.json"
REPORTS = ROOT / "reports"
GITHUB = "https://github.com"
PICKER = load_provider("picker")
SOLVER = load_provider("solver")
MODEL = PICKER.model
MAX_FIXES = 2
PACE = 2  # seconds between search calls
SECRETS = SECRET_NAMES
AGENT_KEEPS = ("CLAUDE_CODE_OAUTH_TOKEN",)  # the only secret the agent's own process needs

DEFAULTS = {
    "updated_days": 30, "search_pages": 2, "candidate_limit": 30, "shortlist_size": 5,
    "max_comment_pages": 5, "max_timeline_pages": 5, "claim_days": 14,
    "retry_days": 7, "max_attempts": 2, "min_score": 6, "min_confidence": 0.7,
    "general_issues": False, "min_stars": 200,
    "exclude_labels": ["good first issue"],
}
PROFILE_FIELDS = {"setup_required", "email", "email_enabled", "summary", "skills", "skill_aliases", "languages",
                  "work_types", "labels", "documentation_labels", "watchlist", "merged_in", "days", "scouting"}
REQUIRED_FIELDS = ("days", "labels", "languages", "merged_in", "skills", "watchlist")  # read as PROFILE[...]
POSITIVE_INTEGERS = ("updated_days", "search_pages", "candidate_limit", "shortlist_size", "max_comment_pages",
                     "max_timeline_pages", "claim_days", "retry_days", "max_attempts", "min_stars")


def _string_list(name, values):
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() or any(ord(char) < 32 or ord(char) == 127 for char in value)
        for value in values
    ):
        raise ValueError(f"{name} must be a list of nonempty strings without control characters")


def _search_labels(name, values):
    _string_list(name, values)
    if any('"' in value or "\\" in value for value in values):
        raise ValueError(f"{name} cannot contain double quotes or backslashes in GitHub search")


def load_settings(profile):
    """Merge the profile's scouting overrides onto the defaults, rejecting unusable values."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    if unknown := sorted(profile.keys() - PROFILE_FIELDS):
        raise ValueError(f"unknown profile fields: {', '.join(unknown)}")
    if not isinstance(profile.get("scouting", {}), dict):
        raise ValueError("scouting must be an object")
    # The app's on/off switch for email. Without it set to true, an address in `email` never sends.
    if type(profile.get("email_enabled", False)) is not bool:
        raise ValueError("email_enabled must be true or false")
    if "days" in profile and (type(profile["days"]) is not int or not 1 <= profile["days"] <= timedelta.max.days):
        raise ValueError(f"days must be a positive integer at most {timedelta.max.days}")
    for name in ("languages", "watchlist"):
        if name in profile:
            _string_list(name, profile[name])
    for name in ("labels", "documentation_labels"):
        if name in profile:
            _search_labels(name, profile[name])
    for repo in profile.get("watchlist", []):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", repo) or repo.split("/")[1] in {".", ".."}:
            raise ValueError("watchlist entries must use owner/repository format")
    settings = DEFAULTS | profile.get("scouting", {})
    _search_labels("scouting.exclude_labels", settings["exclude_labels"])
    for name in POSITIVE_INTEGERS:
        if type(settings[name]) is not int or settings[name] < 1:
            raise ValueError(f"scouting.{name} must be a positive integer")
    if settings["updated_days"] > timedelta.max.days:
        raise ValueError(f"scouting.updated_days must be at most {timedelta.max.days}")
    if settings["search_pages"] > 10:
        raise ValueError("scouting.search_pages must be at most 10 (GitHub search result limit)")
    if type(settings["general_issues"]) is not bool:
        raise ValueError("scouting.general_issues must be a boolean")
    if settings["shortlist_size"] > 8:
        raise ValueError("scouting.shortlist_size must be at most 8 (picker context budget)")
    return settings


def require_fields(profile):
    """Fail at startup, not mid-run after paid calls, when the profile omits a field the engine reads."""
    if missing := [name for name in REQUIRED_FIELDS if name not in profile]:
        raise ValueError(f"missing profile fields: {', '.join(missing)}")


def load_workloads(profile):
    """-> (languages, work_types), rejecting anything the validation harness cannot run."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    _string_list("languages", profile.get("languages"))
    _string_list("work_types", profile.get("work_types", ["code"]))
    languages = {canonical_language(lang) for lang in profile["languages"]}
    work_types = set(profile.get("work_types", ["code"]))
    if not languages <= LANGUAGE_RUNTIMES.keys() or not work_types <= {"code", "documentation"} or not work_types:
        raise ValueError("unsupported profile language or work type")
    return languages, work_types


SCOUT = load_settings(PROFILE)
require_fields(PROFILE)
LANGUAGES, WORK_TYPES = load_workloads(PROFILE)
