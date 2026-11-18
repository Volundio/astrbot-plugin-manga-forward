"""`manga` 指令参数解析模块。"""

from __future__ import annotations

import re

from .models import CommandUsageError, ParsedMangaCommand, SourceId

USAGE_TEXT = (
    "📖 指令用法：manga <漫画名> <第几话>\n"
    "示例：\n"
    "  manga 电锯人 第1话\n"
    "  manga 葬送的芙莉莲 120\n"
    "  manga 孤独摇滚 第1卷\n"
    "  manga 药屋少女的呢喃 最新话\n"
    "（可选追加 -s 再漫画 / -s 拷贝 / -s komiic 临时指定源）"
)

# 剥离唤醒词与开头的 manga 指令词
_CMD_PREFIX_RE = re.compile(
    r"^[\s/!！.。~～]*manga(?:\s+|$)",
    re.IGNORECASE,
)

# 匹配 -s xxx 或 --source=xxx / --source xxx
_SOURCE_OPT_RE = re.compile(
    r"(?:^|\s)(?:-s|--source)(?:=|\s+)(\S+)",
    re.IGNORECASE,
)

# 支持无空格粘连形式兜底：如 "电锯人第12话" / "电锯人最新话"
_GLUED_CHAPTER_RE = re.compile(
    r"^(.+?)(第[0-9零〇一壹二贰两兩三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟.]+[话話章回卷]|最新[话話章]?)$"
)

# 支持显式源前缀 ID，如 zmh:57069 / copy:dianjuren / komiic:294
_EXPLICIT_ID_RE = re.compile(r"^([a-zA-Z]+):([a-zA-Z0-9_-]+)$")

_QUOTE_PAIRS: tuple[tuple[str, str], ...] = (
    ('"', '"'),
    ("'", "'"),
    ("“", "”"),
    ("‘", "’"),
    ("《", "》"),
    ("「", "」"),
    ("『", "』"),
)


def _strip_outer_quotes(text: str) -> str:
    s = text.strip()
    for left, right in _QUOTE_PAIRS:
        if len(s) >= 2 and s.startswith(left) and s.endswith(right):
            return s[len(left) : len(s) - len(right)].strip()
    return s


def extract_args_after_command(message_str: str) -> str:
    """从 AstrBot 事件的纯文本消息中剥离唤醒词与 `manga` 指令名，保留原始参数串。"""
    raw = (message_str or "").strip()
    if not raw:
        return ""
    m = _CMD_PREFIX_RE.match(raw)
    if m:
        return raw[m.end() :].strip()
    # 若上游已剥离唤醒词但存在 @提及 等前缀，定位首个独立 manga 词
    tokens = raw.split(maxsplit=1)
    if tokens and tokens[0].lstrip("/!！.。").lower() == "manga":
        return tokens[1].strip() if len(tokens) > 1 else ""
    return raw


def parse_manga_command(raw_args: str) -> ParsedMangaCommand:
    """解析 `manga` 指令后的参数串，输出漫画名、目标话数及可选源限定。"""
    text = (raw_args or "").strip()
    if not text:
        raise CommandUsageError(USAGE_TEXT)

    forced_source: SourceId | None = None

    def _consume_source(match: re.Match[str]) -> str:
        nonlocal forced_source
        raw_src = match.group(1).strip()
        resolved = SourceId.from_alias(raw_src)
        if resolved is None:
            raise CommandUsageError(
                f"未知的漫画源「{raw_src}」，可选值：再漫画 (zmh)、拷贝漫画 (copy)、komiic。"
            )
        forced_source = resolved
        return " "

    text = _SOURCE_OPT_RE.sub(_consume_source, text).strip()
    if not text:
        raise CommandUsageError(USAGE_TEXT)

    manga_part = ""
    chapter_part = ""

    # 1. 若首部使用成对引号/书名号显式包裹漫画名，如：《电锯人 第二部》 第120话
    for left, right in _QUOTE_PAIRS:
        if text.startswith(left):
            end_idx = text.find(right, len(left))
            if end_idx > len(left):
                rest = text[end_idx + len(right) :].strip()
                if rest:
                    manga_part = text[len(left) : end_idx].strip()
                    chapter_part = rest
                    break

    # 2. 常规空格分隔：从最右侧切分出最后一个 token 作为 <第几话>，左侧完整保留为漫画名
    if not manga_part:
        if " " in text or "\t" in text:
            parts = text.rsplit(maxsplit=1)
            if len(parts) == 2:
                manga_part = _strip_outer_quotes(parts[0])
                chapter_part = parts[1].strip()

    # 3. 容错兜底：无空格粘连输入，如 `/manga 电锯人第1话`
    if not manga_part:
        glued = _GLUED_CHAPTER_RE.match(text)
        if glued:
            manga_part = _strip_outer_quotes(glued.group(1))
            chapter_part = glued.group(2).strip()

    if not manga_part or not chapter_part:
        raise CommandUsageError(USAGE_TEXT)

    explicit_id: str | None = None
    id_match = _EXPLICIT_ID_RE.match(manga_part)
    if id_match:
        prefix_src = SourceId.from_alias(id_match.group(1))
        if prefix_src is not None:
            forced_source = prefix_src
            explicit_id = id_match.group(2)

    return ParsedMangaCommand(
        manga_query=manga_part,
        chapter_spec=chapter_part,
        forced_source=forced_source,
        explicit_manga_id=explicit_id,
    )
