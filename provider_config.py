"""Provider selection and credential names, independent of profiles and network clients."""
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


PROVIDERS = {"groq", "openai", "openai-compatible", "anthropic", "claude-code", "codex"}
API_PROVIDERS = {"groq", "openai", "openai-compatible", "anthropic"}
SECRET_NAMES = (
    "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY", "GROQ_API_KEY", "SCOUT_API_KEY",
    "SCOUT_PICKER_API_KEY", "SCOUT_SOLVER_API_KEY", "RESEND_API_KEY",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN", "GH_TOKEN", "LANGSMITH_API_KEY",
)
BASE_URLS = {
    "groq": "https://api.groq.com/openai/v1", "openai": "https://api.openai.com/v1",
    "openai-compatible": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com/v1",
}
KEY_NAMES = {
    "groq": "GROQ_API_KEY", "openai": "OPENAI_API_KEY", "openai-compatible": "SCOUT_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY", "claude-code": "ANTHROPIC_API_KEY", "codex": "CODEX_API_KEY",
}


@dataclass(frozen=True)
class ProviderConfig:
    role: str
    provider: str
    model: str = ""
    base_url: str = ""
    auth: str = "auto"

    @property
    def key_name(self):
        override = f"SCOUT_{self.role.upper()}_API_KEY"
        if os.environ.get(override):
            return override
        if self.provider == "codex" and self.auth == "api-key" and not os.environ.get("CODEX_API_KEY"):
            return "OPENAI_API_KEY"
        if self.provider == "openai-compatible" and not os.environ.get("SCOUT_API_KEY"):
            return "OPENAI_API_KEY"
        return KEY_NAMES[self.provider]

    @property
    def api_key(self):
        return os.environ.get(self.key_name, "")


def load_provider(role, environ=None):
    env = os.environ if environ is None else environ
    prefix = f"SCOUT_{role.upper()}_"
    provider = env.get(prefix + "PROVIDER", "groq" if role == "picker" else "claude-code").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError(f"{prefix}PROVIDER must be one of: {', '.join(sorted(PROVIDERS))}")
    auth = env.get(prefix + "AUTH", "auto").strip().lower()
    if auth not in {"auto", "api-key", "subscription"}:
        raise ValueError(f"{prefix}AUTH must be auto, api-key, or subscription")
    if provider in API_PROVIDERS and auth == "subscription":
        raise ValueError(f"{provider} requires an API key; subscription login uses claude-code or codex")
    default_model = env.get("SCOUT_MODEL", "openai/gpt-oss-120b") if provider == "groq" else ""
    model = (env.get(prefix + "MODEL") or default_model).strip()
    base_url = (env.get(prefix + "BASE_URL") or BASE_URLS.get(provider, "")).rstrip("/")
    if provider in API_PROVIDERS:
        parsed = urlsplit(base_url)
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or not (parsed.scheme == "https" or parsed.scheme == "http" and local)):
            raise ValueError(f"{prefix}BASE_URL must use HTTPS (HTTP is allowed only for loopback servers); no embedded credentials")
    return ProviderConfig(role, provider, model, base_url, auth)
