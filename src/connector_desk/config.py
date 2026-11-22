"""Configuration loaded from environment / .env. Secrets live only in .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Env vars whose values must never be written anywhere (ledger, stdout).
SECRET_ENV_VARS = ("COMPOSIO_API_KEY", "ANTHROPIC_API_KEY")

TRACKER_TAB = "Tracker"
STATUS_TAB = "Status"


@dataclass(frozen=True)
class Config:
    composio_api_key: str
    anthropic_api_key: str
    anthropic_model: str
    composio_user_id: str
    sheet_id: str
    gmail_label: str
    stale_days: int
    ledger_path: Path

    def missing_for_live(self) -> list[str]:
        required = {
            "COMPOSIO_API_KEY": self.composio_api_key,
            "ANTHROPIC_API_KEY": self.anthropic_api_key,
            "ANTHROPIC_MODEL": self.anthropic_model,
            "COMPOSIO_USER_ID": self.composio_user_id,
            "SHEET_ID": self.sheet_id,
            "GMAIL_LABEL": self.gmail_label,
        }
        return [k for k, v in required.items() if not v]


def load_config(load_dotenv_file: bool = True) -> Config:
    if load_dotenv_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(REPO_ROOT / ".env")
        except ImportError:  # dotenv is optional for offline tests
            pass
    env = os.environ.get
    return Config(
        composio_api_key=env("COMPOSIO_API_KEY", ""),
        anthropic_api_key=env("ANTHROPIC_API_KEY", ""),
        anthropic_model=env("ANTHROPIC_MODEL", ""),
        composio_user_id=env("COMPOSIO_USER_ID", ""),
        sheet_id=env("SHEET_ID", ""),
        gmail_label=env("GMAIL_LABEL", "connector-desk"),
        stale_days=int(env("STALE_DAYS", "7") or 7),
        ledger_path=Path(env("LEDGER_PATH", str(REPO_ROOT / "ledger" / "actions.jsonl"))),
    )
