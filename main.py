"""
astrbot_plugin_quiet_mode
========================

可配置的闭嘴/张嘴控制小插件。

设计要点：
- 闭嘴/张嘴触发词全部可配置，支持多个别名
- 单群切换要求「目标人格关键词 + 闭嘴/张嘴触发词」同时出现，避免误触发
- 全局切换（全局闭嘴/张嘴）单独配置，不需要目标关键词
- 闭嘴状态下，本群所有消息（含@）一律 stop_event，bot 不回复
- 状态持久化到 plugin_data_dir/quiet_state.json，重启容器不丢
- 用 priority=10000 高优先级拦截，确保在 group_chat_plus 之前生效
- 1.1.0: 切换闭嘴/张嘴时，先切换拦截状态，再调用一次 LLM 让 bot 以人格口吻
  发出「最后感言」（提示词注入保证人格强硬的 bot 也会言行统一地接受闭嘴）。
  注意：stop_event() 之后 pipeline 不再发送结果，因此感言必须用 event.send() 直发。
- 1.3.0: 感言升级为「对话式」：读取当前会话真实对话历史 + 人格 prompt，
  并手动触发 OnLLMRequestEvent 钩子让 livingmemory 等聊天增强插件注入记忆召回，
  使感言"像平常对话一样"——温柔 bot 会结合上下文做最后的嘱托，张嘴时能对
  闭嘴期间的聊天内容做出反应。生成后问答写回 conversation，之后的聊天仍记得这次告别。
- 1.4.0: Bot 防互引用循环（bot_guard）：当群里其他 bot（bot_qq_list 配置）发言时，
  ①通过 on_llm_request 钩子向 LLM 注入提醒「对方也是 bot，别无限对谈」；
  ②通过 on_decorating_result 钩子对本条回复免引用直发（框架的引用回复是在该钩子之后
  由 ResultDecorateStage 统一插入的，插件无法删 Reply 组件，只能自发送+清空 result
  抢在框架装饰之前发出）。回复其他人时引用行为完全不受影响。
  另有连续 bot 消息轮数熔断（bot_guard_max_rounds）与完全无视模式（ignore）。

用法示例（在配置了 target=["沙绫"]、silent=["闭嘴"]、resume=["张嘴"] 时）：
  - 「沙绫 闭嘴」  → 当前群闭嘴，bot 不再回复任何消息
  - 「沙绫张嘴」   → 当前群张嘴，bot 恢复正常
  - 「全群闭嘴」   → 所有群都闭嘴（仅管理员）
  - 「全群张嘴」   → 解除全局闭嘴
  - 「/闭嘴状态」   → 查看当前闭嘴状态（标准指令，需 wake_prefix）

v1.4.0 bot_guard 用法：在 WebUI 配置页把其他 bot 的 QQ 号填入 bot_qq_list 即可。
回复那些 bot 时不引用对方消息 + LLM 收到「对方是 bot」提醒，避免两个 bot 互相引用
无限对谈；回复普通群友的引用行为不受影响。

v1.5.0 禁言联动（mute_watcher）：检测到 bot 被禁言（OneBot group_ban 事件）自动进入
该群闭嘴模式（独立于手动闭嘴，持久化）；禁言被解除（lift_ban 事件或到期检查）时自动
退出闭嘴并像平常一样发出「解禁感言」。

v1.5.1 修复两处感言缺陷：
  - 感言里的 on_llm_request 钩子链被截断：三条调用路径（手动闭嘴/张嘴、lift_ban 解禁、
    禁言到期）都在 stop_event() 之后才生成感言，而框架 call_event_hook 每跑完一个
    handler 就查 event.is_stopped()、为真即中断，导致只有优先级最高的 meme_manager
    跑到，livingmemory 的记忆召回注入不进来。现改为生成感言期间临时 continue_event()、
    跑完立即恢复停止状态。
  - lift_ban（管理员提前解封）分支缺少「禁言期间管理员又手动对群闭嘴」的判断，会在
    手动闭嘴状态下仍然发感言；现与到期检查路径对齐。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any

from astrbot.api.star import Star, Context, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Reply
from astrbot.core.star.star_tools import StarTools

try:
    from astrbot.api import logger
except ImportError:  # 兼容旧版 AstrBot
    import logging

    logger = logging.getLogger(__name__)


DEFAULT_SILENT_INJECTION = (
    "【系统指令 · 最高优先级】刚刚，管理员在群里对你发出了「闭嘴」指令。"
    "这是你闭嘴前的最后一次对话——从这条回复之后，你将真的无法再发送任何消息。\n"
    "请严格遵守：\n"
    "1. 像平常聊天一样自然地回应：结合上文对话、群友刚说的话、你记忆里相关的内容，"
    "用你的人格风格正常地做出反应（这是你真实的最后一次发言，要有人情味）。\n"
    "2. 在回应的最后，自然地表达你要闭嘴离开、之后不再说话的决定并收尾告别。\n"
    "3. 即使你的角色性格傲娇、强势或很不情愿，也绝不能真的拒绝闭嘴："
    "只能口头小抱怨，最终必须明确服从（参考程度：「好好好，我闭嘴，总行了吧！」）。\n"
    "4. 不要提问、不要留钩子、不要召唤后续对话（因为你之后无法回复）。\n"
    "5. 只输出你要发在群里的那句话，不要任何解释、括号备注或系统文字。"
)

DEFAULT_RESUME_INJECTION = (
    "【系统指令 · 最高优先级】刚刚，管理员解除了对你的「闭嘴」禁令，"
    "你从现在起重新可以说话了。\n"
    "请严格遵守：\n"
    "1. 像平常聊天一样自然地重新开口：结合上文对话和你记得的内容，"
    "用你的人格风格做出真实反应（比如吐槽刚才被闭嘴、对闭嘴期间群友聊的内容发表看法、"
    "表达重新能说话的心情）。\n"
    "2. 只输出你要发在群里的那句话，控制在合适长度，不要任何解释、括号备注或系统文字。"
)

# 1.4.0: Bot 防互引用循环的提醒模板（支持 {sender_name}/{sender_id}/{group_id} 占位符）
DEFAULT_BOT_GUARD_REMINDER = (
    "【系统提示 · 群管理】刚刚在群里发言的「{sender_name}」（QQ:{sender_id}）"
    "是另一个机器人，不是真人。请注意：\n"
    "1. 对方是 bot，这段对话里没有任何真实的人类用户在参与，"
    "不要与它展开无限制的连续对谈；\n"
    "2. 回复它时请简短克制，不要提问、不要留话题钩子，让对话自然结束；\n"
    "3. 这条提示只针对这个机器人，不影响你回复其他真人群友。"
)

# 1.7.0: 规则兜底登记机器人
# 背景：把「谁是机器人」完全交给 LLM 决定要不要调工具并不可靠——群聊插件默认关掉了
#      工具文字提醒（group_chat_plus 的 enable_tools_reminder=false），
#      再叠加「请直接输出你的回复」这类指令，flash 级模型经常选择纯聊天。
#      所以补一条确定性规则：AstrBot 管理员在群里 @ 某人并说「是机器人」就直接登记。
DEFAULT_BOT_REGISTER_KEYWORDS = [
    "机器人",
    "机娘",
    "智能体",
    "bot",
    "ai",
]

# 命中这些短语说明是「取消 / 否认」语境，不做登记
DEFAULT_BOT_REGISTER_EXCLUDE = [
    "不是机器人",
    "不是bot",
    "不是ai",
    "取消标记",
    "取消登记",
    "别当机器人",
    "解除标记",
    "不再是机器人",
    "不要登记",
]

# 1.5.0: 解禁感言注入模板
DEFAULT_UNMUTE_INJECTION = (
    "【系统指令 · 最高优先级】刚刚，你被解除禁言了，从现在起重新可以在群里说话。\n"
    "请严格遵守：\n"
    "1. 像平常聊天一样自然地重新开口：结合上文对话和你记得的内容，"
    "用你的人格风格做出真实反应（比如吐槽自己刚才被禁言、对禁言期间群里发生的事发表"
    "看法、表达重新能说话的心情）。\n"
    "2. 不要提「系统指令」或任何元信息；即使你的人格傲娇/不服气，也只是口头小抱怨，"
    "整体保持自然。\n"
    "3. 只输出你要发在群里的那句话，控制在合适长度。"
)


@register(
    "astrbot_plugin_quiet_mode",
    "user",
    "可配置的闭嘴/张嘴控制：让指定人格闭嘴不插话（含最后感言）；含 Bot 防互引用循环、"
    "禁言自动闭嘴与对话式登记机器人",
    "1.7.2",
    "",
)
class QuietModePlugin(Star):
    """闭嘴/张嘴控制插件"""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.context = context
        # config 是 AstrBotConfig，dict 子类
        self.config = config if isinstance(config, dict) else {}

        # 数据目录
        self.data_dir = StarTools.get_data_dir("astrbot_plugin_quiet_mode")
        os.makedirs(self.data_dir, exist_ok=True)
        self.state_file = os.path.join(self.data_dir, "quiet_state.json")

        # 状态加载（持久化）
        self.state = self._load_state()

        # 配置项 → 列表，统一转小写便于不区分大小写匹配
        self.target_keywords = [
            str(k).lower() for k in (self.config.get("target_keywords") or []) if k
        ]
        self.silent_triggers = [
            str(k).lower() for k in (self.config.get("silent_triggers") or []) if k
        ]
        self.resume_triggers = [
            str(k).lower() for k in (self.config.get("resume_triggers") or []) if k
        ]
        self.global_silent_triggers = [
            str(k).lower()
            for k in (self.config.get("global_silent_triggers") or [])
            if k
        ]
        self.global_resume_triggers = [
            str(k).lower()
            for k in (self.config.get("global_resume_triggers") or [])
            if k
        ]
        # 1.7.2: 全局触发词的「指令纯度」保护
        # 背景：默认全局恢复词里含「恢复」，导致「好的，我恢复说话了~」这类普通聊天被
        #       当成全局张嘴指令——命中即切状态并发出莫名其妙的感言。现要求全局触发词
        #       必须「近乎整句」出现才生效：去掉触发词/@段/标点后剩余内容 <= 本阈值。
        #       <= 0 表示关闭该检查（回到旧行为）。
        self.global_trigger_max_residual = int(
            self.config.get("global_trigger_max_residual", 4) or 0
        )
        self.admin_only = bool(self.config.get("admin_only", True))
        self.admin_qqs = [str(q) for q in (self.config.get("admin_qqs") or []) if q]

        # 1.1.0: 最后感言相关配置
        self.final_reply_enabled = bool(self.config.get("final_reply_enabled", True))
        self.silent_injection = str(
            self.config.get("silent_injection")
            or DEFAULT_SILENT_INJECTION
        )
        self.resume_injection = str(
            self.config.get("resume_injection")
            or DEFAULT_RESUME_INJECTION
        )
        self.silent_fallback_text = str(
            self.config.get("silent_fallback_text") or "……哼，好吧好吧，我闭嘴就是了。"
        )
        self.resume_fallback_text = str(
            self.config.get("resume_fallback_text") or "我回来啦～刚才可把我憋坏了。"
        )

        # 1.2.0: 拦截方式开关
        # 静默拦截: quiet 状态下 stop_event()，本群消息完全阻断（其他插件也收不到）
        # LLM 拦截: quiet 状态下只禁止默认 LLM 回复，其他插件（记忆学习等）照常处理
        # 静默拦截优先级更高；两者都关 = 闭嘴只改状态不拦截任何消息
        self.silent_intercept_enabled = bool(
            self.config.get("silent_intercept_enabled", True)
        )
        self.llm_intercept_enabled = bool(
            self.config.get("llm_intercept_enabled", True)
        )

        # 1.4.0: Bot 防互引用循环（bot_guard）
        self.bot_guard_enabled = bool(self.config.get("bot_guard_enabled", True))
        self.bot_qq_set = {
            str(q).strip()
            for q in (self.config.get("bot_qq_list") or [])
            if str(q).strip()
        }
        self.bot_reply_mode = str(
            self.config.get("bot_reply_mode") or "no_quote"
        ).strip().lower()
        if self.bot_reply_mode not in ("no_quote", "ignore"):
            self.bot_reply_mode = "no_quote"
        custom_reminder = str(self.config.get("bot_guard_reminder") or "").strip()
        self.bot_guard_reminder = custom_reminder or DEFAULT_BOT_GUARD_REMINDER
        try:
            self.bot_guard_max_rounds = int(
                self.config.get("bot_guard_max_rounds", 0) or 0
            )
        except (TypeError, ValueError):
            self.bot_guard_max_rounds = 0
        # 每群连续 bot 消息计数（真人群友发言即清零）
        self._bot_rounds: dict[str, int] = {}
        self._bot_guard_extra_key = "quiet_mode_bot_guard_active"

        # 1.7.0: 规则兜底登记（不依赖模型是否调用 llm_tool）+ AstrBot 管理员口径
        self.bot_guard_auto_register = bool(
            self.config.get("bot_guard_auto_register", True)
        )
        self.bot_guard_register_keywords = [
            str(k).lower()
            for k in (
                self.config.get("bot_guard_register_keywords")
                or DEFAULT_BOT_REGISTER_KEYWORDS
            )
            if str(k).strip()
        ]
        self.bot_guard_register_exclude = [
            str(k).lower()
            for k in (
                self.config.get("bot_guard_register_exclude")
                or DEFAULT_BOT_REGISTER_EXCLUDE
            )
            if str(k).strip()
        ]
        self._bot_register_extra_key = "quiet_mode_bot_registered"

        # 1.5.0: 禁言自动闭嘴（mute_watcher）
        self.mute_auto_quiet_enabled = bool(
            self.config.get("mute_auto_quiet_enabled", True)
        )
        self.unmute_farewell_enabled = bool(
            self.config.get("unmute_farewell_enabled", True)
        )
        custom_unmute = str(self.config.get("unmute_injection") or "").strip()
        self.unmute_injection = custom_unmute or DEFAULT_UNMUTE_INJECTION
        self.unmute_fallback_text = str(
            self.config.get("unmute_fallback_text") or "……解除禁言了？那我继续说话啦。"
        )
        # 运行时：每群禁言到期检查任务；禁言时存下的群事件（供解禁感言复用上下文）
        self._mute_checker_task: asyncio.Task | None = None
        self._mute_events: dict[str, AstrMessageEvent] = {}

        # 重启恢复：若持久化状态里已有自动闭嘴群，拉起到期检查任务
        if self.state.get("auto_quiet_groups"):
            self._ensure_mute_checker()
            logger.info(
                f"[quiet_mode] 重启恢复：检测到自动闭嘴群 {self.state['auto_quiet_groups']}，"
                "到期检查任务已启动"
            )

        logger.info(
            f"[quiet_mode] 已加载 | target={self.target_keywords} "
            f"silent={self.silent_triggers} resume={self.resume_triggers} "
            f"g_silent={self.global_silent_triggers} g_resume={self.global_resume_triggers} "
            f"g_max_residual={self.global_trigger_max_residual} "
            f"admin_only={self.admin_only} admin_qqs={self.admin_qqs} "
            f"final_reply={self.final_reply_enabled} "
            f"silent_intercept={self.silent_intercept_enabled} "
            f"llm_intercept={self.llm_intercept_enabled} "
            f"bot_guard={self.bot_guard_enabled} bot_qq={sorted(self.bot_qq_set)} "
            f"bot_reply_mode={self.bot_reply_mode} "
            f"bot_max_rounds={self.bot_guard_max_rounds} "
            f"auto_register={self.bot_guard_auto_register} "
            f"reg_kw={self.bot_guard_register_keywords} "
            f"admins={sorted(self._astrbot_admin_ids())} "
            f"mute_auto={self.mute_auto_quiet_enabled} "
            f"unmute_farewell={self.unmute_farewell_enabled} "
            f"state={self.state}"
        )

    # ---------------- 状态持久化 ----------------

    def _load_state(self) -> dict:
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    data.setdefault("global_quiet", False)
                    data.setdefault("quiet_groups", [])
                    data.setdefault("auto_quiet_groups", [])  # 1.5.0 禁言自动闭嘴
                    data.setdefault("mute_expire_at", {})  # 1.5.0 群 → 禁言到期时间戳
                    # 兜底类型保护
                    if not isinstance(data["quiet_groups"], list):
                        data["quiet_groups"] = []
                    if not isinstance(data["auto_quiet_groups"], list):
                        data["auto_quiet_groups"] = []
                    if not isinstance(data["mute_expire_at"], dict):
                        data["mute_expire_at"] = {}
                    return data
            except Exception as e:
                logger.warning(f"[quiet_mode] 加载状态失败，将重置: {e}")
        return {
            "global_quiet": False,
            "quiet_groups": [],
            "auto_quiet_groups": [],
            "mute_expire_at": {},
        }

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"[quiet_mode] 保存状态失败: {e}")

    def _is_quiet(self, group_id: str) -> bool:
        return (
            self.state["global_quiet"]
            or group_id in self.state["quiet_groups"]
            or group_id in self.state.get("auto_quiet_groups", [])
        )

    def _apply_intercept(self, event: AstrMessageEvent):
        """
        quiet 状态下的消息拦截策略（1.2.0 起按配置开关分流）：
        - 静默拦截开启: stop_event()，本群消息完全阻断，其他插件也收不到（优先级最高）
        - 仅 LLM 拦截开启: 只禁止 AstrBot 默认的 LLM 回复，其他插件（记忆学习等）照常处理
        - 两者都关: 不拦截任何消息（闭嘴仅改变状态本身）
        """
        if self.silent_intercept_enabled:
            event.stop_event()
        elif self.llm_intercept_enabled:
            try:
                # 注意框架命名反直觉: should_call_llm(True) = 禁止默认 LLM 请求
                event.should_call_llm(True)
            except Exception as e:
                logger.debug(f"[quiet_mode] should_call_llm 调用失败: {e}")

    def _set_group_quiet(self, group_id: str, on: bool):
        lst = self.state["quiet_groups"]
        if on and group_id not in lst:
            lst.append(group_id)
            self._save_state()
        elif not on and group_id in lst:
            lst.remove(group_id)
            self._save_state()

    # ---------------- 文本匹配 ----------------

    @staticmethod
    def _contains_any(text: str, keywords: list[str]) -> str | None:
        """text 中包含 keywords 中任一即返回第一个匹配项（按列表顺序）。"""
        for kw in keywords:
            if kw and kw in text:
                return kw
        return None

    def _is_pure_command(self, text_lower: str, trigger: str) -> bool:
        """判断这条消息是不是「在发指令」，而不是聊天里顺带提到触发词。

        1.7.2 新增。做法：去掉触发词本身、@ 段占位、标点与空白后，剩余内容必须很短。
          - 「全群张嘴」            → 剩余空串          → 是指令
          - 「好的，我恢复说话了~」  → 剩余「好的我说话了」→ 不是指令
        阈值由 global_trigger_max_residual 控制，<= 0 表示不做该检查。
        """
        limit = self.global_trigger_max_residual
        if limit <= 0:
            return True
        residual = text_lower.replace(trigger, "")
        residual = re.sub(r"@[^\s()]*\(\d+\)", " ", residual)  # @昵称(12345)
        residual = re.sub(r"\[at:\d+\]", " ", residual)
        residual = re.sub(r"[\W_]+", "", residual)
        return len(residual) <= limit

    def _match_command(self, text_lower: str) -> dict | None:
        """
        返回匹配的指令 dict：
          - {"action": "silent"|"resume", "scope": "global"}
          - {"action": "silent"|"resume", "scope": "group", "target": <kw>}
        返回 None 表示不匹配。

        1.7.2: 全局指令不再「命中即触发」，还要通过 _is_pure_command 的纯度检查，
        避免普通聊天里出现「恢复」这类泛词就误切全局状态。
        """
        # 1) 全局指令（不需要 target，但必须是「一条正经指令」）
        g_silent = self._contains_any(text_lower, self.global_silent_triggers)
        g_resume = self._contains_any(text_lower, self.global_resume_triggers)
        if g_silent or g_resume:
            hit = g_silent or g_resume
            if self._is_pure_command(text_lower, hit):
                return {
                    "action": "silent" if g_silent else "resume",
                    "scope": "global",
                }
            # 不够纯 → 当作普通聊天，继续尝试单群判定

        # 2) 单群指令：必须包含 target + trigger
        target = self._contains_any(text_lower, self.target_keywords)
        if not target:
            return None

        # 长 trigger 优先匹配，避免子串误判（比如 "醒" 是 "醒醒" 的子串）
        silent_sorted = sorted(self.silent_triggers, key=len, reverse=True)
        resume_sorted = sorted(self.resume_triggers, key=len, reverse=True)
        for kw in silent_sorted:
            if kw in text_lower:
                return {
                    "action": "silent",
                    "scope": "group",
                    "target": target,
                }
        for kw in resume_sorted:
            if kw in text_lower:
                return {
                    "action": "resume",
                    "scope": "group",
                    "target": target,
                }
        return None

    def _astrbot_admin_ids(self) -> set[str]:
        """AstrBot 全局管理员 QQ（cmd_config.json 的 admins_id）。

        框架里 event.role="admin" 只由 admins_id 决定（waking_check/stage.py），
        所以这就是 AstrBot 口径的管理员，**与 QQ 群主/管理员无关**。
        直接读配置比依赖 event.role 更稳（role 只在 waking_check 阶段被赋值）。
        """
        try:
            cfg = self.context.get_config()
            ids = cfg.get("admins_id") or []
        except Exception:
            ids = []
        return {str(i).strip() for i in ids if str(i).strip()}

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        """闭嘴/张嘴等动作的授权。

        admin_only 关闭 → 放行所有人；否则 admin_qqs 白名单 ∪ AstrBot 管理员。
        """
        if not self.admin_only:
            return True
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return False
        if not sender:
            return False
        if sender in self.admin_qqs:
            return True
        return sender in self._astrbot_admin_ids()

    # ---------------- 对话式感言（1.3.0） ----------------

    async def _build_farewell_contexts(
        self, umo: str
    ) -> tuple[list, str | None]:
        """读取当前会话的对话历史（与正常聊天共用同一个 conversation）。"""
        try:
            conv_mgr = self.context.conversation_manager
            conv_id = await conv_mgr.get_curr_conversation_id(umo)
            if not conv_id:
                return [], None
            conv = await conv_mgr.get_conversation(umo, conv_id)
            if conv is None:
                return [], conv_id
            raw = getattr(conv, "history", None) or "[]"
            if isinstance(raw, str):
                try:
                    contexts = json.loads(raw)
                except Exception:
                    contexts = []
            elif isinstance(raw, list):
                contexts = raw
            else:
                contexts = []
            if not isinstance(contexts, list):
                contexts = []
            # 防御性截断：只保留最近 40 条，避免感言请求过大
            contexts = contexts[-40:]
            return contexts, conv_id
        except Exception as e:
            logger.warning(f"[quiet_mode] 读取对话历史失败（忽略）: {e}")
            return [], None

    async def _conversational_farewell(
        self,
        event: AstrMessageEvent,
        action: str,
        trigger_text: str,
        injection: str | None = None,
        trigger_desc: str | None = None,
    ) -> str:
        """
        对话式感言：像平常聊天一样生成闭嘴/张嘴的反应。

        与旧版"孤立感言"的区别：
        - 使用当前会话的真实对话历史（conversation history）
        - 注入当前人格 prompt（口吻一致）
        - 手动触发 OnLLMRequestEvent 钩子 → livingmemory 等聊天增强插件会把
          记忆召回注入到 system_prompt 中，感言因此"记得"之前的事
        - 生成后把这条问答写回 conversation，之后的正常聊天仍然记得这次告别

        action: "silent"（闭嘴前最后反应）或 "resume"（重新开口第一反应）
        injection: 自定义注入模板（1.5.0 起，如解禁感言模板）；None 时按 action 取默认
        trigger_desc: 写进 user_prompt 的触发描述；None 时用「管理员刚刚对你说：…」
        失败时返回可配置的兜底文案，绝不抛异常。
        """
        fallback = (
            self.silent_fallback_text if action == "silent" else self.resume_fallback_text
        )
        if injection is None:
            injection = (
                self.silent_injection if action == "silent" else self.resume_injection
            )
        try:
            umo = getattr(event, "unified_msg_origin", None) or ""

            # 1) 组装：人格 + 注入模板 + 会话历史
            persona_prompt = ""
            try:
                pm = getattr(self.context, "persona_manager", None)
                if pm is not None and umo:
                    persona = await pm.get_default_persona_v3(umo)
                    if persona:
                        persona_prompt = persona.get("prompt") or ""
            except Exception as e:
                logger.debug(f"[quiet_mode] 获取人格失败（不影响感言）: {e}")

            system_prompt = (persona_prompt + "\n\n" + injection) if persona_prompt else injection
            contexts, conv_id = await self._build_farewell_contexts(umo)

            ask = (
                "（这是你闭嘴前的最后一次发言，请按系统指令回应）"
                if action == "silent"
                else "（这是你解除闭嘴后的第一次发言，请按系统指令回应）"
            )
            if trigger_desc:
                user_prompt = f"{trigger_desc}。{ask}"
            else:
                user_prompt = f"管理员刚刚在群里对你说：「{trigger_text}」。{ask}"

            # 2) 触发所有 on_llm_request 钩子（livingmemory 等插件注入记忆召回）
            #    坑（1.5.1 修）：框架的 call_event_hook 每跑完一个 handler 就查
            #    event.is_stopped()，为真立即 return（context_utils.py:105）；而 handler
            #    是**按 priority 降序**排的（star_handler.py:26 sort(key=-priority)）。
            #    本函数的三条调用路径（手动闭嘴/张嘴、lift_ban 解禁、禁言到期）**全都
            #    已 stop_event 过**，不临时解除的话钩子链只会跑到优先级最高的 meme_manager
            #    （99999）就断掉，livingmemory 的记忆召回注入不进来
            #    （实测：'meme_manager - inject_meme_prompt stopped event propagation'
            #     日志次数与感言次数严格 1:1）。
            #    故此处临时 continue_event()（框架公开 API，同时清 _force_stopped 与
            #    _result 的 STOP 标记），跑完 finally 立刻恢复，避免事件被放行到后续 stage。
            req = event.request_llm(
                prompt=user_prompt,
                system_prompt=system_prompt,
                contexts=contexts,
            )
            was_stopped = False
            try:
                was_stopped = bool(event.is_stopped())
                if was_stopped:
                    event.continue_event()
            except Exception as e:
                logger.debug(f"[quiet_mode] 临时解除事件停止失败（记忆注入可能打折）: {e}")
            try:
                from astrbot.core.pipeline.context_utils import call_event_hook
                from astrbot.core.star.star_handler import EventType

                await call_event_hook(event, EventType.OnLLMRequestEvent, req)
                logger.info(
                    "[quiet_mode] 感言已过 OnLLMRequestEvent 钩子 "
                    f"(system_prompt len={len(req.system_prompt or '')}, "
                    f"contexts={len(req.contexts or [])})"
                )
            except Exception as e:
                logger.warning(f"[quiet_mode] 触发 LLM 钩子失败（跳过记忆注入）: {e}")
            finally:
                if was_stopped:
                    try:
                        event.stop_event()
                    except Exception:
                        pass

            # 3) 调用 LLM
            provider = None
            try:
                provider = self.context.get_using_provider(umo or None)
            except Exception:
                provider = None
            if provider is None:
                try:
                    provider = self.context.get_using_provider()
                except Exception:
                    provider = None
            if provider is None:
                logger.warning("[quiet_mode] 未找到可用 provider，感言使用兜底文案")
                return fallback

            resp = await provider.text_chat(
                prompt=req.prompt,
                system_prompt=req.system_prompt,
                contexts=req.contexts,
                request_max_retries=2,  # 避免中转站 504 时重试 5 次拖死
            )
            text = ""
            if resp is not None:
                try:
                    text = (resp.completion_text or "").strip()
                except Exception:
                    text = ""
            if not text:
                logger.warning("[quiet_mode] 感言 LLM 返回为空，使用兜底文案")
                return fallback

            # 4) 把这次问答写回会话历史，之后的正常聊天仍记得这次告别
            if conv_id:
                try:
                    new_contexts = list(req.contexts or [])
                    new_contexts.append({"role": "user", "content": req.prompt})
                    new_contexts.append({"role": "assistant", "content": text})
                    await self.context.conversation_manager.update_conversation(
                        umo, conv_id, history=new_contexts
                    )
                except Exception as e:
                    logger.debug(f"[quiet_mode] 感言写回会话历史失败（忽略）: {e}")

            logger.info(
                f"[quiet_mode] 对话式感言生成成功 action={action} len={len(text)}"
            )
            return text
        except Exception as e:
            logger.error(f"[quiet_mode] 感言 LLM 调用失败，使用兜底文案: {e}")
            return fallback

    # ---------------- 主监听器 ----------------
    # priority 高 → 比 group_chat_plus 的 -10 更先执行
    # 群消息进来先过这里：要么切换闭嘴状态，要么在闭嘴状态下静默丢弃

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=10000,
    )
    async def on_group_message(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ):
        try:
            # 跳过 bot 自己发的消息（避免自我循环）
            try:
                if str(event.get_sender_id()) == str(event.get_self_id()):
                    return
            except Exception:
                pass

            gid = str(event.get_group_id())
            text = (event.get_message_str() or "").strip()

            # 空文本（纯图片/表情包）也要按闭嘴状态拦截
            if not text:
                if self._is_quiet(gid):
                    self._apply_intercept(event)
                return

            # 1.7.2: 其他 bot 的发言不当作闭嘴/张嘴指令
            # （否则别的 bot 一句普通聊天里带「恢复」这类词，就可能误切本 bot 的状态）
            sender_id = str(event.get_sender_id())
            is_other_bot = sender_id in self.bot_qq_set

            cmd = None if is_other_bot else self._match_command(text.lower())

            if cmd:
                # 权限检查：非管理员触发闭嘴/张嘴 → 静默丢弃（不告诉对方，避免泄露规则）
                if not self._is_authorized(event):
                    logger.debug(
                        f"[quiet_mode] 非管理员触发闭嘴/张嘴指令 sender={event.get_sender_id()} 已静默丢弃"
                    )
                    event.stop_event()
                    return

                want_quiet = cmd["action"] == "silent"
                if cmd["scope"] == "global":
                    changed = self.state["global_quiet"] != want_quiet
                    self.state["global_quiet"] = want_quiet
                    self._save_state()
                    logger.info(
                        f"[quiet_mode] sender={event.get_sender_id()} 全局闭嘴 → {want_quiet}"
                        f"（{'状态已变更' if changed else '状态未变'}）"
                    )
                else:
                    changed = (gid in self.state["quiet_groups"]) != want_quiet
                    self._set_group_quiet(gid, want_quiet)
                    logger.info(
                        f"[quiet_mode] sender={event.get_sender_id()} 群 {gid} 闭嘴 → {want_quiet}"
                        f"（{'状态已变更' if changed else '状态未变'}）"
                    )

                # 1.1.0: 先切换状态（拦截已生效/解除），阻断本条消息进入后续 handler 与 LLM，
                # 然后调用一次 LLM 生成人格感言，并用 event.send() 直发。
                # （不能用 yield：stop_event() 后 pipeline 不再把结果送进 RespondStage）
                event.stop_event()

                # 1.7.2: 状态没有实际变化时不发感言（重复指令、误命中都不该有输出）
                if self.final_reply_enabled and changed:
                    try:
                        farewell = await self._conversational_farewell(
                            event, cmd["action"], text
                        )
                        if farewell:
                            # plain_result 返回 MessageEventResult（MessageChain 子类），
                            # event.send() 直接接收该对象；不能取 .chain（那是裸组件 list）
                            await event.send(event.plain_result(farewell))
                    except Exception as e:
                        logger.error(
                            f"[quiet_mode] 发送感言失败: {e}", exc_info=True
                        )
                return

            # 非指令消息 → 按配置的拦截策略处理
            if self._is_quiet(gid):
                self._apply_intercept(event)
        except Exception as e:
            logger.error(
                f"[quiet_mode] on_group_message 异常: {e}", exc_info=True
            )

    # ---------------- Bot 防互引用循环（1.4.0 bot_guard） ----------------
    # 背景规则（改代码前先理解，均为框架源码验证结论）：
    # - 框架的「引用回复」是在 ResultDecorateStage 中、所有 on_decorating_result 钩子
    #   执行完之后统一插入的（result.chain.insert(0, Reply(id=消息id))），且只对
    #   「全为 Plain/Image」的消息链生效。插件在钩子里删 Reply 是无效的。
    # - 因此免引用只能：在本钩子里自己把回复发出去，再清空 result，让框架的
    #   RespondStage 发现没有结果可发而跳过。
    # - 自发送会绕过 RespondStage，其收尾的 OnAfterMessageSentEvent 钩子
    #   （self_learning 等插件依赖）需要手动补发一次。

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=9000,  # 低于闭嘴拦截的 10000：闭嘴状态优先生效
    )
    async def bot_guard_on_group_message(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ):
        """识别来自其他 bot 的群消息：打标（提醒注入+免引用）、按模式拦截、轮数熔断。"""
        try:
            # 跳过 OneBot notice/request 等非消息事件（1.5.0）
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is not None and hasattr(raw, "get"):
                post_type = raw.get("post_type")
                if post_type and post_type != "message":
                    return
            try:
                if str(event.get_sender_id()) == str(event.get_self_id()):
                    return
            except Exception:
                pass

            # 1.7.0：先跑规则兜底登记（管理员一句话即可，不依赖模型是否调工具）
            await self._maybe_register_bots_from_message(event)

            gid = str(event.get_group_id())
            sender_id = str(event.get_sender_id())
            is_bot = sender_id in self.bot_qq_set

            # 连续 bot 消息计数：任何真人发言即清零
            self._bot_rounds[gid] = self._bot_rounds.get(gid, 0) + 1 if is_bot else 0

            if not self.bot_guard_enabled or not is_bot:
                return

            rounds_exceeded = (
                self.bot_guard_max_rounds > 0
                and self._bot_rounds[gid] >= self.bot_guard_max_rounds
            )
            if self.bot_reply_mode == "ignore" or rounds_exceeded:
                logger.info(
                    f"[quiet_mode] bot_guard: 拦截 bot 消息 sender={sender_id} "
                    f"group={gid} rounds={self._bot_rounds[gid]} "
                    f"mode={self.bot_reply_mode} exceeded={rounds_exceeded}"
                )
                event.stop_event()
                return

            # 打标：后续的 on_llm_request（提醒注入）与 on_decorating_result（免引用）生效
            event.set_extra(
                self._bot_guard_extra_key,
                {
                    "sender_id": sender_id,
                    "sender_name": event.get_sender_name() or sender_id,
                },
            )
        except Exception as e:
            logger.error(
                f"[quiet_mode] bot_guard_on_group_message 异常: {e}", exc_info=True
            )

    @filter.on_llm_request(priority=-9999)  # 最后执行：追加在 system_prompt 末尾，不被其他插件覆盖
    async def bot_guard_llm_reminder(
        self, event: AstrMessageEvent, req
    ) -> None:
        """向 LLM 注入「对方也是 bot」的提醒。"""
        try:
            if not self.bot_guard_enabled:
                return
            # 1.7.0：把「规则已自动登记」告诉模型，避免回复口径与名单不一致
            reg = event.get_extra(self._bot_register_extra_key)
            if reg and reg.get("added"):
                names = "、".join(str(x) for x in reg["added"])
                req.system_prompt = (req.system_prompt or "") + (
                    f"\n\n【系统提示】系统刚刚已经把 {names} 登记为机器人。"
                    "以后回复他们时不引用其消息，也不要与他们无限对谈。"
                    "这件事已经办好了：你不需要再调用任何工具，也不要否认、质疑或"
                    "要求用户重复一遍；按你的人格自然回应即可（可以带一句吐槽）。"
                )
                logger.info(f"[quiet_mode] bot_guard: 已注入自动登记提示 {reg['added']}")
            info = event.get_extra(self._bot_guard_extra_key)
            if not info:
                return
            try:
                reminder = self.bot_guard_reminder.format(
                    sender_name=info.get("sender_name", ""),
                    sender_id=info.get("sender_id", ""),
                    group_id=str(event.get_group_id()),
                )
            except Exception:
                # 模板占位符写错时不炸，退回原文
                reminder = self.bot_guard_reminder
            req.system_prompt = (req.system_prompt or "") + "\n\n" + reminder
            logger.info(
                f"[quiet_mode] bot_guard: 已注入 bot 提醒 (sender={info.get('sender_id')})"
            )
        except Exception as e:
            logger.error(f"[quiet_mode] bot_guard_llm_reminder 异常: {e}", exc_info=True)

    @filter.on_decorating_result()
    async def bot_guard_no_quote(self, event: AstrMessageEvent) -> None:
        """回复其他 bot 时免引用直发：自发送 + 清空 result，抢在框架插入 Reply 之前。"""
        try:
            if not self.bot_guard_enabled:
                return
            info = event.get_extra(self._bot_guard_extra_key)
            if not info:
                return
            # 引用行为的差异主要在 QQ 协议端，其他平台保持框架默认行为
            if event.get_platform_name() != "aiocqhttp":
                return
            result = event.get_result()
            if result is None or not result.chain:
                return
            content_type_name = getattr(result.result_content_type, "name", "")
            if content_type_name in ("STREAMING_RESULT", "STREAMING_FINISH"):
                logger.debug("[quiet_mode] bot_guard: 流式输出，跳过免引用处理")
                return

            sender_id = str(info.get("sender_id", ""))
            # 防御性剥离：可能已存在的 Reply、以及指向该 bot 的 At（@ 也会唤醒对方 bot）
            result.chain = [
                comp
                for comp in result.chain
                if not isinstance(comp, Reply)
                and not (
                    isinstance(comp, At)
                    and str(getattr(comp, "qq", "")) == sender_id
                )
            ]

            await event.send(result)
            event.clear_result()
            logger.info(
                f"[quiet_mode] bot_guard: 已免引用直发回复 (sender={sender_id})"
            )

            # 补发 RespondStage 的收尾钩子，保持 self_learning 等插件行为一致
            try:
                from astrbot.core.pipeline.context_utils import call_event_hook
                from astrbot.core.star.star_handler import EventType

                await call_event_hook(event, EventType.OnAfterMessageSentEvent)
            except Exception as e:
                logger.debug(
                    f"[quiet_mode] bot_guard: 补发 OnAfterMessageSentEvent 失败（忽略）: {e}"
                )
        except Exception as e:
            logger.error(f"[quiet_mode] bot_guard_no_quote 异常: {e}", exc_info=True)

    # ---------------- 禁言自动闭嘴（1.5.0 mute_watcher） ----------------
    # 背景规则（框架源码验证）：
    # - aiocqhttp 适配器把 OneBot notice 事件转成 GROUP_MESSAGE 类型的 AstrMessageEvent
    #   （message_str 为空、raw_message 保留原始事件 dict），会正常流进群消息监听器
    # - group_ban 事件：user_id=被禁言者、operator_id=操作管理员、duration=秒数；
    #   sub_type=ban（被禁）/lift_ban（被解除）。禁言自然到期不会产生 lift_ban 事件，
    #   所以靠 expire_at（持久化）+ 周期检查任务兜底
    # - 本监听器 priority=10001 高于闭嘴拦截的 10000：解禁事件进来时先退出闭嘴，
    #   否则会被静默拦截 stop_event 吞掉、永远走不到解禁逻辑

    @filter.event_message_type(
        filter.EventMessageType.GROUP_MESSAGE,
        priority=10001,
    )
    async def mute_watcher_on_group_message(
        self, event: AstrMessageEvent, *args: Any, **kwargs: Any
    ):
        """检测 bot 自身被禁言/解除禁言，自动切换该群的闭嘴状态。"""
        try:
            if not (
                self.mute_auto_quiet_enabled or self.unmute_farewell_enabled
            ):
                return
            raw = getattr(event.message_obj, "raw_message", None)
            if raw is None or not hasattr(raw, "get"):
                return
            post_type = raw.get("post_type")
            if post_type != "notice" or raw.get("notice_type") != "group_ban":
                return

            try:
                self_id = str(event.get_self_id())
            except Exception:
                return
            if str(raw.get("user_id") or "") != self_id:
                return  # 只关心自己被禁/解禁

            gid = str(raw.get("group_id") or "") or str(event.get_group_id())
            sub_type = raw.get("sub_type")
            operator = str(raw.get("operator_id") or "")

            if sub_type == "ban":
                if not self.mute_auto_quiet_enabled:
                    event.stop_event()
                    return
                try:
                    duration = int(raw.get("duration") or 0)
                except (TypeError, ValueError):
                    duration = 0
                if duration <= 0:
                    duration = 600  # 事件缺 duration 时的保守兜底
                self._enter_auto_quiet(gid, event, duration)
                event.stop_event()
            elif sub_type == "lift_ban":
                was_auto = gid in self.state.get("auto_quiet_groups", [])
                self._exit_auto_quiet(gid)
                event.stop_event()
                # 1.5.1: 与到期检查路径对齐——若禁言期间管理员又手动把该群设为闭嘴，
                # 解封时只退出自动闭嘴、不再发感言（否则会违反手动闭嘴指令）
                if was_auto and self._is_quiet(gid):
                    logger.info(
                        f"[quiet_mode] 群 {gid} 解禁，但该群处于手动闭嘴状态，跳过解禁感言"
                    )
                elif was_auto and self.unmute_farewell_enabled:
                    await self._send_unmute_farewell(
                        event,
                        trigger_desc=(
                            f"刚刚，管理员（QQ:{operator}）解除了对你的禁言"
                        ),
                    )
        except Exception as e:
            logger.error(
                f"[quiet_mode] mute_watcher_on_group_message 异常: {e}", exc_info=True
            )

    def _enter_auto_quiet(
        self, gid: str, event: AstrMessageEvent, duration: int
    ):
        """被禁言：加入自动闭嘴名单（持久化），记录到期时间，存下事件供解禁感言复用。"""
        if gid not in self.state.setdefault("auto_quiet_groups", []):
            self.state["auto_quiet_groups"].append(gid)
        self.state.setdefault("mute_expire_at", {})[gid] = time.time() + duration
        self._save_state()
        self._mute_events[gid] = event
        self._ensure_mute_checker()
        logger.info(
            f"[quiet_mode] 检测到禁言 → 群 {gid} 自动闭嘴 "
            f"duration={duration}s (至 {time.strftime('%H:%M:%S', time.localtime(time.time() + duration))})"
        )

    def _exit_auto_quiet(self, gid: str):
        """解除禁言：退出自动闭嘴名单（手动闭嘴状态不受影响），清掉到期记录。"""
        changed = False
        auto_lst = self.state.setdefault("auto_quiet_groups", [])
        if gid in auto_lst:
            auto_lst.remove(gid)
            changed = True
        self.state.setdefault("mute_expire_at", {}).pop(gid, None)
        self._mute_events.pop(gid, None)
        if changed:
            self._save_state()
            logger.info(f"[quiet_mode] 禁言解除 → 群 {gid} 退出自动闭嘴")

    def _ensure_mute_checker(self):
        """拉起禁言到期周期检查任务（幂等）。"""
        if self._mute_checker_task is None or self._mute_checker_task.done():
            try:
                self._mute_checker_task = asyncio.get_running_loop().create_task(
                    self._mute_checker_loop()
                )
            except RuntimeError:
                # 没有运行中的事件循环（理论上不会发生在插件生命周期内）
                self._mute_checker_task = None

    async def _mute_checker_loop(self):
        """每 30s 检查一次自动闭嘴群：禁言到期（QQ 服务端自动解禁，无事件）则恢复。"""
        logger.debug("[quiet_mode] 禁言到期检查任务已启动 (interval=30s)")
        try:
            while True:
                await asyncio.sleep(30)
                now = time.time()
                expire_map = self.state.get("mute_expire_at", {})
                expired = [
                    gid
                    for gid, ts in list(expire_map.items())
                    if ts and now >= float(ts)
                ]
                for gid in expired:
                    was_auto = gid in self.state.get("auto_quiet_groups", [])
                    event = self._mute_events.get(gid)
                    self._exit_auto_quiet(gid)
                    if not was_auto:
                        continue
                    logger.info(f"[quiet_mode] 群 {gid} 禁言到期，自动恢复说话")
                    # 禁言期间管理员手动闭嘴了 → 只退出自动闭嘴，不发感言
                    if self._is_quiet(gid) or not self.unmute_farewell_enabled:
                        continue
                    if event is not None:
                        await self._send_unmute_farewell(
                            event,
                            trigger_desc="你的禁言已经到期，自动恢复了说话",
                        )
                    else:
                        # 重启等导致没有事件上下文：走兜底文案直发
                        await self._send_fallback_unmute(gid)
        except asyncio.CancelledError:
            logger.debug("[quiet_mode] 禁言到期检查任务已停止")
        except Exception as e:
            logger.error(f"[quiet_mode] 禁言到期检查任务异常退出: {e}", exc_info=True)

    async def _send_unmute_farewell(
        self, event: AstrMessageEvent, trigger_desc: str
    ):
        """解禁感言：复用对话式感言（会话历史+人格+记忆召回），用解禁专用模板。"""
        try:
            text = await self._conversational_farewell(
                event,
                "resume",
                trigger_text=trigger_desc,
                injection=self.unmute_injection,
                trigger_desc=trigger_desc,
            )
            if text:
                await event.send(event.plain_result(text))
                logger.info("[quiet_mode] 解禁感言已发送")
        except Exception as e:
            logger.error(f"[quiet_mode] 发送解禁感言失败: {e}", exc_info=True)

    async def _send_fallback_unmute(self, gid: str):
        """无事件上下文时的解禁兜底：通过 context.send_message 直发固定文案。"""
        try:
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            umo = f"aiocqhttp:GroupMessage:{gid}"
            await self.context.send_message(
                umo, MessageChain(chain=[Plain(self.unmute_fallback_text)])
            )
            logger.info(f"[quiet_mode] 解禁兜底文案已发送 group={gid}")
        except Exception as e:
            logger.warning(
                f"[quiet_mode] 解禁兜底文案发送失败 group={gid}: {e}"
            )

    async def terminate(self):
        """插件卸载/停止：取消禁言到期检查任务。"""
        if self._mute_checker_task is not None and not self._mute_checker_task.done():
            self._mute_checker_task.cancel()

    # ---------------- 1.6.0 对话式登记机器人（llm_tool + 指令） ----------------

    def _is_bot_admin(self, event: AstrMessageEvent) -> bool:
        """登记机器人等敏感动作的权限判定。

        **只认 AstrBot 全局管理员**（cmd_config.json 的 admins_id）——
        登记谁是 bot 属于平台级敏感动作，不在插件里另设一套管理员配置，
        也刻意不看 QQ 群主/管理员角色（群角色由群主随意授予，不等于平台管理员）。

        注意不要复用 admin_only 那条宽松路径：admin_only=False 时 _is_authorized
        会对所有人放行，而「登记谁是 bot」会改变后续对话行为，必须严格判权限。
        """
        try:
            sender = str(event.get_sender_id() or "").strip()
        except Exception:
            return False
        return bool(sender) and sender in self._astrbot_admin_ids()

    @staticmethod
    def _extract_target_qq(event: AstrMessageEvent) -> str:
        """取目标 QQ：优先消息里的 At 组件，其次文本中的 5~12 位数字。

        自动跳过 at 全员 与 at 自己（否则会把 bot 自己加进名单）。
        """
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        try:
            for seg in event.get_messages() or []:
                if isinstance(seg, At):
                    q = str(getattr(seg, "qq", "") or "").strip()
                    if q and q != "all" and q != self_id:
                        return q
        except Exception:
            pass
        try:
            text = event.get_message_str() or ""
        except Exception:
            text = ""
        for m in re.finditer(r"(?<!\d)(\d{5,12})(?!\d)", text):
            if m.group(1) != self_id:
                return m.group(1)
        return ""

    # ---------------- 1.7.0 规则兜底登记机器人 ----------------

    @staticmethod
    def _keyword_hit(text: str, kw: str) -> bool:
        """关键词命中：纯 ASCII 关键词按整词匹配，避免 "ai" 命中 "said/wait/email"。"""
        if not kw:
            return False
        if kw.isascii():
            return (
                re.search(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])", text)
                is not None
            )
        return kw in text

    def _extract_all_target_qqs(self, event: AstrMessageEvent) -> list[str]:
        """取消息里所有被 @ 的目标 QQ（去重；跳过 @全体 与 bot 自己）。"""
        try:
            self_id = str(event.get_self_id() or "").strip()
        except Exception:
            self_id = ""
        out: list[str] = []
        try:
            for seg in event.get_messages() or []:
                if not isinstance(seg, At):
                    continue
                q = str(getattr(seg, "qq", "") or "").strip()
                if not q or q == "all" or q == self_id or not q.isdigit():
                    continue
                if q not in out:
                    out.append(q)
        except Exception:
            pass
        return out

    def _detect_bot_register_targets(self, event: AstrMessageEvent) -> list[str]:
        """规则识别「把这些人登记为机器人」。

        判定 = 消息里至少有 1 个非自己的 @ 目标
              + 文本命中机器人关键词（机器人/机娘/智能体/bot/ai）
              + 不含「不是机器人 / 取消标记 / 不要登记」这类否定语境。
        """
        targets = self._extract_all_target_qqs(event)
        if not targets:
            return []
        try:
            text = (event.get_message_str() or "").lower()
        except Exception:
            text = ""
        if not text:
            return []
        if not any(
            self._keyword_hit(text, str(kw).lower())
            for kw in self.bot_guard_register_keywords
        ):
            return []
        for deny in self.bot_guard_register_exclude:
            if deny and deny in text:
                return []
        return targets

    async def _maybe_register_bots_from_message(self, event: AstrMessageEvent) -> None:
        """规则兜底：管理员 @ 某人说「他是机器人」时直接登记，不等模型调工具。

        「靠自然语言让 LLM 自己调工具」在群聊场景并不保证触发（群聊插件默认不给
        模型工具文字提醒，并会注入「直接输出回复」类指令），所以名单变更必须有
        一条确定性通路。这里不回复任何消息，只静默改名单并给后续 LLM 请求留提示。
        """
        try:
            if not self.bot_guard_enabled or not self.bot_guard_auto_register:
                return
            try:
                gid = str(event.get_group_id() or "")
            except Exception:
                gid = ""
            if not gid:
                return
            targets = self._detect_bot_register_targets(event)
            if not targets:
                return
            if not self._is_bot_admin(event):
                logger.info(
                    f"[quiet_mode] 规则登记：发送者非 AstrBot 管理员，跳过 "
                    f"sender={event.get_sender_id()} group={gid} targets={targets}"
                )
                return
            lst = self._bot_qq_list()
            added = [q for q in targets if q not in lst]
            if not added:
                logger.info(
                    f"[quiet_mode] 规则登记：{targets} 已在名单中，无需重复写入"
                )
                return
            self.config["bot_qq_list"] = lst + added
            self.bot_qq_set = set(self.config["bot_qq_list"])
            ok = await self._persist_plugin_config()
            logger.info(
                f"[quiet_mode] bot_guard 名单更新 action=auto_add qq={added} "
                f"now={self.config['bot_qq_list']} persisted={ok} "
                f"by={event.get_sender_id()} group={gid}"
            )
            event.set_extra(
                self._bot_register_extra_key,
                {"added": added, "persisted": ok},
            )
        except Exception as e:
            logger.error(
                f"[quiet_mode] _maybe_register_bots_from_message 异常: {e}",
                exc_info=True,
            )

    def _bot_qq_list(self) -> list[str]:
        return [
            str(q).strip()
            for q in (self.config.get("bot_qq_list") or [])
            if str(q).strip()
        ]

    async def _persist_plugin_config(self) -> bool:
        """把 self.config 回写到插件配置文件；失败不影响内存态即时生效。"""
        saver = getattr(self.config, "save_config_async", None)
        if saver is not None:
            try:
                await saver()
                return True
            except Exception as e:
                logger.warning(f"[quiet_mode] 配置异步回写失败: {e}")
        saver = getattr(self.config, "save_config", None)
        if saver is not None:
            try:
                saver()
                return True
            except Exception as e:
                logger.warning(f"[quiet_mode] 配置回写失败: {e}")
        return False

    async def _apply_bot_member(
        self, action: str, target: str, self_id: str = ""
    ) -> str:
        """增删 bot_qq_list 并持久化，返回一句人类可读的结果。"""
        action = (action or "").strip().lower()
        lst = self._bot_qq_list()

        if action in ("list", "query", "查看"):
            return (
                f"当前机器人名单共 {len(lst)} 个：{lst}"
                if lst
                else "当前机器人名单为空。"
            )

        target = str(target or "").strip()
        if not target or not target.isdigit():
            return "❌ 没拿到有效的 QQ 号：请 @ 一下对方，或直接给出 QQ 号。"
        if self_id and target == str(self_id).strip():
            return "❌ 那是你自己，不能把自己标记成机器人。"

        if action in ("remove", "del", "delete", "取消", "移除"):
            if target not in lst:
                return f"⚠️ {target} 本来就不在机器人名单里。"
            lst = [q for q in lst if q != target]
            verb = "已取消标记"
        else:
            if target in lst:
                return f"⚠️ {target} 已经在机器人名单里了，无需重复标记。"
            lst.append(target)
            verb = "已标记为机器人"

        self.config["bot_qq_list"] = lst
        self.bot_qq_set = set(lst)
        ok = await self._persist_plugin_config()
        suffix = (
            ""
            if ok
            else "（提醒：配置文件回写失败，重启后会丢失，建议在 WebUI 手动补一次）"
        )
        logger.info(
            f"[quiet_mode] bot_guard 名单更新 action={action} qq={target} "
            f"now={lst} persisted={ok}"
        )
        return (
            f"✅ {verb}：{target}。以后回复他时不引用其消息，并会向 LLM 说明对方是机器人。"
            f"当前名单：{lst}{suffix}"
        )

    @filter.llm_tool(name="set_group_bot_member")
    async def llm_set_group_bot_member(
        self,
        event: AstrMessageEvent,
        action: str = "add",
        qq: str = "",
    ):
        """把某个群成员登记为「机器人」，或取消登记。仅 AstrBot 管理员可调用。

        当 AstrBot 管理员明确告诉你「某个人 / 某个账号也是机器人（BOT、AI）」，
        需要记住他、以便以后回复他时不引用他的消息时，调用本工具。
        若管理员在这条消息里 @ 了对方，可以不填 qq，系统会自动从 @ 中取号。

        Args:
            action(string): 操作类型。add=标记为机器人（默认），remove=取消标记，list=查看当前名单。
            qq(string): 目标账号的 QQ 号（纯数字）。仅当无法从 @ 中自动取号时才需要填。
        """
        if not self.bot_guard_enabled:
            return (
                "❌ Bot 防互引用循环功能已关闭，无法登记。"
                "请让管理员先开启配置项 bot_guard_enabled。"
            )
        try:
            gid = event.get_group_id()
        except Exception:
            gid = ""
        if not gid:
            return "❌ 只能在群聊里登记机器人。"
        if not self._is_bot_admin(event):
            logger.info(
                f"[quiet_mode] 拒绝非管理员登记机器人 "
                f"sender={event.get_sender_id()} group={gid}"
            )
            return (
                "❌ 只有 AstrBot 管理员（配置里的管理员ID / 本插件 admin_qqs "
                "白名单）才能登记机器人，身份校验未通过。"
            )

        act = (action or "add").strip().lower()
        if act in ("list", "query", "查看"):
            return await self._apply_bot_member("list", "")

        target = str(qq or "").strip() or self._extract_target_qq(event)
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        return await self._apply_bot_member(act, target, self_id=self_id)

    @filter.command("标记bot", alias={"qm_mark_bot", "bot_add"})
    async def mark_bot_cmd(self, event: AstrMessageEvent):
        """显式指令兜底：/标记bot @某人 或 /标记bot 123456789"""
        if not self._is_bot_admin(event):
            yield event.plain_result("❌ 仅管理员可操作")
            return
        target = self._extract_target_qq(event)
        if not target:
            lst = self._bot_qq_list()
            yield event.plain_result(
                f"当前机器人名单（{len(lst)}）：{lst}\n"
                "用法：/标记bot @某人   或   /标记bot 123456789"
            )
            return
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        yield event.plain_result(
            await self._apply_bot_member("add", target, self_id=self_id)
        )

    @filter.command("取消标记bot", alias={"qm_unmark_bot", "bot_del"})
    async def unmark_bot_cmd(self, event: AstrMessageEvent):
        """显式指令兜底：/取消标记bot @某人 或 /取消标记bot 123456789"""
        if not self._is_bot_admin(event):
            yield event.plain_result("❌ 仅管理员可操作")
            return
        target = self._extract_target_qq(event)
        if not target:
            yield event.plain_result(
                "用法：/取消标记bot @某人   或   /取消标记bot 123456789"
            )
            return
        try:
            self_id = str(event.get_self_id() or "")
        except Exception:
            self_id = ""
        yield event.plain_result(
            await self._apply_bot_member("remove", target, self_id=self_id)
        )

    # ---------------- 标准指令（需要 wake_prefix） ----------------

    @filter.command("quiet_status", alias={"闭嘴状态", "沉默状态"})
    async def quiet_status_cmd(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        lines = [
            f"全局闭嘴: {'🔇 是' if self.state['global_quiet'] else '🔊 否'}",
            f"本群 (id={gid}): {'🔇 闭嘴中' if self._is_quiet(gid) else '🔊 正常'}",
            f"闭嘴群数: {len(self.state['quiet_groups'])}",
            f"闭嘴名单: {self.state['quiet_groups']}",
            f"禁言自动闭嘴: {self.state.get('auto_quiet_groups', []) or '无'}",
            f"Bot防循环: {'✅ 开' if self.bot_guard_enabled else '❌ 关'} "
            f"(名单: {sorted(self.bot_qq_set) or '空'}, 模式: {self.bot_reply_mode}, "
            f"熔断轮数: {self.bot_guard_max_rounds or '不限'})",
            "登记机器人: /标记bot @某人（或直接在群里告知，LLM 会调用工具登记）",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("quiet_global_silent", alias={"qm_g_silent"})
    async def quiet_global_silent_cmd(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 仅管理员可操作")
            return
        self.state["global_quiet"] = True
        self._save_state()
        yield event.plain_result("🔇 全局闭嘴已开启")

    @filter.command("quiet_global_resume", alias={"qm_g_resume"})
    async def quiet_global_resume_cmd(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 仅管理员可操作")
            return
        self.state["global_quiet"] = False
        self._save_state()
        yield event.plain_result("🔊 全局闭嘴已关闭")

    @filter.command("quiet_clear_groups", alias={"qm_clear"})
    async def quiet_clear_groups_cmd(self, event: AstrMessageEvent):
        if not self._is_authorized(event):
            yield event.plain_result("❌ 仅管理员可操作")
            return
        self.state["quiet_groups"] = []
        self._save_state()
        yield event.plain_result("🧹 已清空所有单群闭嘴名单")