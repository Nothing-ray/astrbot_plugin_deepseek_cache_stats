# astrbot_plugin_deepseek_cache_stats

统计 DeepSeek 上下文缓存命中率，并精准定位**哪一次调用拉低了缓存命中**。

以自定义 Provider 子类（`deepseek_cache_stats`）逐次记录每次 LLM 调用的 `prompt_cache_hit/miss/creation_tokens`，并计算**可缓存前缀指纹**，通过 `/cache_stats`（别名 `/cs`）指令组查询命中率、低命中明细、会话序列与时间趋势，便于分析与排查缓存失效。

## 功能特性

- **逐次记录**：缓存命中/未命中/写入 token、延迟、是否流式，分离 DeepSeek 原生字段
- **前缀指纹归因**：`sys_prompt_hash` + `history_hash`，定位「前缀在哪一次变了」
- **低命中侦探**：`/cs miss` 列出低命中率调用及其指纹
- **会话序列**：`/cs session` 查看单会话内调用序列与指纹变迁（标注系统提示词变化点）
- **时间趋势**：`/cs trend` 按小时/天聚合命中率
- **配置化**：WebUI 可视化配置（总开关、日志级别、摘要、阈值、留存、model 过滤）
- **自动告警**：命中率低于阈值时自动写 WARNING 日志（含指纹）
- **留存清理**：惰性按天数/条数清理，避免库无限增长
- **指令组**：WebUI 补全友好，支持中文别名

## 安装

1. 克隆到 AstrBot 插件目录：
   ```bash
   git clone https://github.com/Nothing-ray/astrbot_plugin_deepseek_cache_stats \
     ~/.astrbot/data/plugins/astrbot_plugin_deepseek_cache_stats
   ```
2. WebUI → 提供商管理 → 你的 DeepSeek 提供商 → 把 `type` 改为 `deepseek_cache_stats`（其余 api_base / key / model 配置不变）
3. WebUI → 插件管理 → 重载插件

> DeepSeek 的 `api_base=https://api.deepseek.com` 保持不变；本插件仅在其外层套一层统计，HTTP/重试/工具调用全部继承自基类，零侵入。

## 快速验证

安装后用 3 步确认插件工作：

1. WebUI 改 provider `type` 为 `deepseek_cache_stats` → 重载插件
2. 发任意一句对话给 DeepSeek
3. 发 `/cs summary` → 看到 `调用次数: 1` 即生效；再发一句 → 第二次应看到命中率上升（同前缀命中缓存）

> 若 `调用次数` 始终为 0，检查 `model_filter` 是否误填、日志级别是否掩盖了错误。

## 配置

WebUI → 插件管理 → 本插件 → 设置：

| 配置项 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 记录总开关，关闭后插件透明不落库 |
| `log_each_call` | `true` | 每次调用后在日志输出一行缓存命中摘要 |
| `log_level` | `info` | 逐次日志级别（debug/info/warning） |
| `store_snippet` | `false` | 是否额外存储 system/prompt 前 N 字摘要（隐私敏感，默认关） |
| `snippet_len` | `80` | 摘要截断长度 |
| `low_hit_threshold` | `0.5` | 低命中阈值，用于 `/cs miss` 默认过滤与自动告警；设为 `0` 关闭告警 |
| `retention_days` | `30` | 仅保留最近 N 天记录，`0` = 永久 |
| `retention_max_rows` | `0` | 仅保留最近 N 条记录，`0` = 不限 |
| `model_filter` | `[]` | 空 = 记录全部 model；非空 = 仅记录列表内的 model |

> 配置改动后经 WebUI「重载插件」生效。

### 配置调优建议

| 配置 | 怎么选 |
|---|---|
| `retention_days` | 默认 30 天 → 稳态约 3 万行（中度 1000 次/天），查询均 <150ms。**长期跑建议保持默认**；改 `0`（永久）会持续增长到百万行，届时裸 `/cs summary` 全量查询会变慢（可带时间窗缓解） |
| `retention_max_rows` | 一般用 `days` 即可；设硬上限会触发额外的清理子查询，非必要不设 |
| `low_hit_threshold` | 默认 0.5；追求高命中可设 0.8（更早暴露异常）；设 `0` 关闭自动告警 |
| `model_filter` | 多 provider 混用时只盯 DeepSeek，避免噪声；单 provider 留空 |
| `store_snippet` | 排查「哪段 prompt 导致前缀变化」时临时开（会落库 prompt 片段，隐私敏感），日常关 |
| `log_each_call` / `log_level` | 生产环境用 `info`/`warning`；排查问题时临时切 `debug` |

## 命令用法

`/cs`（别名 `/cache_stats`、`/缓存统计`）。单独输入 `/cs` 会打印子指令帮助树。

| 命令 | 说明 |
|---|---|
| `/cs summary [窗]` | 总览（调用数/命中率/平均耗时） |
| `/cs recent [n] [窗]` | 最近明细（带指纹） |
| `/cs model [窗]` | 按模型聚合 |
| `/cs miss [阈值%] [窗]` | 低命中侦探（核心排查视图） |
| `/cs session [id]` | 会话调用序列（无 id 列出近期会话） |
| `/cs trend [hour\|day] [窗]` | 时间趋势 |
| `/cs reset` | 清空记录 |

**时间窗语法**：`1h`（近 1 小时）、`2d`（近 2 天）、`today`（今天 0 点起）、`YYYY-MM-DD`。示例：`/cs recent 10 1h`、`/cs miss 30 today`。

### 命令输出样例

