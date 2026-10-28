"""会话灯效与播报中打断的回归测试：无需音箱硬件，用假 speaker 记录 ubus 命令。"""

import asyncio
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# 开发机上可能没有设备侧依赖，缺失时用最小桩替代，仅为让模块可导入。
try:
    import open_xiaoai_server  # noqa: F401
except ImportError:
    _oxs = types.ModuleType("open_xiaoai_server")
    _oxs.decode_audio = lambda *a, **k: None
    sys.modules["open_xiaoai_server"] = _oxs

try:
    import core.utils.config  # noqa: F401
except ImportError:
    _cfg_mod = types.ModuleType("core.utils.config")
    _cfg_mod.ConfigManager = type(
        "ConfigManager", (), {"instance": staticmethod(lambda: None)}
    )
    sys.modules["core.utils.config"] = _cfg_mod

import core.external_conversation as ext  # noqa: E402
import core.services.conversation_light as light_mod  # noqa: E402
from core import ref  # noqa: E402
from core.services.conversation_light import ConversationLight, effects_from_config  # noqa: E402


def show(effect):
    return f"ubus -t 1 call led show '{{\"L\":{effect}}}'"


def shut(effect):
    return f"ubus -t 1 call led shut '{{\"L\":{effect}}}'"


EFFECTS = {"listening": 9, "hearing": 41, "thinking": 2, "speaking": 3}


class FakeConfig:
    def __init__(self, values: dict):
        self.values = values

    def get_app_config(self, key, default=None):
        return self.values.get(key, default)


def make_speaker(exit_code: int = 0):
    speaker = MagicMock()
    speaker.run_shell = AsyncMock(return_value=SimpleNamespace(exit_code=exit_code))
    speaker.wake_up = AsyncMock()
    speaker.play = AsyncMock()
    speaker.stop_device_audio = AsyncMock()
    return speaker


def commands(speaker) -> list[str]:
    return [c.args[0] for c in speaker.run_shell.await_args_list]


async def drain(n: int = 20):
    for _ in range(n):
        await asyncio.sleep(0)


# ---- ConversationLight ----


def test_state_changes_shut_previous_and_show_next():
    async def main():
        speaker = make_speaker()
        light = ConversationLight(speaker, EFFECTS, asyncio.get_running_loop())
        light.set("listening")
        await drain()
        light.set("thinking")
        await drain()
        await light.close()
        assert commands(speaker) == [show(9), f"{shut(9)}; {show(2)}", shut(2)]

    asyncio.run(main())


def test_rapid_changes_only_apply_latest():
    async def main():
        speaker = make_speaker()
        light = ConversationLight(speaker, EFFECTS, asyncio.get_running_loop())
        light.set("listening")
        light.set("hearing")
        light.set("thinking")
        await drain()
        await light.close()
        assert commands(speaker) == [show(2), shut(2)]

        # 亮灯命令还没发出就熄灯：什么都不用做
        quick = make_speaker()
        light = ConversationLight(quick, EFFECTS, asyncio.get_running_loop())
        light.set("listening")
        await light.close()
        assert commands(quick) == []

    asyncio.run(main())


def test_same_effect_sends_nothing_and_off_without_light_is_silent():
    async def main():
        speaker = make_speaker()
        effects = {**EFFECTS, "hearing": 9}
        light = ConversationLight(speaker, effects, asyncio.get_running_loop())
        light.set("listening")
        await drain()
        light.set("hearing")
        await drain()
        await light.close()
        assert commands(speaker) == [show(9), shut(9)]

        idle = make_speaker()
        light = ConversationLight(idle, EFFECTS, asyncio.get_running_loop())
        await light.close()
        assert commands(idle) == []

    asyncio.run(main())


def test_speaking_light_is_refreshed(monkeypatch):
    monkeypatch.setattr(light_mod, "REFRESH_INTERVAL_S", 0.01)

    async def main():
        speaker = make_speaker()
        light = ConversationLight(speaker, EFFECTS, asyncio.get_running_loop())
        light.set("speaking")
        await asyncio.sleep(0.05)
        await light.close()
        cmds = commands(speaker)
        assert cmds[0] == show(3)
        assert any("led status" in c and show(3) in c for c in cmds[1:-1])
        assert cmds[-1] == shut(3)

    asyncio.run(main())


