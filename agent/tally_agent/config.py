"""
config.py — the agent's own non-secret configuration file (YAML).

Deliberately separate from credential_store.py: this file is safe to
read, version, or hand to support/diagnostics (it's plain text, written
by the installer or `tally_agent enroll`); credential_store.py holds the
one thing that must never be — the credential_id/credential_secret pair
issued by the server at enrollment (see that module's own docstring).

Default location:
  Windows : %PROGRAMDATA%\\TallyAgent\\config.yaml   (machine-wide, matches
            the Windows Service running as LocalSystem/a service account)
  other   : ~/.tally_agent/config.yaml                (dev/verification
            only — this project has no Windows machine; see package
            docstring)
"""
from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

_DEFAULT_HEARTBEAT_INTERVAL_SECONDS = 30
_DEFAULT_TALLY_PORT = 9000


def default_config_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        return Path(base) / "TallyAgent"
    return Path.home() / ".tally_agent"


def default_config_path() -> Path:
    return default_config_dir() / "config.yaml"


def default_log_path() -> Path:
    return default_config_dir() / "logs" / "tally_agent.log"


@dataclass
class AgentConfig:
    server_url: str = ""  # e.g. "wss://dip.example.com" — ws_client appends "/api/tally-agent/ws"
    tally_host: str = "127.0.0.1"
    tally_port: int = _DEFAULT_TALLY_PORT
    heartbeat_interval_seconds: int = _DEFAULT_HEARTBEAT_INTERVAL_SECONDS
    log_level: str = "INFO"
    log_file: str = field(default_factory=lambda: str(default_log_path()))
    max_concurrent_commands: int = 4

    def ws_url(self) -> str:
        if not self.server_url:
            raise ConfigError("server_url is not configured — run 'tally_agent enroll' first.")
        scheme_fixed = self.server_url.rstrip("/")
        if scheme_fixed.startswith("https://"):
            scheme_fixed = "wss://" + scheme_fixed[len("https://"):]
        elif scheme_fixed.startswith("http://"):
            scheme_fixed = "ws://" + scheme_fixed[len("http://"):]
        return f"{scheme_fixed}/api/tally-agent/ws"


class ConfigError(Exception):
    """Raised for a missing/unreadable/invalid config file — caught by
    cli.py and turned into a clear operator-facing message rather than a
    raw traceback."""


def load_config(path: Path | None = None) -> AgentConfig:
    path = path or default_config_path()
    if not path.exists():
        raise ConfigError(
            f"No config file found at {path}. Run 'tally_agent enroll --server-url ... --token ...' first."
        )
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Could not parse {path} as YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a YAML mapping, not a {type(raw).__name__}.")
    known_fields = {f for f in AgentConfig.__dataclass_fields__}
    filtered = {k: v for k, v in raw.items() if k in known_fields}
    return AgentConfig(**filtered)


def save_config(config: AgentConfig, path: Path | None = None) -> Path:
    path = path or default_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(asdict(config), sort_keys=False), encoding="utf-8")
    return path
