# 更新记录 (Changelog)

本项目的所有重要变更都将记录在此文件中。

格式基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [1.7.0] - 2026-09-10

### Added

- **规则兜底登记机器人（不依赖 AI 调工具）**：实测「告诉 bot 谁是 bot」并不可靠——群聊插件 `group_chat_plus` 默认关闭 `enable_tools_reminder`（模型拿不到任何工具文字提示），又会在 prompt 里写「请直接输出你的回复」、对 @ 了多人的消息额外注入「不一定只是在对你一个人说」，flash 级模型于是只顾聊天、根本不调用工具（生产日志实证：全日志 `使用工具：` 0 条，模型侧直连测试 tool_calls 正常）。
  现补一条确定性通路：**AstrBot 管理员**在群里 `@` 某人并说「他是机器人 / 是BOT」时，插件在群消息阶段直接把人写进 `bot_qq_list` 并持久化，完全不依赖模型是否调用工具；随后通过 `on_llm_request` 把「已登记」告知模型，避免回复口径与名单不一致
  - `bot_guard_auto_register`（默认 `true`）——规则兜底开关
  - `bot_guard_register_keywords`（默认 `["机器人","机娘","智能体","bot","ai"]`）——触发关键词；纯 ASCII 关键词按整词匹配（避免 `ai` 命中 `said/wait/email`）
  - `bot_guard_register_exclude`（默认含「不是机器人 / 取消标记 / 不要登记」等）——否定语境不登记
  - 一条消息 `@` 多个人时会**逐个登记**（此前 LLM 工具一次只能处理一个目标）

### Fixed

- **管理员口径修正**：登记机器人的权限判定此前写成「群管理员」语义，实际 `event.is_admin()` 在框架里只由 `admins_id`（AstrBot 全局管理员）决定，与 QQ 群角色无关；而默认 `admins_id` 是占位符 `["astrbot"]`，等于**没有任何人**有权登记（即使模型调用了工具也会被拒）。
  现改为明确以 **AstrBot 全局管理员**为准，并与插件 `admin_qqs` 白名单**取并集**（`admin_qqs` 的 schema 描述本来就是「与 AstrBot admin 权限并列」，旧代码却是覆盖语义，已一并修正 `_is_authorized`）
- 被拒提示与工具 docstring 由「仅群管理员」改为「仅 AstrBot 管理员」，避免误导

## [1.6.0] - 2026-09-10

### Added

- **对话式登记机器人（LLM 工具调用）**：群管理员不必再手改配置——在群里 `@` 对方说一句「他也是机器人」，LLM 即调用本插件注册的 `set_group_bot_member` 工具，把该 QQ 写入 `bot_qq_list` 并**回写插件配置持久化**，立即复用 bot_guard 的「回复他不引用 + 注入对方是 bot 提醒」。QQ 号优先从消息的 `At` 组件自动提取，无需手动报号
- **显式指令兜底**（模型抽风时可手动操作）：
  - `/标记bot @某人`（也支持 `/标记bot 123456789`）——标记为机器人
  - `/取消标记bot @某人`（也支持给 QQ 号）——取消标记
  - 不带目标时列出当前名单
- `/quiet_status` 增加登记机器人的用法提示

### Security

- `filter.llm_tool` 注册的工具**不带权限过滤器**，对所有群/私聊均可见，因此工具内部做了三重校验：① 严格管理员判定（`admin_qqs` 命中或 `event.is_admin()`，**不复用** `admin_only=False` 会放行的 `_is_authorized`）；② 必须为群聊上下文；③ 目标 QQ 不允许是 bot 自己（跳过 `At` 全员与 `At` 自己）。防止普通群友诱导 LLM 把真人误标成机器人（后果包括不再引用其消息、甚至被连续 bot 消息熔断波及）

## [1.5.1] - 2026-09-10

### Fixed

- **感言里的 `on_llm_request` 钩子链被截断**：闭嘴/张嘴感言、解禁感言此前均在 `stop_event()` 之后生成，而框架 `call_event_hook` 每执行完一个 handler 就检查 `event.is_stopped()`、为真立即中断（`context_utils.py:105`），且 handler 按 priority 降序执行（`star_handler.py:26`）——导致只有优先级最高的 `meme_manager`（99999）能跑到，livingmemory 的长期记忆召回注入不进来（生产日志实证：该中断日志次数与感言次数严格 1:1）
  - 现改为生成感言期间临时 `event.continue_event()`、`finally` 立即恢复停止状态，感言不再缺记忆召回
- **`lift_ban`（管理员提前解封）分支缺少手动闭嘴判断**：禁言期间管理员若又手动把该群设为「闭嘴」，解封时仍会发送解禁感言、违反手动指令。现与「自然到期」路径对齐：检测到手动闭嘴则只退出自动闭嘴、跳过感言（并记 INFO 日志）

