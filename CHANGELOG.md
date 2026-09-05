# 更新记录 (Changelog)

本项目的所有重要变更都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.3.0] - 2026-09-05

### Changed

- **感言机制重写为「对话式」**：
  - 感言不再是一段孤立的告别词，而是像平常聊天一样生成——读取当前会话的真实对话历史（最近 40 条）+ 当前人格 prompt
  - 手动触发 `OnLLMRequestEvent` 钩子，livingmemory 等聊天增强插件会正常注入记忆召回，感言因此"记得"之前的事
  - 生成后的问答写回会话历史，之后的正常聊天仍记得这次告别
- 注入模板重写：要求 bot 结合上下文像平常一样回应，最后自然收尾告别/重新开口，人格再傲娇也必须服从闭嘴（言行统一）

### Technical

- 感言请求使用 `conversation_manager` 读取/写回 conversation history（防御性截断最近 40 条）
- 通过 `call_event_hook(OnLLMRequestEvent, req)` 复用框架钩子分发机制；注意必须在 `stop_event()` 之前调用，否则钩子循环会在第一个 handler 后提前返回

## [1.2.0] - 2026-09-05

### Added

- **拦截方式双开关**：
  - `silent_intercept_enabled`（静默拦截）：闭嘴时 `stop_event()` 完全阻断，其他插件也收不到（默认开，优先级高）
  - `llm_intercept_enabled`（仅 LLM 拦截）：闭嘴时只禁止默认 LLM 回复，记忆学习等插件照常处理消息（默认开，静默拦截关闭时生效）
  - 两者都关 = 闭嘴仅改变状态、不拦截任何消息

### Technical

- 框架命名坑记录：`event.should_call_llm(True)` 才是禁止 LLM 请求（`call_llm` 默认 `False` 表示会调用）

## [1.1.0] - 2026-09-05

### Added

- **最后感言功能**：切换闭嘴/张嘴时，先切换拦截状态，再调用一次 LLM 让 bot 以人格口吻发出最后感言/重新开口感受
- 提示词注入模板：保证人格强硬/傲娇的 bot 也不会真的拒绝闭嘴，只会口头小抱怨后服从收尾
- 新配置项：`final_reply_enabled`、`silent_injection`、`resume_injection`、`silent_fallback_text`、`resume_fallback_text`

### Fixed

- 感言发送改用 `event.send()` 直发：`stop_event()` 后 pipeline 洋葱模型不再把 yield 结果送进 RespondStage，普通 yield 会被静默吞掉

### Technical

- `event.send()` 必须传 `MessageChain` 对象（`event.plain_result()` 返回值可直接传），不能传 `.chain` 裸组件 list
- LLM 调用设置 `request_max_retries=2`，避免第三方中转站 504 时按默认 5 次重试拖十几分钟

## [1.0.0] - 2026-09-05

### Added

- 首个版本：可配置的闭嘴/张嘴控制
- 目标人格关键词 + 触发词组合匹配，支持多个别名
- 单群（需目标关键词）与全局（无需关键词）两级控制
- 状态持久化到 `plugin_data/astrbot_plugin_quiet_mode/quiet_state.json`，重启不丢
- `priority=10000` 高优先级拦截，确保在群聊增强插件之前生效
- 管理员权限控制（`admin_only` + `admin_qqs`），非管理员指令静默丢弃
- 标准指令：`/quiet_status`、`/quiet_global_silent`、`/quiet_global_resume`、`/quiet_clear_groups`
