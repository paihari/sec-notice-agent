"""Runtime configuration, loaded from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Project root = three levels up from this file (src/sec_notice/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

load_dotenv(PROJECT_ROOT / ".env")

# SEC's fair-access policy: identify yourself and stay at or below 10 req/s.
# https://www.sec.gov/os/webmaster-faq#developers
DEFAULT_USER_AGENT = "SEC Notice Agent (hpai.bantwal@gmail.com)"


@dataclass(frozen=True)
class Config:
    user_agent: str
    database_url: str
    data_dir: Path

    @classmethod
    def load(cls) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "data/filings"))
        if not data_dir.is_absolute():
            data_dir = PROJECT_ROOT / data_dir
        return cls(
            user_agent=os.getenv("SEC_USER_AGENT", DEFAULT_USER_AGENT),
            database_url=os.getenv("DATABASE_URL", "sqlite:///sec_notice.db"),
            data_dir=data_dir,
        )


config = Config.load()