def test_failed_show_is_still_shut_later():
    async def main():
        speaker = make_speaker(exit_code=1)
        light = ConversationLight(speaker, EFFECTS, asyncio.get_running_loop())
        light.set("listening")
        await drain()
        await light.close()
        assert commands(speaker) == [show(9), shut(9)]

    asyncio.run(main())


def test_set_from_other_thread():
    async def main():
        import threading

        speaker = make_speaker()
        light = ConversationLight(speaker, EFFECTS, asyncio.get_running_loop())
        thread = threading.Thread(target=light.set, args=("hearing",))
        thread.start()
        thread.join()
        await drain()
        await light.close()
        assert commands(speaker) == [show(41), shut(41)]

    asyncio.run(main())


@pytest.mark.parametrize("config", [
    {"enabled": False, "effect_id": 9},
    None,
    "on",
    ["enabled"],
])
def test_effects_disabled(config):
    assert set(effects_from_config(config).values()) == {None}


def test_effects_defaults_and_native_listening():
    assert effects_from_config({"enabled": True}) == EFFECTS
    custom = effects_from_config({"enabled": True, "effect_id": 7, "thinking_effect_id": 0})
    assert custom["listening"] == 7 and custom["thinking"] is None
    native = effects_from_config({"enabled": True}, native_listening=True)
    assert native["listening"] is None and native["hearing"] is None and native["speaking"] == 3


# ---- ExternalConversationController ----


def fake_backend(**overrides):
    """按 core/backends.py ChatBackend 公开接口做的假后端（配置段 openclaw）。"""
    backend = SimpleNamespace(
        NAME="openclaw",
        LABEL="OpenClaw",
        CONFIG_SECTION="openclaw",
        begin_conversation=lambda: None,
        end_conversation=lambda: None,
        supports_streaming=lambda: False,
        tts_provider=lambda: None,
        get_tts_speaker_for_session_key=lambda: None,
        get_session_key=lambda: None,
        with_rule_prompt=lambda text, for_skill=False: text,
        play_text=AsyncMock(),
        submit=AsyncMock(return_value=None),
        wait_reply=AsyncMock(return_value=None),
    )
    for key, value in overrides.items():
        setattr(backend, key, value)
    return backend


def OpenClawLike():
    return ext.ExternalConversationController(fake_backend())


class FakeKWS:
    def __init__(self):
        self.handler = None
        self.armed = []

    def arm_barge_in(self, handler):
        self.handler = handler
        self.armed.append(True)

    def disarm_barge_in(self):
        self.handler = None
        self.armed.append(False)


@pytest.fixture
def env(monkeypatch):
    """隔离全局状态：配置、speaker、设备 RPC、KWS、回采状态。"""
    config = FakeConfig({
        "openclaw.listening_light": {"enabled": True},
        "openclaw.input_mode": "local_asr",
        "wakeup.timeout": 1,
        "barge_in.enabled": True,
    })
    monkeypatch.setattr(ext, "ConfigManager", SimpleNamespace(instance=lambda: config))
    oxs = SimpleNamespace(
        stop_recording=AsyncMock(),
        start_recording=AsyncMock(),
        stop_tts_playback=MagicMock(),
        begin_playback_session=MagicMock(return_value=7),
    )
    monkeypatch.setattr(ext, "open_xiaoai_server", oxs)
    monkeypatch.setattr(ext, "_NOTIFY_PCM", None)
    monkeypatch.setattr(ext, "_SEND_PCM", None)
    speaker = make_speaker()
    kws = FakeKWS()
    xiaoai = SimpleNamespace(
        live=True,
        echo_reference_live=lambda: xiaoai.live,
        near_end_ratio_db=lambda: -5.0,
        wanted=[],
        set_recording_wanted=lambda w: xiaoai.wanted.append(w),
    )
    monkeypatch.setitem(ref.GLOBAL_STATES, "speaker", speaker)
    monkeypatch.setitem(ref.GLOBAL_STATES, "vad", MagicMock())
    monkeypatch.setitem(ref.GLOBAL_STATES, "kws", kws)
    monkeypatch.setitem(ref.GLOBAL_STATES, "xiaoai", xiaoai)
    return SimpleNamespace(config=config, speaker=speaker, oxs=oxs, kws=kws, xiaoai=xiaoai)