### Technical

- 三条感言调用路径（手动切换、`lift_ban` 解禁、禁言到期）共用 `_conversational_farewell`，修复落在该函数内部，一处改动全路径生效

## [1.5.0] - 2026-09-10

### Added

- **禁言联动（mute_watcher）**：bot 被群管理员禁言时自动闭嘴，解禁时自动开口
  - 检测到 bot 自身被禁言（OneBot `group_ban` 事件，`sub_type=ban`）→ 自动进入该群**自动闭嘴**名单（独立于手动闭嘴、持久化、重启不丢），记录禁言到期时间
  - 禁言解除（管理员解除 `lift_ban` 事件，或自然到期由周期任务发现）→ 自动退出闭嘴，并复用 v1.3.0 的对话式感言机制发出「解禁感言」（读会话历史+人格、触发记忆召回）
  - 禁言期间管理员又手动闭嘴该群 → 只退出自动闭嘴、不发感言（尊重手动指令）
  - 无事件上下文（如重启后禁言已到期）时走兜底文案直发
  - 新配置：`mute_auto_quiet_enabled`、`unmute_farewell_enabled`、`unmute_injection`、`unmute_fallback_text`
  - `/quiet_status` 新增「禁言自动闭嘴」名单显示

### Technical

- 框架源码结论：aiocqhttp 适配器把 OneBot `notice` 事件转成 `GROUP_MESSAGE` 类型事件（`message_str` 为空、`raw_message` 保留原始 dict），会正常流进群消息监听器，可直接从 `event.message_obj.raw_message` 读 `post_type/notice_type/sub_type`
- 监听器 `priority=10001`，**高于闭嘴拦截的 10000**：否则解禁事件进来时会被静默拦截 `stop_event()` 吞掉，永远走不到解禁逻辑
- QQ 禁言**自然到期不产生 `lift_ban` 事件**，故用「持久化到期时间戳 + 每 30s 周期检查任务」兜底；检查任务在 `terminate()` 中随插件卸载取消
- `mute_watcher` 与 `bot_guard` 监听器均对 notice 类事件 `stop_event()`，避免空消息流进群聊决策插件

## [1.4.0] - 2026-09-10

### Added

- **Bot 防互引用循环（bot_guard）**：解决两个都开启「回复时引用对话」的 bot 在群里无限互相对话的问题
  - 把其他 bot 的 QQ 号填入 `bot_qq_list` 即启用；回复名单内 bot 时**不引用对方消息**，回复普通群友的引用行为完全不受影响
  - 同时通过 `on_llm_request` 钩子向 LLM 注入「对方也是 bot，不要无限对谈」的提醒（模板可自定义，支持 `{sender_name}`/`{sender_id}`/`{group_id}` 占位符）
  - 新配置：`bot_guard_enabled`、`bot_qq_list`、`bot_reply_mode`（`no_quote` 回复但不引用 / `ignore` 完全无视）、`bot_guard_reminder`、`bot_guard_max_rounds`（连续 N 条 bot 消息无人插话后熔断，0=不限制）
  - `/quiet_status` 新增 bot_guard 状态显示

### Technical

- 框架源码结论：引用回复由 `ResultDecorateStage` 在**所有 `on_decorating_result` 钩子执行完之后**统一插入（`chain.insert(0, Reply(id=...))`），且只对「全为 Plain/Image」的消息链生效——插件在钩子里删 `Reply` 组件是**无效**的
- 免引用实现：在 `on_decorating_result` 钩子里**自发送回复**（顺带剥离指向该 bot 的 `Reply`/`At` 组件）→ `event.clear_result()` → 框架 RespondStage 发现无结果而跳过，不会再发一遍（也不会再插引用）
- 自发送绕过 RespondStage，故手动 `call_event_hook(OnAfterMessageSentEvent)` 补发收尾钩子，`self_learning` 等依赖发送后事件的插件行为保持一致
- NapCat 源码结论：出站消息段类型未知时 `createSendElements` 会直接 throw——**不存在"隐形消息段"**，因此放弃"塞未知组件骗过 can_decorate"的方案
- 提醒注入钩子用 `priority=-9999` 保证最后执行，追加在 system_prompt 末尾，避免被其他插件覆盖

## [1.3.1] - 2026-09-05

### Added

- **插件图标**：新增 `logo.png`（AstrBot 框架自动检测插件目录下的 `logo.png`，WebUI / 插件市场展示用），并提供 512×512 优化版（442 KB）
- **平台支持标注**：`metadata.yaml` 新增 `support_platforms` 字段，显式声明适配全部 18 个平台适配器（aiocqhttp / QQ 官方 / 微信系 / Telegram / Discord / Slack / KOOK / LINE / 钉钉 / 飞书 / Satori / WebChat 等）——插件仅使用通用事件 API，与平台无关

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
