from __future__ import annotations

import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, register

try:
    from astrbot.core.agent.message import TextPart
except Exception:  # AstrBot < 4.24 fallback
    TextPart = None


@register(
    "astrbot_plugin_reply_style_guard",
    "akiby17",
    "为群聊/闲聊持续注入短回复风格锚点，并在模型偶发输出过长时进行安全截短，降低长对话后的文风漂移。",
    "1.0.0",
)
class ReplyStyleGuard(Star):
    """短回复防漂移插件。

    两层保护：
    1. on_llm_request: 每一轮临时注入“短、自然、不模仿历史长度”的风格锚点；
    2. on_llm_response: 仅对普通闲聊的最终 assistant 文本做可配置的硬长度兜底。

    设计目标不是把所有回答强行砍短，而是让普通群聊长期保持“像真人聊天”的短句风格。
    对明确要求解释/分析/教程/写作/代码等消息，默认自动跳过硬截短。
    """

    DEFAULT_BYPASS_KEYWORDS = (
        "详细", "展开", "解释", "分析", "为什么", "为何", "怎么", "如何",
        "教程", "步骤", "方法", "原理", "原因", "总结", "介绍", "说明",
        "列出", "对比", "比较", "区别", "代码", "报错", "日志", "公式",
        "计算", "推导", "证明", "翻译", "改写", "润色", "写一", "写篇",
        "文章", "报告", "论文", "不少于", "多少字", "完整", "具体",
    )

    ACTION_WORDS = (
        "点头", "摇头", "挠头", "捂脸", "扶额", "摊手", "眨眼", "歪头",
        "大拇指", "鼓掌", "叹气", "偷笑", "坏笑", "笑", "哭", "摸摸",
        "拍拍", "蹭", "抱", "挥手", "叉腰", "沉思", "缩", "抖", "看向",
    )

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        logger.info("[ReplyStyleGuard] loaded v1.0.0")

    # -------------------- config / scope --------------------
    def _enabled(self) -> bool:
        return bool(self.config.get("enabled", True))

    def _scope_allowed(self, event: AstrMessageEvent) -> bool:
        is_private = bool(event.is_private_chat())
        if is_private:
            return bool(self.config.get("apply_private", False))
        return bool(self.config.get("apply_group", True))

    @staticmethod
    def _msg_text(event: AstrMessageEvent) -> str:
        text = str(getattr(event, "message_str", "") or "").strip()
        if text:
            return text
        obj = getattr(event, "message_obj", None)
        return str(getattr(obj, "message_str", "") or "").strip()

    def _split_keywords(self, raw: Any) -> tuple[str, ...]:
        if raw is None:
            return self.DEFAULT_BYPASS_KEYWORDS
        if isinstance(raw, (list, tuple, set)):
            items = [str(x).strip() for x in raw]
        else:
            items = re.split(r"[\s,，;；|]+", str(raw))
        cleaned = tuple(x for x in items if x)
        return cleaned or self.DEFAULT_BYPASS_KEYWORDS

    def _should_bypass_hard_guard(self, event: AstrMessageEvent) -> tuple[bool, str]:
        text = self._msg_text(event)
        if not text:
            return False, "empty-message"

        # Slash commands / command-like inputs should never be altered by hard guard.
        if text.startswith(("/", "!", "！")):
            return True, "command"

        # Longer user prompts are more likely to be substantive tasks.
        threshold = max(1, int(self.config.get("bypass_user_message_length", 80)))
        if len(text) >= threshold:
            return True, f"user-message>={threshold}"

        lower = text.lower()
        for kw in self._split_keywords(self.config.get("bypass_keywords", "")):
            if kw.lower() in lower:
                return True, f"keyword:{kw}"

        return False, "casual-chat"

    # -------------------- prompt anchor --------------------
    def _build_style_hint(self, event: AstrMessageEvent) -> str:
        bypass, _ = self._should_bypass_hard_guard(event)
        target_chars = max(10, int(self.config.get("soft_target_chars", 45)))
        max_sentences = max(1, int(self.config.get("max_sentences", 2)))

        base = (
            "<chat_style_guard>"
            "这是即时聊天，不是作文。历史消息只用于理解事实、人物关系和当前话题，"
            "不要模仿历史中 assistant 回复的长度、句式、口癖密度或动作描写。"
            "普通闲聊优先只回一句自然短句，用户说得短，你也要短。"
            f"通常控制在约{target_chars}个汉字以内，最多{max_sentences}句短句。"
            "不要解释自己刚说的梗，不要把同一个意思换几种说法重复表达，"
            "不要连续堆叠称呼、自称、语气词、括号动作或人设口癖。"
            "人设通过自然措辞体现，不需要每条消息刻意证明人设。"
            "除非确有必要，同一条回复里昵称/称呼尽量不重复，口癖最多自然出现一次。"
        )
        if bypass:
            base += (
                "当前用户消息看起来是需要实质说明的任务，可以按问题需要适度展开；"
                "但仍避免重复、绕圈和无意义铺垫。"
            )
        else:
            base += "当前属于普通闲聊，请把简短自然放在完整展开之前。"

        custom = str(self.config.get("custom_style_hint", "") or "").strip()
        if custom:
            base += f" {custom}"
        base += "</chat_style_guard>"
        return base

    @filter.on_llm_request(priority=-999999)
    async def inject_style_guard(
        self,
        event: AstrMessageEvent,
        req: ProviderRequest,
    ) -> None:
        if not self._enabled() or not self._scope_allowed(event):
            return
        if not bool(self.config.get("prompt_anchor_enabled", True)):
            return

        hint = self._build_style_hint(event)

        # Preferred: temporary per-turn user content, not persisted into history.
        try:
            if TextPart is not None and hasattr(req, "extra_user_content_parts"):
                part = TextPart(text=hint)
                if hasattr(part, "mark_as_temp"):
                    part = part.mark_as_temp()
                req.extra_user_content_parts.append(part)
                if self.config.get("debug_log", False):
                    logger.info(
                        "[ReplyStyleGuard] style anchor injected via extra_user_content_parts "
                        f"| private={event.is_private_chat()} user={event.get_sender_id()}"
                    )
                return
        except Exception as e:
            logger.warning(f"[ReplyStyleGuard] temp injection failed: {e}")

        # Compatibility fallback: append to current user prompt only.
        try:
            if hasattr(req, "prompt"):
                old = str(req.prompt or "")
                req.prompt = f"{old}\n\n{hint}" if old else hint
                logger.warning(
                    "[ReplyStyleGuard] using req.prompt fallback; upgrade AstrBot >=4.24 "
                    "for temporary injection."
                )
                return
        except Exception as e:
            logger.warning(f"[ReplyStyleGuard] prompt fallback failed: {e}")

    # -------------------- deterministic hard guard --------------------
    def _strip_action_parentheses(self, text: str) -> str:
        if not bool(self.config.get("strip_stage_directions", True)):
            return text

        def repl(match: re.Match[str]) -> str:
            inner = (match.group(1) or match.group(2) or "").strip()
            if any(word in inner for word in self.ACTION_WORDS):
                return ""
            return match.group(0)

        # Keep ordinary explanatory parentheses; only remove obvious stage-direction ones.
        text = re.sub(r"（([^（）]{1,20})）|\(([^()]{1,20})\)", repl, text)
        return re.sub(r"\s{2,}", " ", text).strip()

    @staticmethod
    def _sentence_chunks(text: str) -> list[str]:
        # Keep terminal punctuation attached to the sentence.
        parts = re.findall(r".*?(?:[。！？!?~～]+|$)", text, flags=re.S)
        return [p.strip() for p in parts if p and p.strip()]

    @staticmethod
    def _cut_at_natural_boundary(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text.strip()
        head = text[:limit]

        # Prefer ending at a natural punctuation mark near the end.
        candidates = [
            head.rfind("。"), head.rfind("！"), head.rfind("？"),
            head.rfind("!"), head.rfind("?"), head.rfind("；"),
            head.rfind(";"), head.rfind("，"), head.rfind(","),
            head.rfind("、"), head.rfind(" "),
        ]
        pos = max(candidates)
        # Only use the boundary if it is not too early.
        if pos >= max(8, int(limit * 0.55)):
            out = head[: pos + 1].rstrip("，,；;：:、 ")
            if out.endswith(("。", "！", "？", "!", "?", "~", "～")):
                return out
            return out + "。"

        return head.rstrip("，,；;：:、 ") + "…"

    def _smart_shorten(self, text: str) -> str:
        if not text:
            return text

        # Do not damage code / structured answers. These should normally have been bypassed anyway.
        if "```" in text:
            return text

        cleaned = self._strip_action_parentheses(text)
        cleaned = re.sub(r"[ \t]+", " ", cleaned)
        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()

        max_sentences = max(1, int(self.config.get("max_sentences", 2)))
        hard_max = max(20, int(self.config.get("hard_max_chars", 90)))

        chunks = self._sentence_chunks(cleaned)
        if len(chunks) > max_sentences:
            cleaned = "".join(chunks[:max_sentences]).strip()

        if len(cleaned) > hard_max:
            cleaned = self._cut_at_natural_boundary(cleaned, hard_max)

        return cleaned.strip()

    @filter.on_llm_response()
    async def guard_llm_response(
        self,
        event: AstrMessageEvent,
        resp: LLMResponse,
    ) -> None:
        if not self._enabled() or not self._scope_allowed(event):
            return
        if not bool(self.config.get("hard_guard_enabled", True)):
            return
        if resp is None or str(getattr(resp, "role", "")) != "assistant":
            return
        if bool(getattr(resp, "is_chunk", False)):
            return
        if getattr(resp, "tools_call_args", None):
            return

        bypass, reason = self._should_bypass_hard_guard(event)
        if bypass:
            if self.config.get("debug_log", False):
                logger.info(f"[ReplyStyleGuard] hard guard bypassed | reason={reason}")
            return

        try:
            original = str(getattr(resp, "completion_text", "") or "").strip()
        except Exception:
            original = ""
        if not original:
            return

        shortened = self._smart_shorten(original)
        if shortened and shortened != original:
            try:
                resp.completion_text = shortened
                logger.info(
                    "[ReplyStyleGuard] shortened casual reply "
                    f"| {len(original)} -> {len(shortened)} chars"
                )
            except Exception as e:
                logger.warning(f"[ReplyStyleGuard] failed to replace completion_text: {e}")
        elif self.config.get("debug_log", False):
            logger.info(
                "[ReplyStyleGuard] reply already concise "
                f"| len={len(original)} reason={reason}"
            )

    @filter.command("style_guard_status")
    async def style_guard_status(self, event: AstrMessageEvent):
        """查看本插件当前配置与本条消息是否会触发硬截短。"""
        bypass, reason = self._should_bypass_hard_guard(event)
        yield event.plain_result(
            "Reply Style Guard\n"
            f"状态：{'开启' if self._enabled() else '关闭'}\n"
            f"当前范围：{'私聊' if event.is_private_chat() else '群聊'} "
            f"({'生效' if self._scope_allowed(event) else '不生效'})\n"
            f"提示词锚点：{'开启' if self.config.get('prompt_anchor_enabled', True) else '关闭'}\n"
            f"硬长度兜底：{'开启' if self.config.get('hard_guard_enabled', True) else '关闭'}\n"
            f"软目标：约 {int(self.config.get('soft_target_chars', 45))} 字\n"
            f"硬上限：{int(self.config.get('hard_max_chars', 90))} 字\n"
            f"最多句数：{int(self.config.get('max_sentences', 2))}\n"
            f"当前消息硬截短：{'跳过' if bypass else '启用'}（{reason}）"
        )

    async def terminate(self):
        logger.info("[ReplyStyleGuard] unloaded")
