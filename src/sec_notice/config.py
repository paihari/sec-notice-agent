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
    # Phase 3: AI analyst
    anthropic_api_key: str | None
    analyst_model: str
    material_forms: frozenset[str]
    materiality_threshold: int

    @classmethod
    def load(cls) -> "Config":
        data_dir = Path(os.getenv("DATA_DIR", "data/filings"))
        if not data_dir.is_absolute():
            data_dir = PROJECT_ROOT / data_dir
        forms = {
            f.strip().upper()
            for f in os.getenv("MATERIAL_FORMS", "8-K,10-K,10-Q").split(",")
            if f.strip()
        }
        return cls(
            user_agent=os.getenv("SEC_USER_AGENT", DEFAULT_USER_AGENT),
            database_url=os.getenv("DATABASE_URL", "sqlite:///sec_notice.db"),
            data_dir=data_dir,
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            analyst_model=os.getenv("ANALYST_MODEL", "claude-opus-4-8"),
            material_forms=frozenset(forms),
            materiality_threshold=int(os.getenv("MATERIALITY_THRESHOLD", "60")),
        )


config = Config.load()
