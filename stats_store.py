"""缓存命中率统计的持久化层（SQLite via aiosqlite）。

表 cache_call_log 每行对应一次 LLM 调用，字段分离 DeepSeek 原生
prompt_cache_hit_tokens / prompt_cache_miss_tokens / prompt_cache_creation_tokens，
便于按时间/模型/会话做命中率分析与排查。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_deepseek_cache_stats"
DB_FILENAME = "cache_stats.db"

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS cache_call_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    provider_id TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    session_id TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cache_hit INTEGER NOT NULL DEFAULT 0,
    cache_miss INTEGER NOT NULL DEFAULT 0,
    cache_creation INTEGER NOT NULL DEFAULT 0,
    latency_ms REAL NOT NULL DEFAULT 0,
    is_stream INTEGER NOT NULL DEFAULT 0,
    sys_prompt_hash TEXT NOT NULL DEFAULT '',
    history_hash TEXT NOT NULL DEFAULT '',
    ctx_msg_count INTEGER NOT NULL DEFAULT 0,
    prompt_char_len INTEGER NOT NULL DEFAULT 0,
    sys_prompt_snippet TEXT NOT NULL DEFAULT '',
    prompt_snippet TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_cache_call_log_ts ON cache_call_log(ts);
CREATE INDEX IF NOT EXISTS idx_cache_call_log_model ON cache_call_log(model);
"""

_NEW_COLUMNS = ["sys_prompt_hash", "history_hash", "ctx_msg_count",
                "prompt_char_len", "sys_prompt_snippet", "prompt_snippet"]


