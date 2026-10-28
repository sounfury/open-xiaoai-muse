"""Hermes Agent 后端：OpenAI 兼容接口（API Server）加上 Hermes 自己的约定。

- 一次唤醒一个服务端会话：进入连续对话时生成新的会话 ID，每轮请求带 `X-Hermes-Session-Id`、
  只发当前一句，Hermes 从 state.db 读回本次唤醒的历史（含工具调用结果和读过的 skill 正文）；
  退出（再见 / 超时 / 被打断）时丢掉，下次唤醒换新的。bridge 本地不保存历史。
  不要把历史放进 messages：Hermes 对重复的 skill_view 只回"内容未变"，客户端自己带纯文字历史时
  模型看不到 skill 正文，也看不到之前的工具调用。
- 长期记忆作用域：`X-Hermes-Session-Key` = session_key，跨唤醒不变。
- 流式回复里的 `event: hermes.tool.progress` 产出 ("tool", dict)：连续对话据此念"稍等"
  （hermes.tool_filler），放音乐等工具调用后结束对话（hermes.end_after_tools）。

配置段 APP_CONFIG["hermes"]；环境变量 HERMES_ENABLE / HERMES_BASE_URL / HERMES_API_KEY / HERMES_MODEL。
"""

import time
import uuid
from typing import Any

from core.openai import OpenAIManager
from core.utils.config import ConfigManager
from core.utils.logger import logger


class HermesManager(OpenAIManager):
    """Hermes Agent API Server 后端。"""

    NAME = "hermes"
    LABEL = "Hermes"
    CONFIG_SECTION = "hermes"
    ENABLE_ENV = ("HERMES_ENABLE",)
    ENV_PREFIX = "HERMES"

    # 长期记忆作用域的请求头（值是 session_key）；留空不发
    _session_header = "X-Hermes-Session-Key"
    # 本次唤醒的会话 ID 请求头（Hermes 要求配置 api_key 才接受）；留空不发
    _session_id_header = "X-Hermes-Session-Id"
    _conversation_id: str | None = None
    _session_id_warned = False

    @classmethod
    def reload_from_config(cls, enabled: bool | None = None):
        super().reload_from_config(enabled=enabled)
        config = ConfigManager.instance().get_app_config(cls.CONFIG_SECTION, {}) or {}
        cls._session_header = str(config.get("session_header", "X-Hermes-Session-Key") or "").strip()
        cls._session_id_header = str(config.get("session_id_header", "X-Hermes-Session-Id") or "").strip()

    @classmethod
    def begin_conversation(cls):
        """唤醒进入连续对话时调用：开一个新的服务端会话，本次唤醒的每一轮都用它。"""
        cls._conversation_id = cls._new_session_id()
        logger.info(f"[{cls.LABEL}] 新会话: {cls._conversation_id}")

    @classmethod
    def end_conversation(cls):
        """退出连续对话（说再见、超时、被打断）时调用：丢掉会话 ID，下次唤醒换新的。"""
        if cls._conversation_id:
            logger.info(f"[{cls.LABEL}] 会话结束: {cls._conversation_id}")
        cls._conversation_id = None

    @classmethod
    def _headers(cls) -> dict[str, str]:
        headers = super()._headers()
        # 长期记忆作用域，跨唤醒不变
        if cls._session_header and cls._session_key:
            headers[cls._session_header] = cls._session_key
        # 会话 ID：连续对话里用本次唤醒的 ID；单次请求（API / 拦截小爱指令）每次新开一个，
        # 避免相同的话被服务端按内容指纹并进同一个旧会话
        if cls._session_id_header:
            if cls._api_key:
                headers[cls._session_id_header] = cls._conversation_id or cls._new_session_id()
            elif not cls._session_id_warned:
                cls._session_id_warned = True
                logger.warning(
                    f"[{cls.LABEL}] 没有配置 api_key，Hermes 不接受 {cls._session_id_header}："
                    "每轮只发当前一句，后端看不到本次唤醒前面的对话"
                )
        return headers

    @classmethod
    def _sse_events(cls, event: str | None, data: Any) -> list[tuple[str, Any]] | None:
        if event == "hermes.tool.progress":
            # {"tool", "emoji", "label", "toolCallId", "status": "running" | "completed"}
            return [("tool", data)] if isinstance(data, dict) else []
        return None

    @classmethod
    def _new_session_id(cls) -> str:
        return f"xiaoai-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
