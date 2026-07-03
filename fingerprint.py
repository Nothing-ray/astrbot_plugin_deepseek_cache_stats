"""缓存前缀指纹计算。

DeepSeek 按最长公共前缀缓存。history_hash 覆盖 system_prompt + contexts（即可缓存前缀），
用于诊断"哪一次调用改变了前缀导致缓存未命中"。本模块为纯 Python，不依赖 astrbot。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _h(s: str) -> str:
    """sha1 取前 8 位 hex。"""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


def _ser(contexts: Any) -> str:
    """稳定序列化 contexts 列表（排序键），保证相同内容相同顺序得到相同串。"""
    if not contexts:
        return ""
    return json.dumps(contexts, sort_keys=True, ensure_ascii=False)


def _extract_system(system_prompt: Any, contexts: Any) -> str:
    """提取系统提示词：优先 system_prompt 参数；为空则从 contexts 找 role=system。

    AstrBot 的 tool_loop_agent_runner 把 system prompt 作为 messages[0](role=system)
    放进 contexts，而非通过 text_chat 的 system_prompt 参数传递，故需从 contexts 补取，
    否则 sys_prompt_hash 恒为空串哈希（da39a3ee），无法检测系统提示词漂移。
    """
    if system_prompt:
        return system_prompt
    if isinstance(contexts, list):
        for m in contexts:
            if isinstance(m, dict) and m.get("role") == "system":
                content = m.get("content")
                if content:
                    return str(content)
    return ""


def fingerprint(system_prompt, contexts, prompt, *, store_snippet: bool, snippet_len: int) -> dict[str, Any]:
    """计算单次调用的缓存前缀指纹与规模信息。

    返回字段: sys_prompt_hash, history_hash, ctx_msg_count, prompt_char_len,
    sys_prompt_snippet, prompt_snippet（后两者仅在 store_snippet=True 时非空）。
    """
    sp = _extract_system(system_prompt, contexts)
    pm = prompt or ""
    ctx_list = contexts if isinstance(contexts, list) else []
    return {
        "sys_prompt_hash": _h(sp),
        "history_hash": _h(sp + _ser(ctx_list)),
        "ctx_msg_count": len(ctx_list),
        "prompt_char_len": len(pm),
        "sys_prompt_snippet": (sp[:snippet_len] if store_snippet else ""),
        "prompt_snippet": (pm[:snippet_len] if store_snippet else ""),
    }
