"""Tests for config/settings.py (environment-driven process settings)."""
from optionsbot.config.settings import Settings


def test_settings_defaults(monkeypatch):
    monkeypatch.delenv("OPTIONSBOT_POLYGON_API_KEY", raising=False)
    s = Settings(_env_file=None)
    assert s.polygon_api_key is None
    assert s.ibkr_host == "127.0.0.1"
    assert s.ibkr_paper_port == 7497
    assert s.ibkr_live_port == 7496
    assert s.ibkr_paper_port != s.ibkr_live_port
    assert s.log_level == "INFO"


def test_settings_reads_env_override(monkeypatch):
    monkeypatch.setenv("OPTIONSBOT_POLYGON_API_KEY", "test-key-123")
    monkeypatch.setenv("OPTIONSBOT_LOG_LEVEL", "DEBUG")
    s = Settings(_env_file=None)
    assert s.polygon_api_key == "test-key-123"
    assert s.log_level == "DEBUG"


def test_ensure_data_dirs_creates_directories(tmp_path, monkeypatch):
    monkeypatch.delenv("OPTIONSBOT_POLYGON_API_KEY", raising=False)
    s = Settings(_env_file=None, data_dir=tmp_path / "data", cache_dir=tmp_path / "data" / "cache", db_path=tmp_path / "data" / "journal.sqlite")
    s.ensure_data_dirs()
    assert s.data_dir.is_dir()
    assert s.cache_dir.is_dir()
    assert s.db_path.parent.is_dir()
