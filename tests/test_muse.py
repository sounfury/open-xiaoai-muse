"""小爱管家（"你好缪斯"）唤醒词与路由测试，无需音箱 / 原生扩展 / 模型。

    python -m pytest tests/test_muse*.py
    python -m unittest discover -s tests -p "test_muse*.py"
"""

import importlib
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Rust 扩展可能没有编译：用空模块占位（与其他测试一致）
sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

muse = importlib.import_module("core.muse")

def make_config_getter(app_config: dict):
    """模拟 ConfigManager.get_app_config 的嵌套路径查询。"""

    def get(path=None, default=None):
        if not path:
            return app_config
        value = app_config
        for key in path.split("."):
            if not isinstance(value, dict) or key not in value:
                return default
            value = value[key]
        return value

    return get


def load_repo_config():
    """以独立模块名加载仓库里的 config.py，不影响 sys.modules["config"]。"""
    spec = importlib.util.spec_from_file_location("muse_test_config", ROOT / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def env(**values):
    """清掉 muse 相关环境变量后再设置，避免受本机环境影响。"""
    base = {"MUSE_ENABLE": "", "MUSE_WAKE_WORDS": "", "MUSE_KWS_WAKE_WORDS": ""}
    base.update(values)
    return mock.patch.dict(os.environ, base)


class FakeSpeaker:
    def __init__(self, events=None):
        self.events = events if events is not None else []

    async def play(self, text=None, **kwargs):
        self.events.append(("play", text))
        return True

    async def abort_xiaoai(self):
        self.events.append(("abort",))
        return True

    async def wake_up(self, awake=True, silent=True):
        self.events.append(("wake_up", awake))
        return True

    async def stop_device_audio(self):
        self.events.append(("stop_device_audio",))


# ---------------------------------------------------------------------------
# 唤醒词文本解析
# ---------------------------------------------------------------------------


class MuseTextParsingTest(unittest.TestCase):
    WAKE_WORDS = ["你好缪斯"]

    def parse(self, text, mode="prefix", words=None):
        return muse.parse_muse_text(text, words or self.WAKE_WORDS, mode)

    def test_bare_wake_word(self):
        for text in ("你好缪斯", "你好缪斯。", "你好缪斯！", " 你好缪斯 ", "你好，缪斯", "你好缪斯啊"):
            with self.subTest(text=text):
                match = self.parse(text)
                self.assertIsNotNone(match)
                self.assertTrue(match.is_bare)
                self.assertEqual(match.content, "")
                self.assertEqual(match.wake_word, "你好缪斯")

    def test_wake_word_with_content(self):
        cases = {
            "你好缪斯，我今晚想洗澡": "我今晚想洗澡",
            "你好缪斯我今晚想洗澡": "我今晚想洗澡",
            "你好缪斯 我今晚想洗澡": "我今晚想洗澡",
            "你好 缪斯, 我今晚想洗澡": "我今晚想洗澡",
            "你好，缪斯！把台灯调暗一点": "把台灯调暗一点",
            "　你好缪斯：　打开热水器": "打开热水器",  # 全角空格 / 全角冒号
            "你好缪斯……播放 Spotify": "播放 Spotify",
            "你好缪斯、现在几点了？": "现在几点了？",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                match = self.parse(text)
                self.assertIsNotNone(match)
                self.assertFalse(match.is_bare)
                self.assertEqual(match.content, expected)

    def test_native_and_other_text_not_matched(self):
        for text in (
            "小爱同学",
            "小爱同学，开灯",
            "开灯",
            "你好小黑",
            "你好龙虾",
            "缪斯",
            "你好",
            "",
            "   ",
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(self.parse(text))

    def test_prefix_mode_requires_leading_wake_word(self):
        self.assertIsNone(self.parse("我想说你好缪斯帮我开灯"))
        match = self.parse("我想说你好缪斯帮我开灯", mode="contains")
        self.assertEqual(match.content, "帮我开灯")

    def test_aliases(self):
        match = self.parse("你好缪思，开灯", words=["你好缪斯", "你好缪思"])
        self.assertEqual(match.wake_word, "你好缪思")
        self.assertEqual(match.content, "开灯")

    def test_match_muse_text_is_gated_by_muse_enable(self):
        settings = muse.MuseSettings()
        with env(MUSE_ENABLE=""):
            self.assertIsNone(muse.match_muse_text("你好缪斯，开灯", settings))
        with env(MUSE_ENABLE="1"):
            self.assertEqual(muse.match_muse_text("你好缪斯，开灯", settings).content, "开灯")
            self.assertIsNone(muse.match_muse_text("小爱同学开灯", settings))


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


class MuseSettingsTest(unittest.TestCase):
    def test_defaults_without_muse_section(self):
        with env():
            settings = muse.load_settings(make_config_getter({}))
        self.assertEqual(settings.wake_word, "你好缪斯")
        self.assertEqual(settings.match_mode, "prefix")
        self.assertEqual(settings.native_suppress, "abort")
        self.assertEqual(settings.kws_wake_words, list(muse.DEFAULT_KWS_WAKE_WORDS))

    def test_invalid_values_fallback(self):
        getter = make_config_getter({"muse": {"match_mode": "x", "native_suppress": "y", "wake_word": ""}})
        with env():
            settings = muse.load_settings(getter)
        self.assertEqual(settings.match_mode, "prefix")
        self.assertEqual(settings.native_suppress, "abort")
        self.assertEqual(settings.wake_word, "你好缪斯")

    def test_repo_config_muse_section_has_only_wake_word_settings(self):
        config = load_repo_config()
        muse_cfg = config.APP_CONFIG["muse"]
        for removed in ("url", "token", "wait_text", "input_mode", "followup_timeout"):
            self.assertNotIn(removed, muse_cfg)
        self.assertIn("你好缪斯", config.APP_CONFIG["wakeup"]["keywords"])
        self.assertEqual(muse_cfg["wake_word"], "你好缪斯")

    def test_keyword_generation_enabled_for_muse(self):
        spec = importlib.util.spec_from_file_location(
            "kws_keywords_for_muse_test", ROOT / "core/services/audio/kws/keywords.py"
        )
        keywords = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(keywords)
        disabled = {
            "XIAOZHI_ENABLE": "",
            "OPENCLAW_ENABLE": "",
            "OPENCLAW_ENABLED": "",
            "HERMES_ENABLE": "",
            "OPENAI_ENABLE": "",
            "QWENPAW_ENABLE": "",
        }
        with mock.patch.dict(os.environ, {**disabled, "MUSE_ENABLE": ""}):
            self.assertFalse(keywords.should_generate_keywords()[0])
        with mock.patch.dict(os.environ, {**disabled, "MUSE_ENABLE": "1"}):
            self.assertEqual(keywords.should_generate_keywords(), (True, ""))

    def test_startup_entrypoints_prepare_keywords_for_muse(self):
        start_sh = (ROOT / "scripts/start.sh").read_text(encoding="utf8")
        self.assertIn('[[ "$MUSE_ENABLE_VALUE" =~ ^(1|true|yes)$ ]]', start_sh)


# ---------------------------------------------------------------------------
# before_wakeup 路由（config.py）
# ---------------------------------------------------------------------------


class BeforeWakeupRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = load_repo_config()
        self.app_config = self.config.APP_CONFIG
        patcher = mock.patch.object(
            muse, "_app_config", side_effect=make_config_getter(self.app_config)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def route(self, text, source):
        speaker = FakeSpeaker()
        app = mock.AsyncMock()
        result = await self.config.before_wakeup(speaker, text, source, app)
        return result, speaker.events, app

    async def test_muse_kws_wake_word_routes_to_hermes(self):
        with env(MUSE_ENABLE="1"):
            result, events, _ = await self.route("你好缪斯", "kws")
        self.assertEqual(result, "hermes")
        self.assertEqual(events, [])

    async def test_muse_xiaoai_text_routes_to_hermes_and_suppresses_native(self):
        for text in ("你好缪斯，我今晚想洗澡", "你好缪斯我今晚想洗澡", "你好缪斯"):
            with self.subTest(text=text), env(MUSE_ENABLE="1"):
                result, events, _ = await self.route(text, "xiaoai")
                self.assertEqual(result, "hermes")
                self.assertEqual(events, [("abort",)])

    async def test_native_suppress_modes(self):
        self.app_config["muse"]["native_suppress"] = "none"
        with env(MUSE_ENABLE="1"):
            result, events, _ = await self.route("你好缪斯，开灯", "xiaoai")
        self.assertEqual((result, events), ("hermes", []))

        self.app_config["muse"]["native_suppress"] = "dialog"
        xiaoai = types.SimpleNamespace(
            last_recognize_dialog_id="dialog-9", _suppress_dialog=mock.AsyncMock()
        )
        with env(MUSE_ENABLE="1"), mock.patch.object(muse, "get_xiaoai", return_value=xiaoai):
            result, events, _ = await self.route("你好缪斯，开灯", "xiaoai")
        self.assertEqual((result, events), ("hermes", []))
        xiaoai._suppress_dialog.assert_awaited_once_with("dialog-9", "小爱管家接管")

    async def test_native_and_existing_routes_unchanged(self):
        cases = [
            ("打开台灯", "xiaoai", None, []),
            ("小爱同学打开台灯", "xiaoai", None, []),
            ("今天天气怎么样", "xiaoai", None, []),
            ("召唤龙虾", "xiaoai", "openclaw", [("abort",)]),
            ("召唤小智", "xiaoai", "xiaozhi", [("abort",)]),
            ("你好龙虾", "kws", "openclaw", [("play", "龙虾来了")]),
            ("你好小黑", "kws", "openai", [("play", "小黑来了")]),
            ("你好小智", "kws", "xiaozhi", [("play", "小智来了")]),
            ("随便什么", "kws", None, []),
        ]
        for muse_enable in ("", "1"):
            for text, source, expected, expected_events in cases:
                with self.subTest(text=text, source=source, muse=muse_enable), env(MUSE_ENABLE=muse_enable):
                    result, events, _ = await self.route(text, source)
                    self.assertEqual(result, expected)
                    self.assertEqual(events, expected_events)

    async def test_muse_disabled_leaves_muse_text_to_previous_logic(self):
        with env(MUSE_ENABLE=""):
            self.assertEqual(await self.route("你好缪斯", "kws"), (None, [], mock.ANY))
            result, events, _ = await self.route("你好缪斯，我今晚想洗澡", "xiaoai")
        self.assertIsNone(result)
        self.assertEqual(events, [])
