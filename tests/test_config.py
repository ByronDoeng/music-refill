from __future__ import annotations

from music_refill.config import BrowserCfg, Config, load


def test_defaults_are_consistent():
    cfg = Config()
    assert cfg.scan.limit == 50
    assert cfg.scan.sleep_min <= cfg.scan.sleep_max
    assert cfg.browser.debug_port == 9222
    assert cfg.scan.upload_attempts >= 1


def test_env_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("MUSIC_REFILL_DATA_DIR", str(tmp_path))
    cfg = load()
    assert cfg.paths.data_dir == tmp_path


def test_browser_cfg_fields():
    cfg = Config()
    assert isinstance(cfg.browser, BrowserCfg)
    assert cfg.browser.executable == ""


def test_sources_dirs_are_resolved_and_legacy_dir_still_works(tmp_path, monkeypatch):
    """新配置用 dirs 叠加；旧配置 dir 仍能加载，不会被静默丢掉。"""
    import music_refill.config as config_mod

    monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp_path)
    cfg_file = tmp_path / "config.toml"
    cfg_file.write_text(
        '[sources]\ndirs = ["sources_local", "private_sources"]\norder = ["a"]\n',
        encoding="utf-8",
    )
    cfg = load()
    assert cfg.sources.dirs == [
        str(tmp_path / "sources_local"),
        str(tmp_path / "private_sources"),
    ]

    cfg_file.write_text('[sources]\ndir = "legacy_sources"\n', encoding="utf-8")
    cfg = load()
    assert cfg.sources.dirs == [str(tmp_path / "legacy_sources")]


def test_sources_enabled_disabled_are_normalized(tmp_path, monkeypatch):
    import music_refill.config as config_mod

    monkeypatch.setattr(config_mod, "PROJECT_ROOT", tmp_path)
    (tmp_path / "config.toml").write_text(
        '[sources]\nenabled = ["kuwo", " migu ", ""]\ndisabled = ["broken"]\n',
        encoding="utf-8",
    )
    cfg = load()
    assert cfg.sources.enabled == ["kuwo", "migu"]
    assert cfg.sources.disabled == ["broken"]
