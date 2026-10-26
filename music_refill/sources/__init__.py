"""音源适配层：协议定义 + 注册表。适配器默认从 sources_local/ 动态加载。"""
from music_refill.sources.base import MusicSource, SourceTrack, register_source, get_registry
from music_refill.sources.loader import load_sources

__all__ = ["MusicSource", "SourceTrack", "register_source", "get_registry", "load_sources"]