class CacheStatsStore:
    """单连接 + 协程锁的轻量统计库。所有写操作串行化，读操作并发安全。"""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._init_lock = asyncio.Lock()

    async def _ensure(self) -> aiosqlite.Connection:
        if self._conn is not None:
            return self._conn
        async with self._init_lock:
            if self._conn is None:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = await aiosqlite.connect(str(self.db_path))
                await conn.executescript(_CREATE_SQL)
                await conn.commit()
                await self._migrate(conn)
                self._conn = conn
                logger.info("[DeepSeekCache] 统计库就绪: %s", self.db_path)
        assert self._conn is not None
        return self._conn

    async def _migrate(self, conn: aiosqlite.Connection) -> None:
        """补齐新增列（旧库平滑升级），幂等。"""
        cur = await conn.execute("PRAGMA table_info(cache_call_log)")
        existing = {row[1] for row in await cur.fetchall()}
        text_cols = {"sys_prompt_hash", "history_hash", "sys_prompt_snippet", "prompt_snippet"}
        defaults = {"sys_prompt_hash": "''", "history_hash": "''", "ctx_msg_count": "0",
                    "prompt_char_len": "0", "sys_prompt_snippet": "''", "prompt_snippet": "''"}
        for col in _NEW_COLUMNS:
            if col not in existing:
                col_type = "TEXT" if col in text_cols else "INTEGER"
                await conn.execute(
                    f"ALTER TABLE cache_call_log ADD COLUMN {col} {col_type} NOT NULL DEFAULT {defaults[col]}"
                )
        await conn.commit()
        await conn.execute("PRAGMA user_version = 1")

    async def insert(self, record: dict[str, Any]) -> None:
        conn = await self._ensure()
        full: dict[str, Any] = {
            "sys_prompt_hash": "", "history_hash": "", "ctx_msg_count": 0,
            "prompt_char_len": 0, "sys_prompt_snippet": "", "prompt_snippet": "",
        }
        full.update(record)
        await conn.execute(
            """INSERT INTO cache_call_log
               (ts, provider_id, model, session_id, prompt_tokens, completion_tokens,
                cache_hit, cache_miss, cache_creation, latency_ms, is_stream,
                sys_prompt_hash, history_hash, ctx_msg_count, prompt_char_len,
                sys_prompt_snippet, prompt_snippet)
               VALUES (:ts, :provider_id, :model, :session_id, :prompt_tokens,
                       :completion_tokens, :cache_hit, :cache_miss, :cache_creation,
                       :latency_ms, :is_stream, :sys_prompt_hash, :history_hash,
                       :ctx_msg_count, :prompt_char_len, :sys_prompt_snippet, :prompt_snippet)""",
            full,
        )
        await conn.commit()

    async def summary(self, since_ts: int = 0) -> dict[str, Any]:
        """汇总统计；当 since_ts>0 时只统计 ts>=since_ts 的行。"""
        conn = await self._ensure()
        where = "WHERE ts >= :since" if since_ts > 0 else ""
        async with conn.execute(
            f"""SELECT COUNT(*),
                       COALESCE(SUM(prompt_tokens), 0),
                       COALESCE(SUM(completion_tokens), 0),
                       COALESCE(SUM(cache_hit), 0),
                       COALESCE(SUM(cache_miss), 0),
                       COALESCE(SUM(cache_creation), 0),
                       COALESCE(AVG(latency_ms), 0)
                FROM cache_call_log {where}""",
            {"since": since_ts},
        ) as cur:
            row = await cur.fetchone()
        calls, prompt_tok, comp_tok, hit, miss, creation, avg_lat = row
        denom = hit + miss
        hit_rate = (hit / denom) if denom > 0 else 0.0
        return {
            "calls": calls or 0,
            "prompt_tokens": prompt_tok or 0,
            "completion_tokens": comp_tok or 0,
            "cache_hit": hit or 0,
            "cache_miss": miss or 0,
            "cache_creation": creation or 0,
            "hit_rate": hit_rate,
            "avg_latency_ms": round(avg_lat or 0, 1),
        }

    async def recent(self, n: int = 10, since_ts: int = 0) -> list[dict[str, Any]]:
        """最近 N 条调用（新→旧）；当 since_ts>0 时仅返回 ts>=since_ts 的行。

        返回的字典包含 6 个指纹/片段列，便于排查缓存失效。
        """
        conn = await self._ensure()
        where = "WHERE ts >= :since" if since_ts > 0 else ""
        async with conn.execute(
            f"""SELECT ts, model, prompt_tokens, completion_tokens, cache_hit,
                       cache_miss, cache_creation, latency_ms, is_stream, session_id,
                       sys_prompt_hash, history_hash, ctx_msg_count, prompt_char_len,
                       sys_prompt_snippet, prompt_snippet
                FROM cache_call_log {where} ORDER BY id DESC LIMIT :lim""",
            {"since": since_ts, "lim": max(1, min(int(n), 500))},
        ) as cur:
            rows = await cur.fetchall()
        cols = [
            "ts", "model", "prompt_tokens", "completion_tokens", "cache_hit",
            "cache_miss", "cache_creation", "latency_ms", "is_stream", "session_id",
            "sys_prompt_hash", "history_hash", "ctx_msg_count", "prompt_char_len",
            "sys_prompt_snippet", "prompt_snippet",
        ]
        return [dict(zip(cols, r)) for r in rows]

    async def by_model(self, since_ts: int = 0) -> list[dict[str, Any]]:
        """按模型聚合；当 since_ts>0 时只统计 ts>=since_ts 的行。"""
        conn = await self._ensure()
        where = "WHERE ts >= :since" if since_ts > 0 else ""
        async with conn.execute(
            f"""SELECT model, COUNT(*),
                       COALESCE(SUM(cache_hit), 0),
                       COALESCE(SUM(cache_miss), 0),
                       COALESCE(SUM(prompt_tokens), 0),
                       COALESCE(AVG(latency_ms), 0)
                FROM cache_call_log {where} GROUP BY model ORDER BY COUNT(*) DESC""",
            {"since": since_ts},
        ) as cur:
            rows = await cur.fetchall()
        out: list[dict[str, Any]] = []
        for model, calls, hit, miss, ptok, avg_lat in rows:
            denom = hit + miss
            hr = (hit / denom) if denom > 0 else 0.0
            out.append({
                "model": model,
                "calls": calls,
                "cache_hit": hit,
                "cache_miss": miss,
                "prompt_tokens": ptok,
                "hit_rate": hr,
                "avg_latency_ms": round(avg_lat or 0, 1),
            })
        return out

    async def miss(
        self, threshold: float, limit: int = 50, since_ts: int = 0
    ) -> list[dict[str, Any]]:
        """命中率低于 threshold 的调用（新→旧）；排除 hit+miss==0 的冷启动调用。

        threshold 为 [0,1] 的命中率比值，如 0.5 表示低于 50% 命中。
        """
        conn = await self._ensure()
        lim = max(1, min(int(limit), 500))
        sql = """
            SELECT ts, model, session_id, prompt_tokens, completion_tokens,
                   cache_hit, cache_miss, cache_creation, latency_ms, is_stream,
                   sys_prompt_hash, history_hash, ctx_msg_count, prompt_char_len,
                   sys_prompt_snippet, prompt_snippet
            FROM cache_call_log
            WHERE (cache_hit + cache_miss) > 0
              AND (cache_hit * 1.0 / (cache_hit + cache_miss)) < :thr
              AND ts >= :since
            ORDER BY id DESC LIMIT :lim
        """
        async with conn.execute(
            sql, {"thr": threshold, "since": since_ts, "lim": lim}
        ) as cur:
            rows = await cur.fetchall()
        cols = [
            "ts", "model", "session_id", "prompt_tokens", "completion_tokens",
            "cache_hit", "cache_miss", "cache_creation", "latency_ms", "is_stream",
            "sys_prompt_hash", "history_hash", "ctx_msg_count", "prompt_char_len",
            "sys_prompt_snippet", "prompt_snippet",
        ]
        return [dict(zip(cols, r)) for r in rows]

    async def session_seq(self, session_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """单会话的调用序列（按 id 升序），含 1 起始的 seq 序号（窗口函数）。"""
        conn = await self._ensure()
        lim = max(1, min(int(limit), 500))
        sql = """
            SELECT id, ts, model, cache_hit, cache_miss, cache_creation,
                   sys_prompt_hash, history_hash,
                   ROW_NUMBER() OVER (PARTITION BY session_id ORDER BY id) AS seq
            FROM cache_call_log
            WHERE session_id = :sid
            ORDER BY id LIMIT :lim
        """
        async with conn.execute(sql, {"sid": session_id, "lim": lim}) as cur:
            rows = await cur.fetchall()
        cols = ["id", "ts", "model", "cache_hit", "cache_miss", "cache_creation",
                "sys_prompt_hash", "history_hash", "seq"]
        return [dict(zip(cols, r)) for r in rows]

    async def recent_sessions(self, limit: int = 20) -> list[dict[str, Any]]:
        """非空会话列表（最近活跃在前），含调用次数。"""
        conn = await self._ensure()
        lim = max(1, min(int(limit), 500))
        sql = """
            SELECT session_id, MAX(ts) AS last_ts, COUNT(*) AS calls
            FROM cache_call_log WHERE session_id != ''
            GROUP BY session_id ORDER BY last_ts DESC LIMIT :lim
        """
        async with conn.execute(sql, {"lim": lim}) as cur:
            rows = await cur.fetchall()
        return [{"session_id": r[0], "last_ts": r[1], "calls": r[2]} for r in rows]

    async def trend(self, bucket: str = "day", since_ts: int = 0) -> list[dict[str, Any]]:
        """按时间桶聚合命中趋势；bucket ∈ {"hour","day"}，无法识别时按 day。

        时间用本地时区格式化。返回每桶的 calls/hit/miss/hit_rate。
        """
        conn = await self._ensure()
        if bucket == "hour":
            fmt = "strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime')"
        else:
            fmt = "strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime')"
        sql = f"""
            SELECT {fmt} AS bucket, COUNT(*) AS calls,
                   COALESCE(SUM(cache_hit), 0) AS hit,
                   COALESCE(SUM(cache_miss), 0) AS miss,
                   CASE WHEN SUM(cache_hit) + SUM(cache_miss) > 0
                        THEN SUM(cache_hit) * 1.0 / (SUM(cache_hit) + SUM(cache_miss))
                        ELSE 0 END AS hit_rate
            FROM cache_call_log WHERE ts >= :since
            GROUP BY bucket ORDER BY bucket
        """
        async with conn.execute(sql, {"since": since_ts}) as cur:
            rows = await cur.fetchall()
        return [{"bucket": r[0], "calls": r[1], "hit": r[2], "miss": r[3],
                 "hit_rate": r[4]} for r in rows]

    async def _trim(self, now_ts: int, retention_days: int, max_rows: int) -> int:
        """保留期清理（与配置解耦，由调用方传值）。返回被删除的行数。

        - retention_days>0：删除 ts 早于 now_ts - retention_days*86400 的行；
        - max_rows>0：仅保留最新的 max_rows 行，其余删除。
        两条规则按顺序叠加执行。
        """
        conn = await self._ensure()
        deleted = 0
        if retention_days > 0:
            cur = await conn.execute(
                "DELETE FROM cache_call_log WHERE ts < :cutoff",
                {"cutoff": now_ts - retention_days * 86400},
            )
            deleted += cur.rowcount or 0
        if max_rows > 0:
            cur = await conn.execute(
                "DELETE FROM cache_call_log WHERE id NOT IN "
                "(SELECT id FROM cache_call_log ORDER BY id DESC LIMIT :m)",
                {"m": max_rows},
            )
            deleted += cur.rowcount or 0
        await conn.commit()
        return deleted

    async def reset(self) -> int:
        conn = await self._ensure()
        async with conn.execute("SELECT COUNT(*) FROM cache_call_log") as cur:
            n = (await cur.fetchone())[0]
        await conn.execute("DELETE FROM cache_call_log")
        await conn.execute(
            "DELETE FROM sqlite_sequence WHERE name='cache_call_log'",
        )
        await conn.commit()
        return n or 0


_store: CacheStatsStore | None = None


def get_store() -> CacheStatsStore:
    """返回进程级单例；库位于 data/plugin_data/<插件名>/cache_stats.db。"""
    global _store
    if _store is None:
        db_path = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / DB_FILENAME
        _store = CacheStatsStore(db_path)
    return _store
