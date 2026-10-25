"""音源适配层：协议定义 + 注册表。真正的音源实现在 sources_local/（gitignored）。"""
from music_refill.sources.base import MusicSource, SourceTrack, register_source, get_registry
from music_refill.sources.loader import load_sources

__all__ = ["MusicSource", "SourceTrack", "register_source", "get_registry", "load_sources"]
