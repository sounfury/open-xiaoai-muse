"""唤醒词生成工具：将配置中的常规与 Muse 唤醒词编码到 keywords.txt。"""

import re
import unicodedata
from pathlib import Path


def init_project_context():
    """动态导入父模块"""
    import os
    import sys

    project_root = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "../../../..")
    )
    if project_root not in sys.path:
        sys.path.insert(0, project_root)


init_project_context()

from core.utils.config import ConfigManager
from core.utils.file import get_model_file_path
from core.utils.logger import logger


def should_generate_keywords():
    """Return whether keyword generation should run."""
    import os

    def enabled(*names: str) -> bool:
        value = next((os.environ[name] for name in names if os.environ.get(name)), "")
        return value.lower() in ("1", "true", "yes")

    # 小智、外部对话后端（core/backends.py）、小爱管家任一启用就需要唤醒词文件
    # OPENCLAW_ENABLE (新) 和 OPENCLAW_ENABLED (旧) 都认
    if not any(
        (
            enabled("XIAOZHI_ENABLE"),
            enabled("HERMES_ENABLE"),
            enabled("OPENCLAW_ENABLE", "OPENCLAW_ENABLED"),
            enabled("OPENAI_ENABLE"),
            enabled("QWENPAW_ENABLE"),
            enabled("MUSE_ENABLE"),
        )
    ):
        return False, (
            "XIAOZHI_ENABLE, HERMES_ENABLE, OPENCLAW_ENABLE/OPENCLAW_ENABLED, OPENAI_ENABLE, "
            "QWENPAW_ENABLE and MUSE_ENABLE are all disabled"
        )

    return True, ""


def _keyword_text(keyword: str) -> str:
    """KWS 关键词文本：NFKC（全角 → 半角）、英文转大写（BPE 词表是大写）、合并多余空格。"""
    return " ".join(unicodedata.normalize("NFKC", keyword).upper().split())


def get_keyword_texts(config_getter) -> list[str]:
    """wakeup.keywords + 小爱管家 KWS 子集（MUSE_ENABLE=1 时，见 core/muse.py），去重保序。"""
    from core.muse import kws_wake_words

    keywords = list(config_getter("wakeup.keywords", []) or [])
    keywords += kws_wake_words(config_getter)
    texts: list[str] = []
    for keyword in keywords:
        if not isinstance(keyword, str):
            continue
        text = _keyword_text(keyword)
        if text and text not in texts:
            texts.append(text)
    return texts


def get_args():
    config = ConfigManager.instance()
    tokens_type = "cjkchar+bpe"
    tokens = get_model_file_path("tokens.txt")
    bpe_model = get_model_file_path("bpe.model")
    output = get_model_file_path("keywords.txt")
    texts = get_keyword_texts(config.get_app_config)
    return locals()


def encode_keywords(texts, text2token, **kwargs) -> list[list[str]]:
    """逐个编码关键词；编码失败（含 OOV 字符）的跳过并警告，不影响其他关键词。

    sherpa_onnx.text2token 遇到 OOV 只会 print 并静默丢掉该条，
    返回列表会变短，所以这里一条一条编码，才能知道是哪一条被跳过。
    """
    encoded: list[list[str]] = []
    for text in texts:
        try:
            result = text2token([text], **kwargs)
        except Exception as exc:
            logger.warning(
                f"关键词 {text!r} 编码失败，已跳过: {type(exc).__name__}: {exc}",
                module="KWS",
            )
            continue
        tokens = result[0] if result else None
        if not tokens:
            logger.warning(
                f"关键词 {text!r} 含有模型词表（tokens.txt）里没有的字符，已跳过",
                module="KWS",
            )
            continue
        encoded.append(list(tokens))
    return encoded