```
/cs summary
📊 DeepSeek 缓存命中率总览
调用次数: 156
命中率  : 87.3%  (hit=12000 / hit+miss=13750)
缓存命中: 12000   未命中: 1750   写入: 500
输入token: 250000   输出token: 18000
平均耗时: 1800 ms

/cs recent 2
📋 最近明细 (新→旧)
1. [07-08 10:30:15] deepseek-chat | hit 2304 miss 174 (93%) | 2204ms | S | sys=c0b39c6e hist=326d8bae
2. [07-08 10:25:03] deepseek-chat | hit 100 miss 2400 (4%) | 3100ms | S | sys=ab12cd34 hist=326d8bae

/cs model
📊 按模型聚合
- deepseek-chat: 调用 156 次，命中率 87.3% (hit=12000/miss=1750)，平均 1800ms

/cs miss 50
🔎 低命中明细 (阈值 50%)
1. [07-08 10:25:03] deepseek-chat | hit 100/miss 2400 (4%) | sys=ab12cd34 hist=326d8bae

/cs session <id>
📜 会话调用序列
#1 [07-08 10:20:00] hit 0/miss 1000 (0%) | sys=c0b39c6e hist=326d8bae
#2 [07-08 10:22:00] hit 950/miss 50 (95%) | sys=c0b39c6e hist=326d8bae
#3 [07-08 10:25:00] hit 100/miss 2400 (4%) | sys=ab12cd34 hist=... ⚠系统提示词变化

/cs session（无 id）
🗂️ 近期会话 (用 /cs session <id> 查看序列)
- abc123: 8 次 @ 07-08 10:30

/cs trend day
📈 命中率趋势
- 2026-07-07: 调用 120，命中率 89.0%
- 2026-07-08: 调用 156，命中率 87.3%

/cs reset
🗑️ 已清空 156 条记录。
```

> 样例中 `S`=流式、`-`=非流式；`⚠系统提示词变化` 是 `/cs session` 排查的关键线索。

## 缓存诊断原理

DeepSeek 按**最长公共前缀**缓存 token。每次调用记录两个指纹：

- `sys_prompt_hash`：系统提示词指纹（`sha1` 前 8 位）。从 `system_prompt` 参数**或 `contexts[0]`（role=system）**提取 —— AstrBot 的 agent runner 把系统提示词放进 `contexts` 而非参数，插件会自动补取（v0.2.0+ Fix D）。
- `history_hash`：`system_prompt + contexts`（即可缓存前缀）指纹。

排查低命中时，在同一会话内逐次比对（`/cs session <id>`）：

- `sys_prompt_hash` **变了** → 系统提示词漂移 → 整段缓存失效（头号元凶，会标 `⚠系统提示词变化`）
- `history_hash` **变了**但消息数相近 → 历史被重排/截断
- 指纹**稳定**却仍 miss → 缓存被淘汰，或前缀低于 1024 token 门槛（非配置问题）

> 注：`deepseek-chat` 前缀需 ≥ 1024 token 才会缓存；相同前缀第二次请求命中。

## 性能与留存

默认 `retention_days=30` 已是长期运行下最强的保护：

- **稳态规模**：按中度使用（~1000 次/天）约 3 万行、DB ~20MB，所有查询 <150ms
- **索引**：`idx_ts` / `idx_model` / `idx_session` 覆盖时间窗、按模型、按会话三类过滤
- **防碎片**：`PRAGMA auto_vacuum=FULL` 让 `_trim` 删除自动回收页，长期跑 db 文件不会因碎片膨胀
- **留存选择**：保持默认 30 天即可；若改永久留存（`retention_days=0`），数据增长到百万行后建议查询都带时间窗

> `auto_vacuum=FULL` 只对**空库**生效。老库升级到 v0.2.0+ 后需删一次 `cache_stats.db` 重启，新库才会启用（详见 commit 历史）。

## FAQ / 排查

**Q：hit_rate 一直是 0%？**
冷启动正常 —— 首次请求无缓存，同前缀的第二次请求才会命中。若持续 0%，用 `/cs session <id>` 检查 `sys_prompt_hash` 是否每次都变（系统提示词不稳定）。

**Q：hit_rate 突然下降？**
`/cs miss` 看低命中明细 → `/cs session <id>` 比对指纹变化点：`sys` 变 = 系统提示词漂移；`hist` 变 = 历史重排；都稳定却 miss = 缓存淘汰或前缀 <1024 token。

**Q：`sys_prompt_hash` 全是 `da39a3ee`？**
那是空串的 sha1。v0.2.0 早期有 bug（未从 contexts 提取 system），**Fix D 已修**。升级到 ≥0.2.0 并重载插件即恢复；若仍如此，说明该会话确实没有系统提示词。
  AstrBot ≥4.29 环境请升级到 ≥v0.2.1：v4.29 起 contexts 为 Message 对象（非 dict），旧版 `_extract_system` 无法提取。

**Q：缓存总是不命中？**
1) 确认可缓存前缀 ≥ 1024 token；2) 同会话内保持系统提示词与历史稳定；3) DeepSeek 缓存有时效，长间隔会被淘汰。

**Q：`/cs reset` 误删了数据？**
不可逆。日常靠 `retention_days` 软清理即可，一般不需要手动 reset。

## 兼容性

AstrBot `>= 4.26.3, < 5`

- **v0.2.1**：兼容 AstrBot 4.29（contexts 为 pydantic Message 对象）——`_ser` 加 `default=repr` 修复 `Object of type Message is not JSON serializable`（该异常导致统计不落库），`_extract_system` 兼容属性访问

## License

MIT
