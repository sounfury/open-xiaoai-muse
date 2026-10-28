"""小爱管家（"你好缪斯"）唤醒词。

  - KWS 命中缪斯唤醒词（muse.kws_wake_words，如"你好缪斯" / "hi muse"），
    或小爱原生 ASR 文本以任一唤醒词开头（muse.wake_words，含同音字和单独的"缪斯" / "muse"）
    → config.before_wakeup 返回 "hermes"，进入 Hermes 连续对话
  - MUSE_ENABLE=1 才生效：关闭时 match_muse_text / kws_wake_words 都返回空

本模块顶层只依赖标准库，不导入 open_xiaoai_server，
因此 config.py / keywords.py / 测试可以直接导入。
"""

import os
import re
import unicodedata
from dataclasses import dataclass, field

from core.ref import get_xiaoai
from core.utils.logger import logger

LOG_MODULE = "Muse"

_TRUTHY = ("1", "true", "yes")
# 只剩这些语气词时视为"只说了唤醒词"
_FILLER_CHARS = frozenset("啊呀吧呢嗯哦喔哈嘛诶欸")

NATIVE_SUPPRESS_MODES = ("abort", "dialog", "none")
MATCH_MODES = ("prefix", "contains")

# ---- 默认唤醒词（与 config.py 的 APP_CONFIG["muse"] 保持一致，旧版自定义 config 缺省时使用）----

# ASR 文本前缀匹配的完整集合。匹配时忽略大小写、全半角、空格和标点，所以
# "hi muse" 同时覆盖 "Hi Muse" / "himuse" / "ＨＩ　ＭＵＳＥ"，"你好缪斯" 覆盖 "你好，缪斯"。
DEFAULT_WAKE_WORDS = (
    # 正写 + 英文
    "你好缪斯", "嗨缪斯", "哈喽缪斯", "嘿缪斯", "hi缪斯", "hey缪斯", "hello缪斯",
    "hi muse", "hey muse", "hello muse", "你好muse", "嗨muse", "哈喽muse", "嘿muse",
    # 单独的名字：只在句首匹配，英文要求词边界（museum 不算）
    "缪斯", "muse",
    # 同音字（miù sī）：ASR 常见误识别，也可以单独使用
    "你好缪思", "嗨缪思", "哈喽缪思", "嘿缪思", "缪思",
    "你好缪丝", "嗨缪丝", "哈喽缪丝", "嘿缪丝", "缪丝",
    "你好谬斯", "嗨谬斯", "哈喽谬斯", "嘿谬斯", "谬斯",
    "你好谬思", "嗨谬思", "哈喽谬思", "嘿谬思", "谬思",
    # 近音字：本身是常见词（妙思泉涌 / 佳木斯 / 慕斯蛋糕），只接受带问候语的写法
    "你好妙思", "嗨妙思", "哈喽妙思", "嘿妙思",
    "你好妙斯", "嗨妙斯", "哈喽妙斯", "嘿妙斯",
    "你好木斯", "嗨木斯", "哈喽木斯", "嘿木斯",
    "你好慕斯", "嗨慕斯", "哈喽慕斯", "嘿慕斯",
)

# 合并进本地 KWS（常驻监听）的子集：只放"问候语 + 名字"，不放 2 音节的单独名字（误唤醒风险高），
# 也不放同音/近音字写法（KWS 按汉字建模，每多一种写法就多一条常驻误唤醒路径）。
DEFAULT_KWS_WAKE_WORDS = (
    "你好缪斯", "嗨缪斯", "哈喽缪斯", "嘿缪斯", "hi muse", "hey muse", "hello muse",
)

# 唤醒词前允许出现的语气词（只在句首去掉，可重复，如"嗯，缪斯"）。
# 不含"那个"：它也是指示代词（"那个缪斯是谁"），去掉会让单独的"缪斯"更容易误触发。
DEFAULT_LEADING_FILLERS = ("嗯", "呃", "额", "啊", "喂", "诶", "欸", "哎", "um", "uh", "hmm", "emm")

# 环境变量 MUSE_WAKE_WORDS / MUSE_KWS_WAKE_WORDS 的分隔符（英文唤醒词里有空格，不能按空格切）
_LIST_SEPARATORS = re.compile(r"[,，、;；\n]")


def is_muse_enabled() -> bool:
    """MUSE_ENABLE=1 时启用"你好缪斯"唤醒词（与其他后端一样只由环境变量控制）。"""
    return os.environ.get("MUSE_ENABLE", "").strip().lower() in _TRUTHY


# ---- 配置 ----


