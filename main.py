"""DeepSeek 缓存命中率统计插件入口。

- 顶部 import cache_provider 触发 @register_provider_adapter 注册新 Provider 类型。
- 提供 /cache_stats (别名 /cs) 指令：总览 / 近期明细 / 按模型聚合 / 清空。
"""

from __future__ import annotations

import time
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star

try:  # 导入即注册 deepseek_cache_stats 适配器（包内相对导入）
    from . import cache_provider  # noqa: F401
except Exception:
    logger.exception("[DeepSeekCache] provider 模块加载失败")

try:
    from .stats_store import get_store
except Exception:
    logger.exception("[DeepSeekCache] stats_store 导入失败")
    get_store = None  # type: ignore[assignment]


def _fmt_summary(s: dict) -> str:
    denom = s["cache_hit"] + s["cache_miss"]
    lines = [
        "📊 DeepSeek 缓存命中率总览",
        f"调用次数: {s['calls']}",
        f"命中率  : {s['hit_rate'] * 100:.1f}%"
        + (f"  (hit={s['cache_hit']} / hit+miss={denom})" if denom else ""),
        f"缓存命中: {s['cache_hit']}   未命中: {s['cache_miss']}   写入: {s['cache_creation']}",
        f"输入token: {s['prompt_tokens']}   输出token: {s['completion_tokens']}",
        f"平均耗时: {s['avg_latency_ms']:.0f} ms",
    ]
    return "\n".join(lines)


def _fmt_by_model(rows: list[dict]) -> str:
    if not rows:
        return "暂无数据"
    out = ["📊 按模型聚合"]
    for r in rows:
        out.append(
            f"- {r['model']}: 调用 {r['calls']} 次，命中率 {r['hit_rate'] * 100:.1f}%"
            f" (hit={r['cache_hit']}/miss={r['cache_miss']})，平均 {r['avg_latency_ms']:.0f}ms",
        )
    return "\n".join(out)


def _fmt_recent(rows: list[dict]) -> str:
    if not rows:
        return "暂无明细"
    out = ["📋 最近明细 (新→旧)"]
    import time as _t

    for i, r in enumerate(rows, 1):
        denom = r["cache_hit"] + r["cache_miss"]
        hr = (r["cache_hit"] / denom * 100) if denom else 0.0
        when = _t.strftime("%m-%d %H:%M:%S", _t.localtime(r["ts"]))
        stream = "S" if r["is_stream"] else "-"
        out.append(
            f"{i}. [{when}] {r['model']} | hit {r['cache_hit']} miss {r['cache_miss']}"
            f" ({hr:.0f}%) | {r['latency_ms']:.0f}ms | {stream}"
            f" | sys={r.get('sys_prompt_hash') or '-'} hist={r.get('history_hash') or '-'}"
        )
    return "\n".join(out)


def _parse_window(arg) -> int:
    """解析时间窗参数为起始 ts；空或无法识别返回 0（不限）。
    支持: 1h/2h(小时)、3d(天)、today(今天0点)、YYYY-MM-DD。"""
    arg = (arg or "").strip().lower()
    if not arg:
        return 0
    now = time.time()
    if arg.endswith("h") and arg[:-1].isdigit():
        return int(now) - int(arg[:-1]) * 3600
    if arg.endswith("d") and arg[:-1].isdigit():
        return int(now) - int(arg[:-1]) * 86400
    if arg == "today":
        return int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    try:
        return int(datetime.strptime(arg, "%Y-%m-%d").timestamp())
    except ValueError:
        return 0


def _resolve_recent_args(a: str, b: str) -> tuple[int, int]:
    """解析 /cs recent 的两个位置参数为 (n, since_ts)。

    a 为数字时视作 n、b 视作时间窗；a 非数字时视作时间窗、n 取默认 10。
    """
    a = (a or "").strip()
    b = (b or "").strip()
    if a.isdigit():
        return int(a), _parse_window(b)
    return 10, _parse_window(a) or _parse_window(b)


