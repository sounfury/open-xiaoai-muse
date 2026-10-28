"""外部对话后端：统一接口（ChatBackend）和注册表。

before_wakeup 返回的路由名（"hermes" / "openai" / "openclaw" / "qwenpaw"）在这里对应到后端类；
连续对话（core/external_conversation.py）、唤醒分发（core/wakeup_session.py）和 MainApp 只通过
ChatBackend 的公开方法使用后端，不访问各后端的私有成员。

新增后端：写一个继承 ChatBackend 的 Manager（实现下面"必须实现"的方法），再在 _REGISTRY 里加一行。
"小智"不在这里：它把原始音频流给服务端，由 WakeupSessionManager 单独处理。
"""

from __future__ import annotations

import importlib
from collections.abc import AsyncIterator
from typing import Any

from core.utils.base import get_env
from core.utils.config import ConfigManager


class ChatBackend:
    """外部对话后端的基类（classmethod 单例风格，和现有 Manager 一致）。

    子类必须实现：
      initialize_from_config(enabled=None) / connect() / close() / is_enabled() / is_connected()
      set_session_key(key) / get_tts_speaker_for_session_key(key=None)
      send(text, wait_response) / send_and_play_reply(text, wait_response)
      _send_and_track(text) -> run_id | None：发出请求，不等回复
      _wait_response(run_id) -> str | None：等这个请求的回复
      _play_response_with_tts(text, tts_speaker=None, playback_token=None)
    可选：supports_streaming() + stream_reply(text)（流式）、begin_conversation() / end_conversation()。
    """

    NAME = ""  # 路由名：before_wakeup 的返回值、after_wakeup 的 source
    LABEL = ""  # 日志和提示里的名字
    CONFIG_SECTION = ""  # APP_CONFIG 里的配置段
    ENABLE_ENV: tuple[str, ...] = ()  # 启用开关的环境变量，按顺序取第一个非空的
    DEFAULT_SESSION_KEY = ""

    _session_key: str | None = None
    _tts_provider: str | None = None
    _rule_prompt = ""
    _rule_prompt_for_skill = ""

    @classmethod
    def enabled_by_env(cls) -> bool:
        for name in cls.ENABLE_ENV:
            value = get_env(name)
            if value:
                return value.strip().lower() in ("1", "true", "yes")
        return False

    # ---- 会话 ----

    @classmethod
    def get_session_key(cls) -> str | None:
        return cls._session_key

    @classmethod
    def reset_session_key(cls):
        """每次唤醒前恢复配置里的 session_key（before_wakeup 可以再用 set_session_key 改）。"""
        config = ConfigManager.instance().get_app_config(cls.CONFIG_SECTION, {}) or {}
        cls._session_key = config.get("session_key", cls.DEFAULT_SESSION_KEY)

    @classmethod
    def begin_conversation(cls):
        """进入连续对话时调用；需要服务端会话的后端覆盖它。"""

    @classmethod
    def end_conversation(cls):
        """退出连续对话（再见 / 超时 / 被打断）时调用。"""

    # ---- 请求 ----

    @classmethod
    def with_rule_prompt(cls, text: str, for_skill: bool = False) -> str:
        """在用户的话后面追加 rule_prompt（for_skill：rule_prompt_for_skill）。"""
        rule = cls._rule_prompt_for_skill if for_skill else cls._rule_prompt
        return f"{text}\n{rule}" if rule else text

    @classmethod
    def supports_streaming(cls) -> bool:
        """连续对话是否走 stream_reply()。"""
        return False

    @classmethod
    async def stream_reply(cls, text: str) -> AsyncIterator[tuple[str, Any]]:
        """流式回复：产出 ("text", str) / ("tool", dict)；失败时抛异常。"""
        raise NotImplementedError(f"{cls.LABEL} 不支持流式回复")
        yield  # pragma: no cover

    @classmethod
    async def submit(cls, text: str) -> str | None:
        """发出请求，不等回复（调用方可以先播提示音）；返回交给 wait_reply 的 run_id。"""
        return await cls._send_and_track(text)

    @classmethod
    async def wait_reply(cls, run_id: str) -> str | None:
        try:
            return await cls._wait_response(run_id)
        finally:
            speakers = getattr(cls, "_response_tts_speakers", None)
            if isinstance(speakers, dict):
                speakers.pop(run_id, None)

    # ---- 播报 ----

    @classmethod
    def tts_provider(cls) -> str | None:
        return cls._tts_provider

    @classmethod
    async def play_text(
        cls,
        text: str,
        tts_speaker: str | None = None,
        playback_token: int | None = None,
    ):
        """用这个后端配置的 TTS 播一段文字（after_wakeup 的"再见"也用它，和回复同一个声音）。"""
        await cls._play_response_with_tts(
            text, tts_speaker=tts_speaker, playback_token=playback_token
        )

    # 以下由子类实现

    @classmethod
    def initialize_from_config(cls, enabled: bool | None = None):
        raise NotImplementedError

    @classmethod
    async def connect(cls) -> bool:
        raise NotImplementedError

    @classmethod
    async def close(cls):
        raise NotImplementedError

    @classmethod
    def is_enabled(cls) -> bool:
        raise NotImplementedError

    @classmethod
    def is_connected(cls) -> bool:
        raise NotImplementedError

    @classmethod
    def set_session_key(cls, session_key: str):
        raise NotImplementedError

    @classmethod
    def get_tts_speaker_for_session_key(cls, session_key: str | None = None) -> str | None:
        raise NotImplementedError

    @classmethod
    async def send(cls, text: str, wait_response: bool = False) -> str | None:
        """单次请求：wait_response=True 返回回复文字，否则返回 run_id；失败返回 None。"""
        raise NotImplementedError

    @classmethod
    async def send_and_play_reply(cls, text: str, wait_response: bool = False) -> str | None:
        """单次请求并播报回复。"""
        raise NotImplementedError

    @classmethod
    async def _send_and_track(cls, text: str) -> str | None:
        raise NotImplementedError

    @classmethod
    async def _wait_response(cls, run_id: str) -> str | None:
        raise NotImplementedError

    @classmethod
    async def _play_response_with_tts(
        cls,
        text: str,
        tts_speaker: str | None = None,
        playback_token: int | None = None,
    ):
        raise NotImplementedError


# 路由名 → 后端类（延迟导入：各后端依赖较重，用到才加载）
_REGISTRY: dict[str, str] = {
    "hermes": "core.hermes:HermesManager",
    "openai": "core.openai:OpenAIManager",
    "openclaw": "core.openclaw:OpenClawManager",
    "qwenpaw": "core.qwenpaw:QwenPawManager",
}


def backend_names() -> tuple[str, ...]:
    return tuple(_REGISTRY)


def _load(path: str) -> type[ChatBackend]:
    module_name, class_name = path.split(":")
    return getattr(importlib.import_module(module_name), class_name)


def get_backend(name: str | None) -> type[ChatBackend] | None:
    path = _REGISTRY.get(name or "")
    return _load(path) if path else None


def all_backends() -> list[type[ChatBackend]]:
    return [_load(path) for path in _REGISTRY.values()]


def enabled_backend_names() -> list[str]:
    """环境变量启用了的后端（HERMES_ENABLE、OPENAI_ENABLE 等）。"""
    return [backend.NAME for backend in all_backends() if backend.enabled_by_env()]