def _app_config(path: str, default=None):
    from core.utils.config import ConfigManager

    return ConfigManager.instance().get_app_config(path, default)


def _to_str(value, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def parse_word_list(value) -> list[str] | None:
    """把配置 / 环境变量里的唤醒词列表转成 list。

    接受 list / tuple，或用 , ， 、 ; 分隔的字符串；None 表示"未配置"。
    """
    if value is None:
        return None
    if isinstance(value, str):
        items = _LIST_SEPARATORS.split(value)
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = list(value)
    else:
        return None
    return [str(item).strip() for item in items if item is not None and str(item).strip()]


def _env_word_list(name: str) -> list[str] | None:
    """环境变量为空（或只有分隔符）时视为未设置。"""
    return parse_word_list(os.environ.get(name, "")) or None


def unique_wake_words(words) -> list[str]:
    """按归一化形式去重，保留第一次出现的写法。"""
    seen: set[str] = set()
    result: list[str] = []
    for word in words:
        if not isinstance(word, str):
            continue
        key = normalize_wake_text(word)
        if key and key not in seen:
            seen.add(key)
            result.append(word.strip())
    return result


@dataclass
class MuseSettings:
    """缪斯唤醒词配置；字段含义见 config.py 的 APP_CONFIG["muse"]。"""

    wake_word: str = "你好缪斯"
    # ASR 前缀匹配用的完整集合（MUSE_WAKE_WORDS > muse.wake_words > DEFAULT_WAKE_WORDS）
    wake_word_list: list[str] = field(default_factory=lambda: list(DEFAULT_WAKE_WORDS))
    wake_word_aliases: list[str] = field(default_factory=list)
    # 本地 KWS 子集（MUSE_KWS_WAKE_WORDS > muse.kws_wake_words > DEFAULT_KWS_WAKE_WORDS）
    kws_wake_word_list: list[str] = field(default_factory=lambda: list(DEFAULT_KWS_WAKE_WORDS))
    leading_fillers: list[str] = field(default_factory=lambda: list(DEFAULT_LEADING_FILLERS))
    match_mode: str = "prefix"
    native_suppress: str = "abort"

    @property
    def wake_words(self) -> list[str]:
        """ASR 文本匹配的完整集合：wake_word + wake_words + aliases + KWS 子集（去重）。

        KWS 子集也并进来，保证 KWS 命中的文本一定能被识别为缪斯唤醒。
        """
        return unique_wake_words(
            [self.wake_word, *self.wake_word_list, *self.wake_word_aliases, *self.kws_wake_word_list]
        )

    @property
    def kws_wake_words(self) -> list[str]:
        """要合并进 wakeup.keywords（本地 KWS）的唤醒词。"""
        return unique_wake_words(self.kws_wake_word_list)


def load_settings(config_getter=None) -> MuseSettings:
    """读取配置：环境变量 MUSE_WAKE_WORDS / MUSE_KWS_WAKE_WORDS 优先，其次 APP_CONFIG["muse"]。"""
    getter = config_getter or _app_config
    cfg = getter("muse", {}) or {}
    if not isinstance(cfg, dict):
        cfg = {}
    defaults = MuseSettings()

    aliases = parse_word_list(cfg.get("wake_word_aliases")) or []
    wake_word_list = _env_word_list("MUSE_WAKE_WORDS")
    if wake_word_list is None:
        wake_word_list = parse_word_list(cfg.get("wake_words"))
    if wake_word_list is None:
        wake_word_list = list(DEFAULT_WAKE_WORDS)
    kws_wake_word_list = _env_word_list("MUSE_KWS_WAKE_WORDS")
    if kws_wake_word_list is None:
        kws_wake_word_list = parse_word_list(cfg.get("kws_wake_words"))
    if kws_wake_word_list is None:
        kws_wake_word_list = list(DEFAULT_KWS_WAKE_WORDS)
    leading_fillers = parse_word_list(cfg.get("leading_fillers"))
    if leading_fillers is None:
        leading_fillers = list(DEFAULT_LEADING_FILLERS)

    match_mode = _to_str(cfg.get("match_mode"), defaults.match_mode).lower()
    if match_mode not in MATCH_MODES:
        match_mode = defaults.match_mode
    native_suppress = _to_str(cfg.get("native_suppress"), defaults.native_suppress).lower()
    if native_suppress not in NATIVE_SUPPRESS_MODES:
        native_suppress = defaults.native_suppress

    return MuseSettings(
        wake_word=_to_str(cfg.get("wake_word"), defaults.wake_word) or defaults.wake_word,
        wake_word_list=wake_word_list,
        wake_word_aliases=aliases,
        kws_wake_word_list=kws_wake_word_list,
        leading_fillers=leading_fillers,
        match_mode=match_mode,
        native_suppress=native_suppress,
    )


# ---- 唤醒词文本解析 ----


def _fold(ch: str) -> str:
    """单个字符归一化：NFKC（全角 → 半角）+ casefold。"""
    return unicodedata.normalize("NFKC", ch).casefold()


def _is_separator(ch: str) -> bool:
    """空白和中英文标点（，。！？、…）都视为分隔符。

    "▁" 是 sentencepiece 的词边界符：英文 KWS 关键词没有 @phrase 时，
    sherpa 返回的是拼起来的 token，例如 "hi▁muse"。
    """
    return ch.isspace() or unicodedata.category(ch)[0] in ("P", "Z") or ch in "~～▁"


def _is_latin_alnum(ch: str) -> bool:
    return ch.isascii() and ch.isalnum()


def _compact(text: str) -> tuple[str, list[int]]:
    """归一化后去掉分隔符，同时记录每个字符在原文中的下标。"""
    chars: list[str] = []
    index: list[int] = []
    for i, raw in enumerate(text):
        for ch in _fold(raw):
            if _is_separator(ch):
                continue
            chars.append(ch)
            index.append(i)
    return "".join(chars), index


def normalize_wake_text(text: str) -> str:
    """唤醒词 / 文本的比较形式："Hey, Muse！" → "heymuse"。"""
    if not isinstance(text, str):
        return ""
    return _compact(text)[0]


def _strip_leading_separators(text: str) -> str:
    i = 0
    while i < len(text) and _is_separator(text[i]):
        i += 1
    return text[i:].strip()


@dataclass(frozen=True)
class MuseMatch:
    wake_word: str  # 命中的唤醒词（配置里的写法，如 "hey muse" / "你好缪思"）
    content: str  # 去掉唤醒词和前导标点后的请求文本，只说了唤醒词时为空

    @property
    def is_bare(self) -> bool:
        return not self.content


class _WakeWordMatcher:
    """在 compact 文本的某个位置尝试匹配唤醒词 / 语气词。"""

    def __init__(self, text: str, words, fillers):
        self.text = text
        self.compact, self.index = _compact(text)
        # 最长优先："你好缪斯" 先于 "缪斯"，"hey muse" 先于 "muse"
        keyed = [(normalize_wake_text(w), w) for w in words if isinstance(w, str)]
        self.words = sorted(((key, w) for key, w in keyed if key), key=lambda item: -len(item[0]))
        self.fillers = sorted(
            (key for key in (normalize_wake_text(f) for f in fillers or ()) if key),
            key=len,
            reverse=True,
        )

    def _end(self, pos: int, length: int) -> int:
        """compact[pos:pos+length] 在原文中的结束下标（不含）。"""
        return self.index[pos + length - 1] + 1

    def _bounded(self, pos: int, key: str) -> bool:
        """英文词边界：以拉丁字母开头/结尾时，前/后一个原文字符不能是拉丁字母或数字。

        "muse, 开灯" / "muse开灯" 可以，"museum" / "mused" / "hi museum" 不行。
        中文没有词边界，"缪斯开灯" 这类不带停顿的说法也要能匹配。
        """
        if key[-1].isascii() and key[-1].isalpha():
            end = self._end(pos, len(key))
            if end < len(self.text) and any(_is_latin_alnum(ch) for ch in _fold(self.text[end])[:1]):
                return False
        if pos > 0 and key[0].isascii() and key[0].isalpha():
            start = self.index[pos]
            if start > 0 and any(_is_latin_alnum(ch) for ch in _fold(self.text[start - 1])[-1:]):
                return False
        return True

    def word_at(self, pos: int) -> tuple[str, str] | None:
        for key, word in self.words:
            if self.compact.startswith(key, pos) and self._bounded(pos, key):
                return key, word
        return None

    def filler_at(self, pos: int) -> str | None:
        for key in self.fillers:
            if self.compact.startswith(key, pos) and self._bounded(pos, key):
                return key
        return None


def parse_muse_text(
    text: str,
    wake_words: list[str],
    match_mode: str = "prefix",
    leading_fillers=DEFAULT_LEADING_FILLERS,
) -> MuseMatch | None:
    """判断文本是否以唤醒词开头（prefix，默认）或包含唤醒词（contains，需显式开启）。

    - 比较前做 NFKC（全角 → 半角）+ casefold，并忽略空格和中英文标点：
        "你好缪斯，我今晚想洗澡" / "你好 缪斯 我今晚想洗澡" / "Hey Muse，开灯" 都能匹配
    - prefix：唤醒词必须在句首；句首的标点、空白和 leading_fillers（嗯 / 喂 / um …）可以跳过
    - 最长优先；英文唤醒词要求词边界（"museum" 不匹配 "muse"）
    - content 是原文去掉唤醒词和其后标点的部分：
        "你好缪斯。" / "你好缪斯啊" → content=""（只说了唤醒词）
    """
    if not isinstance(text, str) or not text.strip():
        return None

    matcher = _WakeWordMatcher(text, wake_words, leading_fillers)
    compact = matcher.compact
    found: tuple[int, str, str] | None = None

    if match_mode == "contains":
        for pos in range(len(compact)):
            hit = matcher.word_at(pos)
            if hit:
                found = (pos, *hit)
                break
    else:
        pos = 0
        while pos < len(compact):
            hit = matcher.word_at(pos)
            if hit:
                found = (pos, *hit)
                break
            filler = matcher.filler_at(pos)
            if not filler:
                break
            pos += len(filler)

    if found is None:
        return None

    pos, key, word = found
    end = matcher._end(pos, len(key))
    content = _strip_leading_separators(text[end:])
    if all(ch in _FILLER_CHARS or _is_separator(ch) for ch in content):
        content = ""
    return MuseMatch(wake_word=word, content=content)


def strip_wake_word_tail(text: str, wake_words) -> str:
    """去掉句首唤醒词的后半截："好缪斯，开灯" → "开灯"；"斯，开灯" → "开灯"。

    一个字的尾音只有后面紧跟标点 / 空格时才去掉，免得把"思考一下"的"思"当成尾音。
    """
    def is_sep(ch: str) -> bool:
        return ch.isspace() or unicodedata.category(ch)[0] in ("P", "Z")

    stripped = text.lstrip()
    suffixes: set[str] = set()
    for word in wake_words or ():
        if not isinstance(word, str):
            continue
        compact = "".join(ch for ch in word if not is_sep(ch))
        suffixes.update(compact[i:] for i in range(1, len(compact)))
    for suffix in sorted(suffixes, key=len, reverse=True):
        if not stripped.lower().startswith(suffix.lower()):
            continue
        rest = stripped[len(suffix):]
        if len(suffix) == 1 and not (rest[:1] and is_sep(rest[0])):
            continue
        i = 0
        while i < len(rest) and is_sep(rest[i]):
            i += 1
        return rest[i:]
    return text


def match_settings_text(text: str, settings: MuseSettings) -> MuseMatch | None:
    """按配置匹配（不检查 MUSE_ENABLE）。"""
    return parse_muse_text(text, settings.wake_words, settings.match_mode, settings.leading_fillers)


def match_muse_text(text: str, settings: MuseSettings | None = None) -> MuseMatch | None:
    """MUSE_ENABLE 关闭时总是返回 None，保证其他路由行为不变。"""
    if not is_muse_enabled():
        return None
    settings = settings or load_settings()
    return match_settings_text(text, settings)


def kws_wake_words(config_getter=None) -> list[str]:
    """MUSE_ENABLE=1 时要合并进本地 KWS（wakeup.keywords）的唤醒词，未启用时为空。"""
    if not is_muse_enabled():
        return []
    return load_settings(config_getter).kws_wake_words


async def suppress_native_xiaoai(speaker, settings: MuseSettings | None = None) -> None:
    """拦截原生小爱对当前这句话的处理（source == "xiaoai" 时在 before_wakeup 中调用）。

    muse.native_suppress:
        "abort"  — speaker.abort_xiaoai()，与 召唤龙虾 示例一致（重启约 1-2 秒）
        "dialog" — 复用 XiaoAI 的 dialog 拦截机制，持续停止该 dialog 的原生回复
        "none"   — 不拦截
    """
    settings = settings or load_settings()
    mode = settings.native_suppress
    if mode == "none" or speaker is None:
        return
    try:
        if mode == "dialog":
            xiaoai = get_xiaoai()
            dialog_id = getattr(xiaoai, "last_recognize_dialog_id", "") if xiaoai else ""
            if dialog_id:
                await xiaoai._suppress_dialog(dialog_id, "小爱管家接管")
                return
            logger.debug("No native dialog_id recorded, fallback to abort_xiaoai", module=LOG_MODULE)
        await speaker.abort_xiaoai()
    except Exception as exc:
        logger.warning(f"拦截原生小爱失败: {type(exc).__name__}: {exc}", module=LOG_MODULE)
