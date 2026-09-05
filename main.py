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

用法示例（在配置了 target=["沙绫"]、silent=["闭嘴"]、resume=["张嘴"] 时）：
  - 「沙绫 闭嘴」  → 当前群闭嘴，bot 不再回复任何消息
  - 「沙绫张嘴」   → 当前群张嘴，bot 恢复正常
  - 「全群闭嘴」   → 所有群都闭嘴（仅管理员）
  - 「全群张嘴」   → 解除全局闭嘴
  - 「/闭嘴状态」   → 查看当前闭嘴状态（标准指令，需 wake_prefix）
"""

from __future__ import annotations

import json
import os
from typing import Any

from astrbot.api.star import Star, Context, register
from astrbot.api.event import AstrMessageEvent, filter
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


@register(
    "astrbot_plugin_quiet_mode",
    "user",
    "可配置的闭嘴/张嘴控制：让指定人格闭嘴不插话（含最后感言）",
    "1.3.0",
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

        logger.info(
            f"[quiet_mode] 已加载 | target={self.target_keywords} "
            f"silent={self.silent_triggers} resume={self.resume_triggers} "
            f"g_silent={self.global_silent_triggers} g_resume={self.global_resume_triggers} "
            f"admin_only={self.admin_only} admin_qqs={self.admin_qqs} "
            f"final_reply={self.final_reply_enabled} "
            f"silent_intercept={self.silent_intercept_enabled} "
            f"llm_intercept={self.llm_intercept_enabled} "
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
                    # 兜底类型保护
                    if not isinstance(data["quiet_groups"], list):
                        data["quiet_groups"] = []
                    return data
            except Exception as e:
                logger.warning(f"[quiet_mode] 加载状态失败，将重置: {e}")
        return {"global_quiet": False, "quiet_groups": []}

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.error(f"[quiet_mode] 保存状态失败: {e}")

    def _is_quiet(self, group_id: str) -> bool:
        return self.state["global_quiet"] or group_id in self.state["quiet_groups"]

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

    def _match_command(self, text_lower: str) -> dict | None:
        """
        返回匹配的指令 dict：
          - {"action": "silent"|"resume", "scope": "global"}
          - {"action": "silent"|"resume", "scope": "group", "target": <kw>}
        返回 None 表示不匹配。
        """
        # 1) 全局指令（不需要 target）
        g_silent = self._contains_any(text_lower, self.global_silent_triggers)
        g_resume = self._contains_any(text_lower, self.global_resume_triggers)
        if g_silent or g_resume:
            return {
                "action": "silent" if g_silent else "resume",
                "scope": "global",
            }

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

    def _is_authorized(self, event: AstrMessageEvent) -> bool:
        if not self.admin_only:
            return True
        # 配置中显式指定的 admin QQ
        if self.admin_qqs:
            try:
                return str(event.get_sender_id()) in self.admin_qqs
            except Exception:
                return False
        # 否则使用 AstrBot 内置 admin 判断
        try:
            return bool(event.is_admin())
        except Exception:
            return False

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
        self, event: AstrMessageEvent, action: str, trigger_text: str
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
        失败时返回可配置的兜底文案，绝不抛异常。
        """
        fallback = (
            self.silent_fallback_text if action == "silent" else self.resume_fallback_text
        )
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
            user_prompt = f"管理员刚刚在群里对你说：「{trigger_text}」。{ask}"

            # 2) 触发所有 on_llm_request 钩子（livingmemory 等插件注入记忆召回）
            #    注意：必须在 stop_event 之前调用，否则钩子循环会在第一个 handler 后提前返回
            req = event.request_llm(
                prompt=user_prompt,
                system_prompt=system_prompt,
                contexts=contexts,
            )
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

            cmd = self._match_command(text.lower())

            if cmd:
                # 权限检查：非管理员触发闭嘴/张嘴 → 静默丢弃（不告诉对方，避免泄露规则）
                if not self._is_authorized(event):
                    logger.debug(
                        f"[quiet_mode] 非管理员触发闭嘴/张嘴指令 sender={event.get_sender_id()} 已静默丢弃"
                    )
                    event.stop_event()
                    return

                if cmd["scope"] == "global":
                    self.state["global_quiet"] = cmd["action"] == "silent"
                    self._save_state()
                    logger.info(
                        f"[quiet_mode] admin={event.get_sender_id()} 切换全局闭嘴 → {self.state['global_quiet']}"
                    )
                else:
                    self._set_group_quiet(gid, cmd["action"] == "silent")
                    logger.info(
                        f"[quiet_mode] admin={event.get_sender_id()} 切换群 {gid} 闭嘴 → {cmd['action']=='silent'}"
                    )

                # 1.1.0: 先切换状态（拦截已生效/解除），阻断本条消息进入后续 handler 与 LLM，
                # 然后调用一次 LLM 生成人格感言，并用 event.send() 直发。
                # （不能用 yield：stop_event() 后 pipeline 不再把结果送进 RespondStage）
                event.stop_event()

                if self.final_reply_enabled:
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

    # ---------------- 标准指令（需要 wake_prefix） ----------------

    @filter.command("quiet_status", alias={"闭嘴状态", "沉默状态"})
    async def quiet_status_cmd(self, event: AstrMessageEvent):
        gid = str(event.get_group_id())
        lines = [
            f"全局闭嘴: {'🔇 是' if self.state['global_quiet'] else '🔊 否'}",
            f"本群 (id={gid}): {'🔇 闭嘴中' if self._is_quiet(gid) else '🔊 正常'}",
            f"闭嘴群数: {len(self.state['quiet_groups'])}",
            f"闭嘴名单: {self.state['quiet_groups']}",
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