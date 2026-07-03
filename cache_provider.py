"""DeepSeek 缓存命中率统计 Provider。

以 ProviderOpenAIOfficial 子类的方式注册新适配器类型 ``deepseek_cache_stats``。
用户把 DeepSeek 提供商的 ``type`` 改为 ``deepseek_cache_stats`` 即可，其余
api_base / key / model 配置不变。

设计要点：
- 只重写 text_chat / text_chat_stream（注入会话、计时、流式标记）与
  _parse_openai_completion（流式/非流式唯一的完成解析汇聚点）。
- HTTP / SSE / 重试 / 工具调用等全部继承自基类，零侵入。
- 在 _parse_openai_completion 处读取原始 completion.usage 的 DeepSeek 原生字段
  prompt_cache_hit_tokens / prompt_cache_miss_tokens / prompt_cache_creation_tokens，
  分离落库，任何异常都吞掉，绝不影响主对话流程。
"""

from __future__ import annotations

import contextvars
import time
from typing import Any, AsyncGenerator

from astrbot.api import logger
from astrbot.core.provider.entities import LLMResponse, ProviderType
from astrbot.core.provider.register import (
    provider_cls_map,
    provider_registry,
    register_provider_adapter,
)
from astrbot.core.provider.sources.openai_source import ProviderOpenAIOfficial

try:  # 运行时配置类型（纯 Python 模块，无 astrbot 依赖）
    from .config import RuntimeConfig
except ImportError:
    from config import RuntimeConfig  # type: ignore[no-redef]

try:  # 兼容包内导入与目录直接导入两种加载方式
    from .stats_store import get_store
except ImportError:
    from stats_store import get_store  # type: ignore[no-redef]

try:  # 指纹计算（纯函数模块）
    from .fingerprint import fingerprint
except ImportError:
    from fingerprint import fingerprint  # type: ignore[no-redef]

try:  # 运行时配置读取（纯 Python 模块，无 astrbot 依赖）
    from .config import get_runtime
except ImportError:
    from config import get_runtime  # type: ignore[no-redef]

# 协程上下文：透传每次调用的会话、起始时间、是否流式，供 _parse_openai_completion 记录
_sess_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "dcs_session", default="",
)
_t0_var: contextvars.ContextVar[float] = contextvars.ContextVar(
    "dcs_t0", default=0.0,
)
_stream_var: contextvars.ContextVar[int] = contextvars.ContextVar(
    "dcs_stream", default=0,
)
_sys_var: contextvars.ContextVar = contextvars.ContextVar("dcs_sys", default="")
_ctx_var: contextvars.ContextVar = contextvars.ContextVar("dcs_ctx", default=None)
_prompt_var: contextvars.ContextVar = contextvars.ContextVar("dcs_prompt", default="")


def _read_int(usage: Any, name: str, default: int = 0) -> int:
    """从原始 usage 读取整数字段，兼容 pydantic 属性 / model_extra / dict。"""
    val = getattr(usage, name, None)
    if val is None:
        extra = getattr(usage, "model_extra", None) or {}
        val = extra.get(name)
    try:
        return int(val) if val is not None else default
    except (TypeError, ValueError):
        return default


def should_alert(hit: int, miss: int, threshold: float) -> bool:
    """判断当前命中率是否触发低命中告警。

    - threshold <= 0：告警关闭，恒返回 False。
    - hit + miss <= 0：无分母（无请求或全 0），恒返回 False。
    - 否则：命中率 = hit / (hit + miss)，低于 threshold 时返回 True。
    """
    if threshold <= 0:
        return False
    denom = hit + miss
    if denom <= 0:
        return False
    return (hit / denom) < threshold


def build_record(
    completion: Any,
    *,
    provider_id: str,
    model: str,
    session_id: str,
    is_stream: int,
    t0: float,
    system_prompt: Any,
    contexts: Any,
    prompt: Any,
    cfg: RuntimeConfig,
) -> dict | None:
    """从一次 LLM 完成结果构建落库记录（纯函数，无副作用）。

    读取 DeepSeek 原生 usage 字段（prompt_cache_hit_tokens /
    prompt_cache_miss_tokens / prompt_cache_creation_tokens）以及
    prompt_tokens / completion_tokens，结合指纹与计时信息组装一条记录字典。

    返回 None 的情况（按顺序短路）：
    1. completion.usage 为 None；
    2. 配置未启用（cfg.enabled 为假）；
    3. 配置了 model_filter 且当前 model 不在白名单内。
    """
    usage = getattr(completion, "usage", None)
    if usage is None:
        return None
    if not cfg.enabled:
        return None
    if cfg.model_filter and model not in cfg.model_filter:
        return None

    cache_hit = _read_int(usage, "prompt_cache_hit_tokens")
    cache_miss = _read_int(usage, "prompt_cache_miss_tokens")
    cache_creation = _read_int(usage, "prompt_cache_creation_tokens")
    prompt_tokens = _read_int(usage, "prompt_tokens")
    completion_tokens = _read_int(usage, "completion_tokens")

    fp = fingerprint(
        system_prompt,
        contexts,
        prompt,
        store_snippet=cfg.store_snippet,
        snippet_len=cfg.snippet_len,
    )

    latency = (time.perf_counter() - t0) * 1000 if t0 else 0.0

    return {
        "ts": int(time.time()),
        "provider_id": provider_id,
        "model": model,
        "session_id": session_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cache_hit": cache_hit,
        "cache_miss": cache_miss,
        "cache_creation": cache_creation,
        "latency_ms": round(latency, 1),
        "is_stream": int(is_stream),
        **fp,
    }


