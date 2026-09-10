# astrbot_plugin_quiet_mode

<p align="center">
  <img src="logo.png" alt="闭嘴模式" width="180"/>
</p>

一个 AstrBot 闭嘴/张嘴控制插件：让 bot 像平常对话一样告别，然后真的闭嘴；张嘴时对闭嘴期间发生的事做出真实反应。

## 特性

- 🔇 **可配置触发词**：`沙绫闭嘴`、`saaya shut up`、`独角兽别说话`……目标关键词 + 触发词自由组合，支持多个别名
- 🌐 **单群 / 全局两级控制**：只让某个群安静，或一键全群闭嘴
- 💬 **对话式感言**：切换状态时，bot 会像平常聊天一样做出反应——
  - 读取当前会话的真实对话历史
  - 注入当前人格 prompt，口吻与平时完全一致
  - 触发 `OnLLMRequestEvent` 钩子，**livingmemory 等聊天增强插件会正常注入记忆召回**
  - 温柔的 bot 会结合上下文做最后的嘱托；傲娇的 bot 会嘟囔着"好好好，我闭嘴总行了吧"然后服从——提示词注入保证人格再强硬也**言行统一**
  - 感言问答写回会话历史，之后的正常聊天仍然记得这次告别
- 🎛 **拦截方式可配置**：
  - **静默拦截**：闭嘴后本群消息完全阻断（其他插件也收不到）
  - **仅 LLM 拦截**：只禁止 AI 回复，记忆学习等插件照常工作——闭嘴期间的事 bot 都"看在眼里"，张嘴时能真实反应
- 💾 **状态持久化**：重启容器/进程不丢失闭嘴状态
- 🛡 **管理员权限控制**：仅管理员可切换，普通用户的指令静默丢弃
- 🤖 **Bot 防互引用循环**：群里两个都开「回复时引用」的 bot 会无限互相对话——把对方 bot 的 QQ 号填进配置，本 bot 回复它时**不引用对方消息**，并提醒 LLM「对方也是 bot，别无限对谈」；回复普通群友的引用行为不受影响
- 🚫 **禁言联动**：检测到 bot 自己被群管理员禁言时**自动进入该群闭嘴模式**（独立于手动闭嘴，持久化）；禁言被解除或自然到期时自动退出闭嘴，并像平常聊天一样发出「解禁感言」

## 安装

将本仓库克隆或下载到 AstrBot 的插件目录：

```bash
cd AstrBot/data/plugins
git clone https://github.com/cloneselfish-wq/astrbot_plugin_quiet_mode.git
```

然后在 AstrBot WebUI 中重载/重启即可加载。

## 使用

在群里发送普通消息即可（无需指令前缀），默认配置：

| 示例 | 效果 |
|---|---|
| `沙绫闭嘴` | 当前群闭嘴，bot 说完最后感言后沉默 |
| `沙绫张嘴` | 当前群恢复，bot 表达重新开口的感受 |
| `全群闭嘴` / `全局闭嘴` | 所有群闭嘴 |
| `全群张嘴` / `全局张嘴` / `恢复` | 解除全局闭嘴 |

标准指令（需唤醒前缀 `/`）：

| 指令 | 说明 |
|---|---|
| `/quiet_status`（别名：`闭嘴状态`、`沉默状态`） | 查看当前闭嘴状态 |
| `/quiet_global_silent` | 全局闭嘴 |
| `/quiet_global_resume` | 解除全局闭嘴 |
| `/quiet_clear_groups` | 清空单群闭嘴名单 |

## 配置

