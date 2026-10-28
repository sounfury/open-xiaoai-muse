"""小爱管家唤醒词集合：ASR 前缀匹配、KWS 子集、keywords.txt 生成、原生 ASR 外部唤醒。

无需音箱 / 原生扩展 / 模型（sherpa_onnx.text2token 用 mock）。

    python -m pytest tests/test_muse_wake_words.py
"""

import asyncio
import importlib
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("open_xiaoai_server", types.SimpleNamespace())

muse = importlib.import_module("core.muse")

MUSE_ENV_KEYS = ("MUSE_ENABLE", "MUSE_WAKE_WORDS", "MUSE_KWS_WAKE_WORDS")


def env(**values):
    """清掉 muse 相关环境变量后再设置，避免受本机环境影响。"""
    base = {key: "" for key in MUSE_ENV_KEYS}
    base.update(values)
    return mock.patch.dict(os.environ, base)


def make_config_getter(app_config: dict):
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
    spec = importlib.util.spec_from_file_location("muse_wake_words_test_config", ROOT / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_keywords_module():
    spec = importlib.util.spec_from_file_location(
        "kws_keywords_for_muse_wake_words_test", ROOT / "core/services/audio/kws/keywords.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def default_settings() -> "muse.MuseSettings":
    with env():
        return muse.load_settings(make_config_getter({}))


def match(text, settings=None):
    return muse.match_settings_text(text, settings or default_settings())


# ---------------------------------------------------------------------------
# 默认集合
# ---------------------------------------------------------------------------


class DefaultWakeWordSetTest(unittest.TestCase):
    REQUIRED = ("你好缪斯", "hi缪斯", "嗨缪斯", "哈喽缪斯", "缪斯", "hi muse", "hey muse", "muse")
    HOMOPHONES = ("缪丝", "谬斯", "妙思", "木斯", "缪思", "谬思", "妙斯")

    def test_required_words_and_homophones_present(self):
        words = {muse.normalize_wake_text(w) for w in muse.DEFAULT_WAKE_WORDS}
        for word in self.REQUIRED:
            self.assertIn(muse.normalize_wake_text(word), words, word)
        for name in self.HOMOPHONES:
            self.assertIn(f"你好{name}", words, name)
            self.assertIn(f"嗨{name}", words, name)

    def test_repo_config_matches_code_defaults(self):
        muse_cfg = load_repo_config().APP_CONFIG["muse"]
        self.assertEqual(tuple(muse_cfg["wake_words"]), muse.DEFAULT_WAKE_WORDS)
        self.assertEqual(tuple(muse_cfg["kws_wake_words"]), muse.DEFAULT_KWS_WAKE_WORDS)
        self.assertEqual(tuple(muse_cfg["leading_fillers"]), muse.DEFAULT_LEADING_FILLERS)
        self.assertEqual(muse_cfg["match_mode"], "prefix")
        # 旧配置缺省这些键时，代码默认值一致
        self.assertEqual(default_settings().wake_word_list, list(muse.DEFAULT_WAKE_WORDS))

    def test_no_duplicates_in_defaults(self):
        for words in (muse.DEFAULT_WAKE_WORDS, muse.DEFAULT_KWS_WAKE_WORDS):
            self.assertEqual(len(muse.unique_wake_words(words)), len(words))

    def test_kws_subset_has_only_greeting_forms(self):
        all_words = {muse.normalize_wake_text(w) for w in muse.DEFAULT_WAKE_WORDS}
        for word in muse.DEFAULT_KWS_WAKE_WORDS:
            self.assertIn(muse.normalize_wake_text(word), all_words, word)
        kws = {muse.normalize_wake_text(w) for w in muse.DEFAULT_KWS_WAKE_WORDS}
        # 单独的名字和近音字不进 KWS
        for bare in ("缪斯", "muse", "缪思", "妙思", "你好妙思", "你好木斯", "你好慕斯"):
            self.assertNotIn(muse.normalize_wake_text(bare), kws, bare)
        for word in ("你好缪斯", "嗨缪斯", "hi muse", "hey muse"):
            self.assertIn(muse.normalize_wake_text(word), kws, word)

    def test_every_default_variant_matches_itself(self):
        settings = default_settings()
        for word in muse.DEFAULT_WAKE_WORDS:
            for text, content in ((word, ""), (f"{word}，开灯", "开灯"), (f"{word}开灯", "开灯"), (f"{word} 我今晚想洗澡", "我今晚想洗澡")):
                with self.subTest(text=text):
                    found = match(text, settings)
                    self.assertIsNotNone(found)
                    self.assertEqual(found.wake_word, word)
                    self.assertEqual(found.content, content)


# ---------------------------------------------------------------------------
# 匹配规则
# ---------------------------------------------------------------------------


class WakeWordMatchingRulesTest(unittest.TestCase):
    def assert_match(self, text, wake_word, content, settings=None):
        found = match(text, settings)
        self.assertIsNotNone(found, text)
        self.assertEqual((found.wake_word, found.content), (wake_word, content), text)

    def test_case_space_and_full_width_variants(self):
        cases = [
            ("Hey Muse", "hey muse"),
            ("HEY MUSE", "hey muse"),
            ("heymuse", "hey muse"),
            ("himuse", "hi muse"),
            ("Hi  Muse", "hi muse"),
            ("Hi, Muse!", "hi muse"),
            ("ＨＩ　ＭＵＳＥ", "hi muse"),  # 全角字母 + 全角空格
            ("Ｍｕｓｅ", "muse"),
            ("MUSE", "muse"),
            ("Hi缪斯", "hi缪斯"),
            ("hi 缪斯", "hi缪斯"),
            ("你好 缪斯", "你好缪斯"),
            ("你好，缪斯", "你好缪斯"),
            ("嗨～缪思", "嗨缪思"),
            ("hi▁muse", "hi muse"),  # sherpa KWS 对无 @phrase 的英文关键词返回拼接 token
            ("hey▁muse", "hey muse"),
        ]
        for text, wake_word in cases:
            with self.subTest(text=text):
                self.assert_match(text, wake_word, "")

    def test_english_word_boundary(self):
        for text in ("museum", "musea", "mused", "muses", "Muse2", "hi museum", "Hey Museum门票多少钱", "himused"):
            with self.subTest(text=text):
                self.assertIsNone(match(text))
        self.assert_match("muse, 开灯", "muse", "开灯")
        self.assert_match("muse开灯", "muse", "开灯")
        self.assert_match("Muse 我今晚想洗澡", "muse", "我今晚想洗澡")
        self.assert_match("muse，turn on the light", "muse", "turn on the light")
        self.assert_match("hey muse turn on the light", "hey muse", "turn on the light")

    def test_sentence_start_only(self):
        for text in (
            "我喜欢缪斯",
            "今天问问缪斯吧",
            "我想说你好缪斯",
            "请 hi muse 开灯",
            "the muse is here",
            "今天妙思路清晰",
            "小爱同学，你好缪斯",
        ):
            with self.subTest(text=text):
                self.assertIsNone(match(text))

    def test_non_trigger_texts(self):
        for text in (
            "小爱同学",
            "小爱同学，开灯",
            "开灯",
            "你好",
            "hi",
            "hey",
            "嗯",
            "妙思路清晰",  # 近音字单独出现不触发
            "木斯是哪个城市",
            "慕斯蛋糕怎么做",
            "妙斯",
            "那个缪斯是谁",  # "那个"不当作语气词跳过
            "umbrella",
            "",
            "   ",
            "，。！",
            None,
        ):
            with self.subTest(text=text):
                self.assertIsNone(match(text))

    def test_chinese_bare_name_has_no_boundary_rule(self):
        # 中文没有词边界：不带停顿的"缪斯开灯"必须能匹配；"缪斯特罗姆"这种罕见情况接受误判
        self.assert_match("缪斯开灯", "缪斯", "开灯")
        self.assert_match("缪思，几点了", "缪思", "几点了")
        self.assert_match("缪斯特罗姆", "缪斯", "特罗姆")

    def test_longest_match_first(self):
        settings = muse.MuseSettings(
            wake_word="你好缪斯",
            wake_word_list=["缪斯", "缪斯管家", "hey", "hey muse"],
            kws_wake_word_list=[],
        )
        self.assert_match("缪斯管家，开灯", "缪斯管家", "开灯", settings)
        self.assert_match("hey muse, 开灯", "hey muse", "开灯", settings)
        self.assert_match("你好缪斯开灯", "你好缪斯", "开灯", settings)
        # 长词因英文词边界失败时退回到较短的词
        self.assert_match("hey museum", "hey", "museum", settings)

    def test_strips_wake_word_and_punctuation(self):
        cases = {
            "你好缪斯，我今晚想洗澡": "我今晚想洗澡",
            "Hey Muse！！ 开灯": "开灯",
            "缪斯：……把台灯调暗一点": "把台灯调暗一点",
            "「你好缪斯」，开灯": "开灯",
            "  ，Hi muse、现在几点了？": "现在几点了？",
            "嗨缪斯 play Spotify": "play Spotify",
            "Muse，Muse 是谁": "Muse 是谁",  # 只去掉一次
        }
        for text, content in cases.items():
            with self.subTest(text=text):
                self.assertEqual(match(text).content, content)

    def test_only_wake_word_gives_empty_content(self):
        for text in ("你好缪斯。", "Hey Muse!", "muse", "缪斯啊", "嗨，缪斯～", "hi▁muse"):
            with self.subTest(text=text):
                found = match(text)
                self.assertTrue(found.is_bare)
                self.assertEqual(found.content, "")

    def test_leading_fillers(self):
        self.assert_match("嗯，缪斯，开灯", "缪斯", "开灯")
        self.assert_match("喂喂，你好缪斯", "你好缪斯", "")
        self.assert_match("呃 hey muse 开灯", "hey muse", "开灯")
        self.assert_match("um, hey muse, 开灯", "hey muse", "开灯")
        self.assert_match("Uh muse 开灯", "muse", "开灯")
        # 英文语气词同样要求词边界
        self.assertIsNone(match("ummuse"))
        # 可关闭
        settings = default_settings()
        settings.leading_fillers = []
        self.assertIsNone(match("嗯，缪斯，开灯", settings))
        self.assert_match("缪斯，开灯", "缪斯", "开灯", settings)

    def test_leading_fillers_from_config(self):
        with env():
            settings = muse.load_settings(make_config_getter({"muse": {"leading_fillers": ["那个"]}}))
        self.assertEqual(settings.leading_fillers, ["那个"])
        self.assert_match("那个，缪斯，开灯", "缪斯", "开灯", settings)
        self.assertIsNone(match("嗯，缪斯", settings))

    def test_contains_mode_is_opt_in(self):
        self.assertIsNone(match("我想说你好缪斯帮我开灯"))
        settings = default_settings()
        settings.match_mode = "contains"
        self.assert_match("我想说你好缪斯帮我开灯", "你好缪斯", "帮我开灯", settings)
        # contains 模式下英文同样要求两侧词边界
        self.assertIsNone(match("I am amused", settings))


# ---------------------------------------------------------------------------
# 配置覆盖
# ---------------------------------------------------------------------------


class WakeWordConfigTest(unittest.TestCase):
    def test_parse_word_list(self):
        self.assertEqual(muse.parse_word_list("小缪, hi bot，嘿小缪、 、;老缪"), ["小缪", "hi bot", "嘿小缪", "老缪"])
        self.assertEqual(muse.parse_word_list(["a", " ", None, "b "]), ["a", "b"])
        self.assertEqual(muse.parse_word_list([]), [])
        self.assertIsNone(muse.parse_word_list(None))
        self.assertIsNone(muse.parse_word_list(123))

    def test_env_overrides_config_wake_words(self):
        getter = make_config_getter({"muse": {"wake_words": ["配置缪缪"]}})
        with env(MUSE_WAKE_WORDS="小缪，Hi Bot、老缪"):
            settings = muse.load_settings(getter)
        self.assertEqual(settings.wake_word_list, ["小缪", "Hi Bot", "老缪"])
        self.assertEqual(match("小缪，开灯", settings).content, "开灯")
        self.assertEqual(match("hi bot 开灯", settings).wake_word, "Hi Bot")
        self.assertIsNone(match("配置缪缪，开灯", settings))
        self.assertIsNone(match("缪思，开灯", settings))  # 默认集合被整体替换
        # 规范唤醒词和 KWS 子集始终可匹配（保证 KWS 命中的文本能路由）
        self.assertEqual(match("你好缪斯，开灯", settings).content, "开灯")
        self.assertEqual(match("hey muse，开灯", settings).content, "开灯")

    def test_config_wake_words_and_aliases(self):
        getter = make_config_getter(
            {"muse": {"wake_words": "小缪，老缪", "wake_word_aliases": ["缪缪"], "kws_wake_words": []}}
        )
        with env():
            settings = muse.load_settings(getter)
        self.assertEqual(settings.wake_word_list, ["小缪", "老缪"])
        self.assertEqual(settings.kws_wake_words, [])
        self.assertEqual(settings.wake_words, ["你好缪斯", "小缪", "老缪", "缪缪"])
        self.assertEqual(match("老缪开灯", settings).content, "开灯")
        self.assertEqual(match("缪缪开灯", settings).wake_word, "缪缪")
        self.assertIsNone(match("hey muse", settings))

    def test_empty_env_falls_back_to_config(self):
        getter = make_config_getter({"muse": {"wake_words": ["小缪"]}})
        for value in ("", "  ", "，、"):
            with self.subTest(value=value), env(MUSE_WAKE_WORDS=value):
                self.assertEqual(muse.load_settings(getter).wake_word_list, ["小缪"])

    def test_kws_env_overrides_config(self):
        getter = make_config_getter({"muse": {"kws_wake_words": ["你好缪斯"]}})
        with env(MUSE_KWS_WAKE_WORDS="你好缪斯, Hey Muse，你好缪斯"):
            settings = muse.load_settings(getter)
        self.assertEqual(settings.kws_wake_words, ["你好缪斯", "Hey Muse"])
        with env():
            self.assertEqual(muse.load_settings(getter).kws_wake_words, ["你好缪斯"])
        with env():
            self.assertEqual(
                muse.load_settings(make_config_getter({})).kws_wake_words, list(muse.DEFAULT_KWS_WAKE_WORDS)
            )

    def test_kws_words_are_always_matchable(self):
        getter = make_config_getter({"muse": {"wake_words": ["小缪"], "kws_wake_words": ["缪斯管家"]}})
        with env():
            settings = muse.load_settings(getter)
        self.assertEqual(match("缪斯管家，开灯", settings).content, "开灯")

    def test_kws_wake_words_gated_by_muse_enable(self):
        getter = make_config_getter({})
        with env(MUSE_ENABLE=""):
            self.assertEqual(muse.kws_wake_words(getter), [])
        with env(MUSE_ENABLE="1"):
            self.assertEqual(muse.kws_wake_words(getter), list(muse.DEFAULT_KWS_WAKE_WORDS))

    def test_match_muse_text_uses_env_override(self):
        with env(MUSE_ENABLE="1", MUSE_WAKE_WORDS="小缪"):
            with mock.patch.object(muse, "_app_config", side_effect=make_config_getter({})):
                self.assertEqual(muse.match_muse_text("小缪，开灯").content, "开灯")
                self.assertIsNone(muse.match_muse_text("缪思，开灯"))


# ---------------------------------------------------------------------------
# keywords.txt 生成
# ---------------------------------------------------------------------------


def fake_text2token(oov=(), fail=()):
    """模拟 sherpa_onnx.text2token：OOV 的条目被静默丢弃，fail 里的抛异常。"""
    calls = []

    def text2token(texts, **kwargs):
        calls.append(list(texts))
        result = []
        for text in texts:
            if any(ch in text for ch in fail):
                raise ValueError("boom")
            if any(ch in text for ch in oov):
                continue
            tokens = []
            for word in text.split():
                if word.isascii():
                    tokens.append(f"▁{word}")
                else:
                    tokens.extend(word)
            result.append(tokens)
        return result

    text2token.calls = calls
    return text2token


class KeywordGenerationTest(unittest.TestCase):
    def setUp(self):
        self.keywords = load_keywords_module()
        self.app_config = {"wakeup": {"keywords": ["你好小智", "hi open claw", "你好缪斯"]}, "muse": {}}
        self.getter = make_config_getter(self.app_config)

    def test_muse_disabled_keeps_wakeup_keywords_only(self):
        with env(MUSE_ENABLE=""):
            texts = self.keywords.get_keyword_texts(self.getter)
        self.assertEqual(texts, ["你好小智", "HI OPEN CLAW", "你好缪斯"])

    def test_muse_enabled_merges_kws_subset_without_duplicates(self):
        with env(MUSE_ENABLE="1"):
            texts = self.keywords.get_keyword_texts(self.getter)
        self.assertEqual(
            texts,
            ["你好小智", "HI OPEN CLAW", "你好缪斯", "嗨缪斯", "哈喽缪斯", "HI MUSE", "HEY MUSE", "HELLO MUSE"],
        )
        # 只说"缪斯"/"muse" 不进 KWS
        self.assertNotIn("缪斯", texts)
        self.assertNotIn("MUSE", texts)

    def test_kws_subset_from_env_and_full_width_normalized(self):
        with env(MUSE_ENABLE="1", MUSE_KWS_WAKE_WORDS="ｈｉ　ｍｕｓｅ，你好缪斯"):
            texts = self.keywords.get_keyword_texts(self.getter)
        self.assertEqual(texts, ["你好小智", "HI OPEN CLAW", "你好缪斯", "HI MUSE"])

    def test_unencodable_keywords_are_skipped_with_warning(self):
        text2token = fake_text2token(oov="ǍŒ", fail="炸")
        texts = ["你好小智", "缪斯Ǎ", "HI MUSE", "炸弹", "ŒUVRE"]
        with self.assertLogs("xiaozhi", level="WARNING") as logs:
            encoded = self.keywords.encode_keywords(texts, text2token, tokens="t", tokens_type="cjkchar+bpe")
        self.assertEqual(encoded, [["你", "好", "小", "智"], ["▁HI", "▁MUSE"]])
        self.assertEqual(text2token.calls, [[t] for t in texts])  # 逐条编码
        output = "\n".join(logs.output)
        for skipped in ("缪斯Ǎ", "炸弹", "ŒUVRE"):
            self.assertIn(skipped, output)

    def test_format_keyword_line(self):
        self.assertEqual(self.keywords.format_keyword_line(["你", "好", "缪", "斯"]), "你 好 缪 斯 @你好缪斯")
        self.assertEqual(self.keywords.format_keyword_line(["▁HI", "▁MU", "S", "E"]), "▁HI ▁MU S E")
        self.assertEqual(self.keywords.format_keyword_line(["嗨", "▁MU", "S", "E"]), "嗨 ▁MU S E @嗨▁MUSE")

    def run_main(self, text2token):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        model_dir = Path(tmp.name)
        (model_dir / "tokens.txt").write_text("", encoding="utf8")
        (model_dir / "bpe.model").write_text("", encoding="utf8")

        config_stub = types.SimpleNamespace(get_app_config=self.getter)
        sherpa_stub = types.SimpleNamespace(text2token=text2token)
        with (
            env(MUSE_ENABLE="1"),
            mock.patch.object(self.keywords, "get_model_file_path", side_effect=lambda name: str(model_dir / name)),
            mock.patch.object(self.keywords.ConfigManager, "instance", return_value=config_stub),
            mock.patch.dict(sys.modules, {"sherpa_onnx": sherpa_stub}),
        ):
            status = self.keywords.main()
        output = model_dir / "keywords.txt"
        return status, output.read_text(encoding="utf8").splitlines() if output.exists() else None

    def test_main_writes_keywords_and_skips_oov(self):
        self.app_config["muse"]["kws_wake_words"] = ["嗨缪斯", "hi muse", "缪斯ǎ"]
        with self.assertLogs("xiaozhi", level="WARNING"):
            status, lines = self.run_main(fake_text2token(oov="Ǎ"))
        self.assertEqual(status, 0)
        self.assertEqual(
            lines,
            [
                "你 好 小 智 @你好小智",
                "▁HI ▁OPEN ▁CLAW",
                "你 好 缪 斯 @你好缪斯",
                "嗨 缪 斯 @嗨缪斯",
                "▁HI ▁MUSE",
            ],
        )

    def test_main_fails_when_nothing_encodes(self):
        with self.assertLogs("xiaozhi", level="WARNING"):
            status, lines = self.run_main(fake_text2token(fail="你HIhi嗨哈"))
        self.assertEqual(status, 1)
        self.assertIsNone(lines)


# ---------------------------------------------------------------------------
# 路由：before_wakeup / 原生 ASR 外部唤醒
# ---------------------------------------------------------------------------


class FakeSpeaker:
    def __init__(self):
        self.events = []

    async def play(self, text=None, **kwargs):
        self.events.append(("play", text))
        return True

    async def abort_xiaoai(self):
        self.events.append(("abort",))
        return True


class BeforeWakeupWakeWordRoutingTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = load_repo_config()
        patcher = mock.patch.object(
            muse, "_app_config", side_effect=make_config_getter(self.config.APP_CONFIG)
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    async def route(self, text, source):
        speaker = FakeSpeaker()
        result = await self.config.before_wakeup(speaker, text, source, mock.AsyncMock())
        return result, speaker.events

    async def test_variants_route_to_hermes(self):
        with env(MUSE_ENABLE="1"):
            for text in ("Hey Muse，开灯", "嗨，缪思，几点了", "muse 我今晚想洗澡", "嗯，缪斯"):
                with self.subTest(text=text):
                    self.assertEqual(await self.route(text, "xiaoai"), ("hermes", [("abort",)]))
            for text in ("hi▁muse", "hey▁muse", "嗨缪斯", "哈喽缪斯"):
                with self.subTest(text=text):
                    self.assertEqual(await self.route(text, "kws"), ("hermes", []))

    async def test_non_muse_text_falls_through(self):
        with env(MUSE_ENABLE="1"):
            for text in ("museum 门票多少钱", "我喜欢缪斯", "今天妙思路清晰", "慕斯蛋糕怎么做"):
                with self.subTest(text=text):
                    self.assertEqual(await self.route(text, "xiaoai"), (None, []))


def import_xiaoai(app_config: dict):
    """参照 test_wakeup_keywords：桩掉 numpy / 原生扩展 / ConfigManager 后导入 core.xiaoai。"""

    class ConfigManagerStub:
        @classmethod
        def instance(cls):
            return cls()

        def get_app_config(self, path=None, default=None):
            return make_config_getter(app_config)(path, default)

    stubs = {
        "numpy": types.SimpleNamespace(int16=object(), float32=object()),
        "open_xiaoai_server": types.SimpleNamespace(),
        "core.utils.config": types.SimpleNamespace(ConfigManager=ConfigManagerStub),
    }
    isolated = ("core.xiaoai", "core.wakeup_session")
    saved = {name: sys.modules.get(name) for name in isolated}
    for name in isolated:
        sys.modules.pop(name, None)
    try:
        with mock.patch.dict(sys.modules, stubs):
            return importlib.import_module("core.xiaoai")
    finally:
        for name, module in saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


class XiaoAINativeMuseKeywordTest(unittest.TestCase):
    def setUp(self):
        self.module = import_xiaoai({"wakeup": {"keywords": ["你好小黑", "你好缪斯"]}, "xiaoai": {}, "muse": {}})
        self.xiaoai = self.module.XiaoAI

        class ConversationStub:
            def reset_retries(self):
                pass

            def apply_runtime_config(self, _config):
                pass

            async def handle_text_command(self, text, speaker):
                pass

        self.xiaoai.conversation = ConversationStub()

    def dispatch(self, text):
        calls = []

        class EventManagerStub:
            @staticmethod
            def consume_xiaoai_asr_result(**_kwargs):
                return False

            @staticmethod
            async def wakeup(text, source):
                calls.append((text, source))

        async def suppress_dialog(dialog_id, reason):
            calls.append(("suppress", dialog_id))

        line = {
            "header": {"namespace": "SpeechRecognizer", "name": "RecognizeResult", "dialog_id": "d1"},
            "payload": {"results": [{"text": text}], "is_final": True, "is_vad_begin": False},
        }
        event = json.dumps(
            {"event": "instruction", "data": {"NewLine": json.dumps(line, ensure_ascii=False)}},
            ensure_ascii=False,
        )
        with (
            mock.patch.object(self.module, "EventManager", EventManagerStub),
            mock.patch.object(self.module, "get_speaker", return_value=None),
            mock.patch.object(self.xiaoai, "_suppress_dialog", side_effect=suppress_dialog),
        ):
            asyncio.run(self.xiaoai.on_event(event))
        return calls

    def test_native_text_equal_to_muse_kws_keyword_is_external_wakeup(self):
        with env(MUSE_ENABLE="1"):
            self.xiaoai.refresh_runtime_config()
            for text in ("嗨缪斯", "Hey Muse", "hey, muse.", "ＨＩ　ＭＵＳＥ", "你好缪斯"):
                with self.subTest(text=text):
                    self.assertEqual(self.dispatch(text), [("suppress", "d1"), (text, "kws")])
            # 句子里还有请求内容 → 走普通 xiaoai 来源，由 before_wakeup 去掉唤醒词
            self.assertEqual(self.dispatch("Hey Muse，开灯"), [("Hey Muse，开灯", "xiaoai")])
            # 只走 ASR 的单独名字不是外部唤醒词
            self.assertEqual(self.dispatch("缪斯"), [("缪斯", "xiaoai")])

    def test_muse_disabled_keeps_previous_behaviour(self):
        with env(MUSE_ENABLE=""):
            self.xiaoai.refresh_runtime_config()
            self.assertEqual(self.dispatch("Hey Muse"), [("Hey Muse", "xiaoai")])
            self.assertEqual(self.dispatch("你好小黑"), [("suppress", "d1"), ("你好小黑", "kws")])


if __name__ == "__main__":
    unittest.main()