def _log_call(record: dict, level: str) -> None:
    """按配置级别输出单次调用日志。"""
    denom = record["cache_hit"] + record["cache_miss"]
    hit_rate = (record["cache_hit"] / denom * 100) if denom > 0 else 0.0
    msg = ("[DeepSeekCache] model=%s hit=%d miss=%d creation=%d hit_rate=%.1f%% "
           "latency=%.0fms prompt=%d output=%d stream=%d sys=%s hist=%s")
    args = (record["model"], record["cache_hit"], record["cache_miss"], record["cache_creation"],
            hit_rate, record["latency_ms"], record["prompt_tokens"], record["completion_tokens"],
            record["is_stream"], record["sys_prompt_hash"], record["history_hash"])
    fn = {"debug": logger.debug, "warning": logger.warning}.get(level, logger.info)
    fn(msg, *args)


# 留存清理：惰性触发，每 _TRIM_INTERVAL 次调用清一次，避免后台任务依赖 event loop
_trim_counter = 0
_TRIM_INTERVAL = 50


async def _maybe_trim(cfg) -> None:
    """惰性留存清理：按 cfg.retention_days / retention_max_rows 清理旧记录。"""
    global _trim_counter
    _trim_counter += 1
    if _trim_counter < _TRIM_INTERVAL:
        return
    _trim_counter = 0
    if cfg.retention_days <= 0 and cfg.retention_max_rows <= 0:
        return
    try:
        await get_store()._trim(int(time.time()), cfg.retention_days, cfg.retention_max_rows)
    except Exception:
        logger.debug("[DeepSeekCache] 留存清理跳过（异常）")


# 热重载幂等：清理可能的旧注册，避免装饰器因重名抛 ValueError 导致新类不生效
provider_cls_map.pop("deepseek_cache_stats", None)
provider_registry[:] = [p for p in provider_registry if p.type != "deepseek_cache_stats"]


@register_provider_adapter(
    "deepseek_cache_stats",
    "DeepSeek (缓存命中率统计)",
    provider_type=ProviderType.CHAT_COMPLETION,
)
class DeepSeekCacheProvider(ProviderOpenAIOfficial):
    """记录 DeepSeek 缓存用量的 ProviderOpenAIOfficial 子类。"""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)

    async def text_chat(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        extra_user_content_parts=None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> LLMResponse:
        _sess_var.set(session_id or "")
        _t0_var.set(time.perf_counter())
        _stream_var.set(0)
        _sys_var.set(system_prompt or "")
        _ctx_var.set(contexts)
        _prompt_var.set(prompt or "")
        return await super().text_chat(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            extra_user_content_parts=extra_user_content_parts,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            **kwargs,
        )

    async def text_chat_stream(
        self,
        prompt=None,
        session_id=None,
        image_urls=None,
        audio_urls=None,
        func_tool=None,
        contexts=None,
        system_prompt=None,
        tool_calls_result=None,
        model=None,
        tool_choice: str = "auto",
        request_max_retries: int | None = None,
        **kwargs,
    ) -> AsyncGenerator[LLMResponse, None]:
        _sess_var.set(session_id or "")
        _t0_var.set(time.perf_counter())
        _stream_var.set(1)
        _sys_var.set(system_prompt or "")
        _ctx_var.set(contexts)
        _prompt_var.set(prompt or "")
        async for chunk in super().text_chat_stream(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            **kwargs,
        ):
            yield chunk

    async def _parse_openai_completion(self, completion, tools):
        """流式/非流式汇聚点：用 build_record 构建记录并落库/告警（任何异常都不影响主流程）。"""
        resp = await super()._parse_openai_completion(completion, tools)
        try:
            cfg = get_runtime()
            model = (getattr(completion, "model", None)
                     or self.provider_config.get("model", "") or "")
            record = build_record(
                completion,
                provider_id=str(self.provider_config.get("id", "")),
                model=model,
                session_id=_sess_var.get(),
                is_stream=_stream_var.get(),
                t0=_t0_var.get(),
                system_prompt=_sys_var.get(),
                contexts=_ctx_var.get(),
                prompt=_prompt_var.get(),
                cfg=cfg,
            )
            if record is None:
                return resp

            # 原生字段挂 resp.extra，兼容其它插件/钩子
            denom = record["cache_hit"] + record["cache_miss"]
            hit_rate = (record["cache_hit"] / denom) if denom > 0 else 0.0
            try:
                extra = dict(getattr(resp, "extra", None) or {})
                extra["deepseek_cache"] = {
                    "cache_hit": record["cache_hit"],
                    "cache_miss": record["cache_miss"],
                    "cache_creation": record["cache_creation"],
                    "hit_rate": hit_rate,
                }
                resp.extra = extra
            except Exception:
                pass

            await get_store().insert(record)

            await _maybe_trim(cfg)

            if cfg.log_each_call:
                _log_call(record, cfg.log_level)

            if should_alert(record["cache_hit"], record["cache_miss"], cfg.low_hit_threshold):
                logger.warning(
                    "[DeepSeekCache] 低命中 model=%s hit_rate=%.1f%% sys_hash=%s hist_hash=%s",
                    record["model"], hit_rate * 100,
                    record["sys_prompt_hash"], record["history_hash"],
                )
        except Exception as e:
            logger.warning("[DeepSeekCache] 记录缓存用量失败，不影响主流程: %s", e)
        return resp