def keyword_overrides(config_getter, key: str) -> dict[str, float]:
    """kws.keyword_thresholds / kws.keyword_scores → {关键词文本: 数值}。

    键按 keywords.txt 里的写法归一化（"Hi Muse" 和 "hi muse" 是同一个），非法值跳过并警告。
    """
    raw = config_getter(f"kws.{key}", {}) or {}
    if not isinstance(raw, dict):
        logger.warning(f"kws.{key} 应该是 dict，已忽略", module="KWS")
        return {}
    result: dict[str, float] = {}
    for keyword, value in raw.items():
        if not isinstance(keyword, str):
            continue
        try:
            result[_keyword_text(keyword)] = float(value)
        except (TypeError, ValueError):
            logger.warning(f"kws.{key} 里 {keyword!r} 的值 {value!r} 不是数字，已忽略", module="KWS")
    return result


def format_keyword_line(
    tokens: list[str], threshold: float | None = None, score: float | None = None
) -> str:
    """keywords.txt 的一行。

    - 纯英文关键词不带 @phrase（KWS 返回拼接的 token，如 "hi▁muse"），其余带 @原文
    - score（:x）覆盖这个词的 keywords_score，threshold（#x）覆盖它的 keywords_threshold
    """
    line = "".join(tokens)
    parts = [" ".join(tokens)]
    if score is not None:
        parts.append(f":{float(score)}")
    if threshold is not None:
        parts.append(f"#{float(threshold)}")
    if not re.match(r"^[▁A-Z\s]+$", line):
        parts.append(f"@{line}")
    return " ".join(parts)


def build_keyword_lines(texts, encoded_by_text, thresholds=None, scores=None) -> list[str]:
    """按关键词文本拼出 keywords.txt 的行（编码失败的词不在 encoded_by_text 里，跳过）。"""
    thresholds = thresholds or {}
    scores = scores or {}
    for keyword in sorted((set(thresholds) | set(scores)) - set(texts)):
        logger.warning(f"kws.keyword_thresholds / keyword_scores 里的 {keyword!r} 不是当前唤醒词，已忽略", module="KWS")
    return [
        format_keyword_line(encoded_by_text[text], thresholds.get(text), scores.get(text))
        for text in texts
        if text in encoded_by_text
    ]


def generate_keywords_file(output: str, config_getter=None, texts=None) -> int:
    """编码唤醒词并写入 output；返回写入的行数（0 = 一个都没编码成功）。"""
    from core.utils.onnx_runtime import prepare_onnxruntime

    prepare_onnxruntime()
    from sherpa_onnx import text2token

    getter = config_getter or ConfigManager.instance().get_app_config
    texts = [_keyword_text(t) for t in texts] if texts is not None else get_keyword_texts(getter)
    kwargs = {
        "tokens": get_model_file_path("tokens.txt"),
        "tokens_type": "cjkchar+bpe",
        "bpe_model": get_model_file_path("bpe.model"),
    }
    encoded_by_text: dict[str, list[str]] = {}
    for text in texts:
        encoded = encode_keywords([text], text2token, **kwargs)
        if encoded:
            encoded_by_text[text] = encoded[0]
    lines = build_keyword_lines(
        texts,
        encoded_by_text,
        keyword_overrides(getter, "keyword_thresholds"),
        keyword_overrides(getter, "keyword_scores"),
    )
    if not lines:
        return 0
    with open(output, "w", encoding="utf8") as f:
        for line in lines:
            f.write(line + "\n")
    return len(lines)


def main():
    should_run, reason = should_generate_keywords()
    if not should_run:
        logger.debug(f"Keyword generation skipped: {reason}", module="KWS")
        return 0

    required_files = [
        get_model_file_path("tokens.txt"),
        get_model_file_path("bpe.model"),
    ]
    missing_files = [path for path in required_files if not Path(path).is_file()]
    if missing_files:
        logger.debug(
            "Keyword generation failed: missing model files: "
            f"{', '.join(missing_files)}",
            module="KWS",
        )
        return 1

    args = get_args()
    written = generate_keywords_file(args["output"], ConfigManager.instance().get_app_config, args["texts"])
    if not written:
        logger.error("Keyword generation failed: no keyword could be encoded", module="KWS")
        return 1
    logger.debug(
        f"Keyword file generated: {args['output']} ({written}/{len(args['texts'])} keywords)",
        module="KWS",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
