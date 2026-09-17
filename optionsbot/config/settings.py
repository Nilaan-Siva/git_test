"""Process-level settings: secrets, connection info, and filesystem paths.

Loaded from environment variables (prefixed OPTIONSBOT_) and an optional `.env` file at the
repo root. This is deliberately separate from the YAML config in this package: settings.py is
per-machine and secret-bearing (gitignored .env); the YAML files are shared, committed, and
human-reviewed trading configuration.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OPTIONSBOT_", extra="ignore")

    polygon_api_key: Optional[str] = None

    ibkr_host: str = "127.0.0.1"
    ibkr_paper_port: int = 7497  # IB Gateway/TWS paper trading port
    ibkr_live_port: int = 7496  # real money -- Phase 8 only, behind the go-live gate
    ibkr_client_id: int = 7

    data_dir: Path = PROJECT_ROOT / "data"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"
    db_path: Path = PROJECT_ROOT / "data" / "journal.sqlite"

    log_level: str = "INFO"

    def ensure_data_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    return Settings()
