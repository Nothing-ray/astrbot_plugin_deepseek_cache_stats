"""运行时配置持有者。

Star 插件在 __init__ 调用 apply_config(插件配置) 把配置推入模块单例；
Provider 在每次调用时通过 get_runtime() 读取，保证重载后立即生效。
本模块为纯 Python，不依赖 astrbot，便于单元测试。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RuntimeConfig:
    enabled: bool = True
    log_each_call: bool = True
    log_level: str = "info"           # debug / info / warning
    store_snippet: bool = False
    snippet_len: int = 80
    low_hit_threshold: float = 0.5    # 0 = 关闭低命中告警
    retention_days: int = 30          # 0 = 永久
    retention_max_rows: int = 0       # 0 = 不限
    model_filter: list[str] = field(default_factory=list)
    schema_version: int = 1


_runtime: RuntimeConfig = RuntimeConfig()


def apply_config(cfg: dict[str, Any] | None) -> RuntimeConfig:
    """根据插件配置字典重建运行时单例并返回。缺失键使用默认值。"""
    global _runtime
    cfg = cfg or {}
    _runtime = RuntimeConfig(
        enabled=bool(cfg.get("enabled", True)),
        log_each_call=bool(cfg.get("log_each_call", True)),
        log_level=str(cfg.get("log_level", "info")),
        store_snippet=bool(cfg.get("store_snippet", False)),
        snippet_len=int(cfg.get("snippet_len", 80)),
        low_hit_threshold=float(cfg.get("low_hit_threshold", 0.5)),
        retention_days=int(cfg.get("retention_days", 30)),
        retention_max_rows=int(cfg.get("retention_max_rows", 0)),
        model_filter=list(cfg.get("model_filter", []) or []),
        schema_version=int(cfg.get("schema_version", 1)),
    )
    return _runtime


def get_runtime() -> RuntimeConfig:
    """返回当前运行时配置单例。"""
    return _runtime