def make_conv(loop):
    conv = OpenClawLike()
    conv.active = True
    conv._loop = loop
    conv._light = ConversationLight(
        ref.get_speaker(), effects_from_config({"enabled": True}), loop
    )
    return conv


def test_stop_turns_light_off(env):
    async def main():
        conv = make_conv(asyncio.get_running_loop())
        conv._light_state("listening")
        await drain()
        conv.stop()
        await drain()
        assert env.kws.armed == [False]

    asyncio.run(main())
    assert commands(env.speaker) == [show(9), shut(9)]


def test_light_disabled_sends_nothing(env):
    env.config.values["openclaw.listening_light"] = {"enabled": False}

    async def main():
        conv = OpenClawLike()
        conv._wait_for_speech = AsyncMock(return_value=None)
        conv._conversation_loop = AsyncMock()
        await conv.start()

    asyncio.run(main())
    assert commands(env.speaker) == []


def test_timeout_turns_listening_on(env):
    async def main():
        conv = make_conv(asyncio.get_running_loop())
        conv._wait_for_speech = AsyncMock(return_value=None)
        assert await conv._run_one_turn_with_local_asr() == "timeout"
        await drain()
        await conv._light.close()

    asyncio.run(main())
    assert commands(env.speaker) == [show(9), shut(9)]


def test_interrupted_before_listening(env):
    async def main():
        conv = make_conv(asyncio.get_running_loop())
        conv.active = False
        conv._wait_for_speech = AsyncMock(return_value=b"x")
        assert await conv._run_one_turn_with_local_asr() == "interrupted"
        conv._wait_for_speech.assert_not_awaited()

    asyncio.run(main())


def test_interrupted_loop_skips_goodbye(env):
    async def main():
        conv = make_conv(asyncio.get_running_loop())
        conv._run_one_turn_with_local_asr = AsyncMock(return_value="interrupted")
        conv._call_after_wakeup = AsyncMock()
        conv._stop_xiaoai_native_listening = AsyncMock()
        await conv._conversation_loop()
        conv._run_one_turn_with_local_asr.assert_awaited_once()
        conv._call_after_wakeup.assert_not_awaited()
        conv._stop_xiaoai_native_listening.assert_not_awaited()
        # 有回采时开场不关麦
        env.oxs.stop_recording.assert_not_awaited()

    asyncio.run(main())


def test_start_releases_light_and_recording_watchdog(env):
    async def main():
        conv = OpenClawLike()

        async def loop():
            conv._light_state("thinking")
            await drain()

        conv._conversation_loop = loop
        await conv.start()
        assert conv._light is None

    asyncio.run(main())
    assert commands(env.speaker) == [show(2), shut(2)]
    assert env.xiaoai.wanted[-1] is True


# ---- 播报中打断 ----


def tts_until_cancelled(conv):
    """模拟一段很长的播报：直到被取消才结束。"""
    started = asyncio.Event()

    async def play(text, tts_speaker=None, playback_token=None):
        started.set()
        await asyncio.sleep(3600)

    conv.backend = fake_backend(play_text=play)
    return started