def _resolve_threshold(s: str, default: float) -> float:
    """解析阈值参数为小数命中率。>1 视为百分比；空或无法解析返回 default。"""
    s = (s or "").strip()
    if not s:
        return default
    try:
        pct = float(s)
        return pct / 100 if pct > 1 else pct
    except ValueError:
        return default


def _fmt_miss(rows: list[dict], threshold: float) -> str:
    """低命中侦探视图：列出命中率低于阈值的调用及其前缀指纹。"""
    if not rows:
        return "✅ 该窗口内无低命中调用。"
    out = [f"🔎 低命中明细 (阈值 {threshold * 100:.0f}%)"]
    for i, r in enumerate(rows, 1):
        denom = r["cache_hit"] + r["cache_miss"]
        hr = (r["cache_hit"] / denom * 100) if denom else 0.0
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
        snip = f' | prompt="{r.get("prompt_snippet", "")}"' if r.get("prompt_snippet") else ""
        out.append(
            f"{i}. [{when}] {r['model']} | hit {r['cache_hit']}/miss {r['cache_miss']} "
            f"({hr:.0f}%) | sys={r.get('sys_prompt_hash') or '-'} hist={r.get('history_hash') or '-'}{snip}"
        )
    return "\n".join(out)


def _fmt_session_seq(rows: list[dict]) -> str:
    """会话内调用序列：显示序号与前缀指纹，标注系统提示词变化点。"""
    if not rows:
        return "无数据"
    out = ["📜 会话调用序列"]
    prev_sys = None
    for r in rows:
        denom = r["cache_hit"] + r["cache_miss"]
        hr = (r["cache_hit"] / denom * 100) if denom else 0.0
        when = time.strftime("%m-%d %H:%M:%S", time.localtime(r["ts"]))
        sys_h = r.get("sys_prompt_hash", "")
        mark = " ⚠系统提示词变化" if (prev_sys is not None and sys_h != prev_sys) else ""
        out.append(
            f"#{r['seq']} [{when}] hit {r['cache_hit']}/miss {r['cache_miss']} ({hr:.0f}%) "
            f"| sys={sys_h} hist={r.get('history_hash', '')}{mark}"
        )
        prev_sys = sys_h
    return "\n".join(out)


def _fmt_trend(rows: list[dict]) -> str:
    """时间趋势：按桶聚合命中率。"""
    if not rows:
        return "暂无数据"
    out = ["📈 命中率趋势"]
    for r in rows:
        out.append(f"- {r['bucket']}: 调用 {r['calls']}，命中率 {r['hit_rate'] * 100:.1f}%")
    return "\n".join(out)


def _fmt_recent_sessions(rows: list[dict]) -> str:
    """列出近期会话。"""
    if not rows:
        return "暂无会话"
    out = ["🗂️ 近期会话 (用 /cs session <id> 查看序列)"]
    for r in rows:
        when = time.strftime("%m-%d %H:%M", time.localtime(r["last_ts"]))
        out.append(f"- {r['session_id']}: {r['calls']} 次 @ {when}")
    return "\n".join(out)


@filter.command_group("cache_stats", alias={"cs", "缓存统计"})
def cache_stats(self):
    """DeepSeek 缓存命中率统计指令组。"""
    pass


