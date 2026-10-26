"""音源集合可组合、可替换，且单个坏文件不影响其它源。"""
from __future__ import annotations

from music_refill.sources.loader import load_sources


def _write_adapter(directory, name: str, source: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.py").write_text(
        "from music_refill.sources.base import SourceTrack, register_source\n"
        f"class Adapter:\n"
        f"    name = '{source}'\n"
        "    quality_ceiling = '320'\n"
        "    def health_check(self): return True\n"
        "    def search(self, title, artist='', limit=5):\n"
        "        return [SourceTrack(source=self.name, title=title, artist=artist,\n"
        "                            ext='mp3', download_url='https://example.invalid/a')]\n"
        "register_source(Adapter())\n",
        encoding="utf-8",
    )


def test_loader_accepts_custom_directory(tmp_path):
    source_dir = tmp_path / "adapters"
    source_dir.mkdir()
    (source_dir / "sample.py").write_text(
        "from music_refill.sources.base import SourceTrack, register_source\n"
        "class Sample:\n"
        "    name = 'sample'\n"
        "    quality_ceiling = '320'\n"
        "    def health_check(self): return True\n"
        "    def search(self, title, artist='', limit=5):\n"
        "        return [SourceTrack(source='sample', title=title, artist=artist,\n"
        "                            ext='mp3', download_url='https://example.invalid/a')]\n"
        "register_source(Sample())\n",
        encoding="utf-8",
    )
    status = load_sources(sources_dir=source_dir, probe=False)
    assert "sample" in status
    assert status["sample"]["healthy"] is True


def test_loader_reports_broken_adapter_without_crashing(tmp_path):
    source_dir = tmp_path / "adapters"
    source_dir.mkdir()
    (source_dir / "broken.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    status = load_sources(sources_dir=source_dir, probe=False)
    assert status["broken"]["healthy"] is False
    assert "加载失败" in status["broken"]["error"]


def test_loader_layers_directories_and_lets_later_override(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_adapter(first, "shared", "shared")
    _write_adapter(second, "shared", "shared")
    _write_adapter(second, "extra", "extra")

    status = load_sources(dirs=[first, second], probe=False)
    assert set(status) == {"shared", "extra"}
    assert status["shared"]["path"].replace("\\", "/").endswith("/second/shared.py")


def test_loader_enabled_and_disabled_filter_registry(tmp_path):
    from music_refill.sources import get_registry

    source_dir = tmp_path / "adapters"
    _write_adapter(source_dir, "one", "one")
    _write_adapter(source_dir, "two", "two")
    _write_adapter(source_dir, "three", "three")

    status = load_sources(
        sources_dir=source_dir, probe=False,
        enabled=["one", "two", "three"], disabled=["two"],
    )
    assert set(status) == {"one", "three"}
    assert set(get_registry()) == {"one", "three"}

    status = load_sources(sources_dir=source_dir, probe=False, enabled=["two"])
    assert set(status) == {"two"}
    assert set(get_registry()) == {"two"}


def test_loader_allows_swapping_single_source_via_shadow_directory(tmp_path):
    """只覆盖一个源时，不需要复制其它适配器。"""
    from music_refill.sources import get_registry

    base = tmp_path / "base"
    override = tmp_path / "override"
    _write_adapter(base, "shared", "shared")
    _write_adapter(base, "other", "other")
    _write_adapter(override, "shared", "shared")

    load_sources(dirs=[base, override], probe=False, order=["shared"])
    registry = get_registry()
    assert set(registry) == {"shared", "other"}