所有配置项均可在 AstrBot WebUI 插件配置页修改，保存后重启生效：

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `target_keywords` | `["沙绫", "saaya", "独角兽"]` | 目标人格关键词，闭嘴/张嘴指令必须包含其一 |
| `silent_triggers` | `["闭嘴", "shut up", "别说话", "安静一下"]` | 闭嘴触发词 |
| `resume_triggers` | `["张嘴", "醒醒", "可以说话了", "说话吧"]` | 张嘴触发词 |
| `global_silent_triggers` | `["全群闭嘴", "全局闭嘴"]` | 全局闭嘴触发词（无需目标关键词） |
| `global_resume_triggers` | `["全群张嘴", "全局张嘴", "恢复"]` | 全局张嘴触发词 |
| `admin_only` | `true` | 仅管理员可切换 |
| `admin_qqs` | `[]` | 额外管理员 QQ 号（留空用 AstrBot 内置权限） |
| `final_reply_enabled` | `true` | 切换时是否生成对话式感言 |
| `silent_injection` | 内置模板 | 闭嘴感言提示词注入（可自定义） |
| `resume_injection` | 内置模板 | 张嘴感言提示词注入（可自定义） |
| `silent_fallback_text` | `……哼，好吧好吧，我闭嘴就是了。` | 感言生成失败时的兜底文案 |
| `resume_fallback_text` | `我回来啦～刚才可把我憋坏了。` | 同上（张嘴） |
| `silent_intercept_enabled` | `true` | 静默拦截：闭嘴时完全阻断消息 |
| `llm_intercept_enabled` | `true` | 仅 LLM 拦截：闭嘴时只禁 AI 回复，其他插件照常（静默拦截关闭时生效） |
| `bot_guard_enabled` | `true` | Bot 防互引用循环开关（需 `bot_qq_list` 非空才实际生效） |
| `bot_qq_list` | `[]` | 群里**其他 bot** 的 QQ 号列表，回复它们时不引用对方消息 |
| `bot_reply_mode` | `no_quote` | `no_quote`=回复但不引用对方；`ignore`=完全无视其他 bot 的消息 |
| `bot_guard_reminder` | 内置模板 | 注入 LLM 的「对方也是 bot」提醒，支持 `{sender_name}` `{sender_id}` `{group_id}` 占位符 |
| `bot_guard_max_rounds` | `0` | 连续 N 条 bot 消息无人插话后熔断（停止回应 bot），`0`=不限制 |
| `mute_auto_quiet_enabled` | `true` | 被禁言自动闭嘴：检测到 bot 被禁言时自动进入该群闭嘴模式（独立、持久化） |
| `unmute_farewell_enabled` | `true` | 解禁自动发「张嘴感言」：禁言解除/到期时像平常聊天一样重新开口 |
| `unmute_injection` | 内置模板 | 解禁感言提示词注入（可自定义） |
| `unmute_fallback_text` | `……解除禁言了？那我继续说话啦。` | 解禁感言兜底文案（无事件上下文或 LLM 失败时直发） |

### 推荐配置：Bot 防互引用循环

两个 bot 都开了「回复时引用对话」时，它们会互相引用无限对聊。解决方式：

1. 只需要改**其中一个** bot 的配置：在它的 WebUI 里打开本插件配置，把**另一个 bot** 的 QQ 号填入 `bot_qq_list`
2. 保持 `bot_reply_mode=no_quote`：收到对方 bot 消息时正常回复，但不引用、并收到「对方也是 bot」的提醒，对话自然收敛
3. 如果不想让 bot 理对方，把 `bot_reply_mode` 改成 `ignore`
4. 提示词约束偶尔失灵时，可把 `bot_guard_max_rounds` 设为 `6` 之类作为硬熔断

### 推荐配置：让"张嘴感言"更真实

若希望 bot 张嘴时能对**闭嘴期间群里聊的内容**做出反应，请关闭 `silent_intercept_enabled`、保留 `llm_intercept_enabled`——这样闭嘴期间的消息会被 livingmemory 等插件正常记录。

### 禁言联动说明

被群管理员禁言时（QQ 的 OneBot `group_ban` 事件），插件会：

1. 自动把该群加入「自动闭嘴名单」（与手动闭嘴名单分开，`/quiet_status` 可见），**持久化**，重启不丢
2. 记录禁言时长；禁言期内的消息按当前拦截策略处理
3. 禁言被**管理员手动解除**（`lift_ban` 事件）或**自然到期**（插件每 30s 检查一次）时，自动退出自动闭嘴，并发出「解禁感言」
4. 若禁言期间管理员又手动对该群「闭嘴」，则只退出自动闭嘴、不发感言（尊重手动指令）

> 注意：自然到期由 QQ 服务端静默解禁、不产生事件，所以插件用「到期时间戳 + 30s 周期检查」兜底；若插件在禁言期间被重启，解禁时会因缺少事件上下文而使用 `unmute_fallback_text` 兜底文案。

## 工作原理

```
"沙绫闭嘴"
   │
   ├─ 1. 立即开启拦截（后续消息按配置的拦截策略处理，不会漏）
   │
   ├─ 2. 组装感言请求 = 当前会话对话历史 + 人格 prompt + 提示词注入
   │       └─ 触发 OnLLMRequestEvent 钩子 → livingmemory 等插件注入记忆召回
   │
   ├─ 3. LLM 生成"平常对话一样"的最后反应，直接发送到群里
   │
   └─ 4. 本次问答写回会话历史（之后的聊天仍记得这次告别）
```

## 更新记录

见 [CHANGELOG.md](CHANGELOG.md)。

## 许可证

MIT