class DeepSeekCacheStatsPlugin(Star):
    def __init__(self, context: Context, config) -> None:
        super().__init__(context)
        try:
            from . import config as cfgmod
            cfgmod.apply_config(config)
        except Exception:
            logger.exception("[DeepSeekCache] 应用插件配置失败，使用默认配置")

    @cache_stats.command("summary", alias={"总览"})
    async def cs_summary(self, event: AstrMessageEvent, window: str = ""):
        """总览（可带时间窗：/cs summary 1h）。"""
        if get_store is None:
            yield event.plain_result("❌ 统计模块未就绪，请检查日志。")
            return
        try:
            s = await get_store().summary(_parse_window(window))
            yield event.plain_result(_fmt_summary(s))
        except Exception as e:
            logger.exception("[DeepSeekCache] 指令处理失败")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @cache_stats.command("recent", alias={"明细", "最近"})
    async def cs_recent(self, event: AstrMessageEvent, a: str = "", b: str = ""):
        """最近明细（/cs recent [n] [窗] 或 /cs recent [窗]）。"""
        if get_store is None:
            yield event.plain_result("❌ 统计模块未就绪，请检查日志。")
            return
        try:
            n, win = _resolve_recent_args(a, b)
            rows = await get_store().recent(n, win)
            yield event.plain_result(_fmt_recent(rows))
        except Exception as e:
            logger.exception("[DeepSeekCache] 指令处理失败")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @cache_stats.command("model", alias={"模型"})
    async def cs_model(self, event: AstrMessageEvent, window: str = ""):
        """按模型聚合。"""
        if get_store is None:
            yield event.plain_result("❌ 统计模块未就绪，请检查日志。")
            return
        try:
            rows = await get_store().by_model(_parse_window(window))
            yield event.plain_result(_fmt_by_model(rows))
        except Exception as e:
            logger.exception("[DeepSeekCache] 指令处理失败")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @cache_stats.command("miss", alias={"低命中"})
    async def cs_miss(self, event: AstrMessageEvent, threshold: str = "", window: str = ""):
        """低命中侦探（/cs miss [阈值%] [窗]）。"""
        if get_store is None:
            yield event.plain_result("❌ 统计模块未就绪，请检查日志。")
            return
        try:
            from .config import get_runtime
            thr = _resolve_threshold(threshold, get_runtime().low_hit_threshold)
            rows = await get_store().miss(thr, limit=50, since_ts=_parse_window(window))
            yield event.plain_result(_fmt_miss(rows, thr))
        except Exception as e:
            logger.exception("[DeepSeekCache] 指令处理失败")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @cache_stats.command("session", alias={"会话"})
    async def cs_session(self, event: AstrMessageEvent, sid: str = ""):
        """会话调用序列（/cs session [id]；无 id 列出近期会话）。"""
        if get_store is None:
            yield event.plain_result("❌ 统计模块未就绪，请检查日志。")
            return
        try:
            if sid:
                rows = await get_store().session_seq(sid, limit=100)
                yield event.plain_result(_fmt_session_seq(rows))
            else:
                rows = await get_store().recent_sessions(limit=20)
                yield event.plain_result(_fmt_recent_sessions(rows))
        except Exception as e:
            logger.exception("[DeepSeekCache] 指令处理失败")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @cache_stats.command("trend", alias={"趋势"})
    async def cs_trend(self, event: AstrMessageEvent, bucket: str = "day", window: str = ""):
        """时间趋势（/cs trend [hour|day] [窗]）。"""
        if get_store is None:
            yield event.plain_result("❌ 统计模块未就绪，请检查日志。")
            return
        try:
            rows = await get_store().trend(bucket or "day", _parse_window(window))
            yield event.plain_result(_fmt_trend(rows))
        except Exception as e:
            logger.exception("[DeepSeekCache] 指令处理失败")
            yield event.plain_result(f"❌ 查询失败: {e}")

    @cache_stats.command("reset", alias={"清空", "重置"})
    async def cs_reset(self, event: AstrMessageEvent):
        """清空所有记录。"""
        if get_store is None:
            yield event.plain_result("❌ 统计模块未就绪，请检查日志。")
            return
        try:
            deleted = await get_store().reset()
            yield event.plain_result(f"🗑️ 已清空 {deleted} 条记录。")
        except Exception as e:
            logger.exception("[DeepSeekCache] 指令处理失败")
            yield event.plain_result(f"❌ 查询失败: {e}")