def test_barge_in_stops_reply_and_skips_notify(env):
    async def main():
        conv = make_conv(asyncio.get_running_loop())
        started = tts_until_cancelled(conv)
        conv._play_notify = AsyncMock()
        conv._wait_for_silence = AsyncMock()
        task = asyncio.create_task(conv._play_reply_and_resume("很长的回答", vad=MagicMock()))
        await started.wait()
        assert env.kws.handler is not None
        env.kws.handler("你好缪斯", 16000 * 10)  # KWS 线程调用
        await asyncio.wait_for(task, 1)
        await drain()

        env.oxs.stop_tts_playback.assert_called_once_with(7)
        env.speaker.stop_device_audio.assert_awaited()
        env.oxs.stop_recording.assert_not_awaited()
        conv._play_notify.assert_not_awaited()
        conv._wait_for_silence.assert_not_awaited()
        assert conv._barge_in_from == 16000 * 10 - 350 * 16
        assert env.kws.armed == [True, False]
        assert conv._light.state == "listening"
        await conv._light.close()

    asyncio.run(main())


def test_reply_finishes_without_barge_in(env):
    async def main():
        conv = make_conv(asyncio.get_running_loop())
        conv.backend = fake_backend()
        conv._play_notify = AsyncMock()
        conv._wait_for_silence = AsyncMock()
        await conv._play_reply_and_resume("短回答", vad=MagicMock())
        conv._play_notify.assert_awaited_once()
        conv._wait_for_silence.assert_awaited_once()
        env.oxs.stop_recording.assert_not_awaited()
        env.oxs.start_recording.assert_not_awaited()
        assert env.kws.armed == [True, False]
        assert conv._barge_in_from is None
        await conv._light.close()

    asyncio.run(main())


def test_near_end_gate_ignores_own_voice(env):
    env.config.values["barge_in.near_end_min_db"] = -3

    async def main():
        conv = make_conv(asyncio.get_running_loop())
        async def play(*_args, **_kwargs):
            await asyncio.sleep(0.05)

        conv.backend = fake_backend(play_text=play)
        conv._play_notify = AsyncMock()
        conv._wait_for_silence = AsyncMock()
        task = asyncio.create_task(conv._play_reply_and_resume("回答", vad=MagicMock()))
        await drain()
        env.kws.handler("你好缪斯", 1000)  # 近端 -5 dB < -3 dB：当作音箱自己的声音
        await asyncio.wait_for(task, 1)
        env.oxs.stop_tts_playback.assert_not_called()
        assert conv._barge_in_from is None
        await conv._light.close()

    asyncio.run(main())


def test_without_echo_reference_mutes_during_reply(env):
    env.xiaoai.live = False

    async def main():
        conv = make_conv(asyncio.get_running_loop())
        conv.backend = fake_backend()
        conv._play_notify = AsyncMock()
        conv._wait_for_silence = AsyncMock()
        await conv._play_reply_and_resume("回答", vad=MagicMock())
        env.oxs.stop_recording.assert_awaited_once()
        env.oxs.start_recording.assert_awaited_once()
        assert env.kws.armed == []
        assert env.xiaoai.wanted == [False, True]
        await conv._light.close()

    asyncio.run(main())


def test_turn_after_barge_in_uses_preroll_and_strips_wake_word(env, monkeypatch):
    monkeypatch.setenv("MUSE_ENABLE", "1")
    asr = types.ModuleType("core.services.audio.asr")
    asr.ASRService = SimpleNamespace(asr=lambda pcm, sample_rate=16000: "你好缪斯，明天天气怎么样")
    monkeypatch.setitem(sys.modules, "core.services.audio.asr", asr)
    import core.muse as muse

    monkeypatch.setattr(muse, "_app_config", lambda path, default=None: default)

    async def main():
        conv = make_conv(asyncio.get_running_loop())
        conv._barge_in_from = 12345
        conv._wait_for_speech = AsyncMock(return_value=b"pcm")
        sent = []

        async def send(text):
            sent.append(text)
            return "run"

        conv.backend = fake_backend(submit=send, wait_reply=AsyncMock(return_value="晴"))
        conv._play_reply_and_resume = AsyncMock()
        assert await conv._run_one_turn_with_local_asr() == "continue"
        conv._wait_for_speech.assert_awaited_once()
        assert conv._wait_for_speech.await_args.kwargs["preroll_from"] == 12345
        assert sent == ["明天天气怎么样"]
        assert conv._barge_in_from is None
        await conv._light.close()

    asyncio.run(main())
