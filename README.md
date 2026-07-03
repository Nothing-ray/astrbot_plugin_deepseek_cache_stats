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

## 缓存诊断原理

DeepSeek 按**最长公共前缀**缓存 token。每次调用记录两个指纹：

- `sys_prompt_hash`：系统提示词指纹（`sha1` 前 8 位）
- `history_hash`：`system_prompt + contexts`（即可缓存前缀）指纹

排查低命中时，在同一会话内逐次比对：

- `sys_prompt_hash` **变了** → 系统提示词漂移 → 整段缓存失效（头号元凶）
- `history_hash` **变了**但消息数相近 → 历史被重排/截断
- 指纹**稳定**却仍 miss → 缓存被淘汰，或前缀低于 1024 token 门槛（非配置问题）

> 注：`deepseek-chat` 前缀需 ≥ 1024 token 才会缓存；相同前缀第二次请求命中。

## 兼容性

AstrBot `>= 4.26.3, < 5`

## License

MIT
