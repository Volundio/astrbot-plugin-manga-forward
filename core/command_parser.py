"""`manga` 指令参数解析模块。"""

from __future__ import annotations

import re

from .models import CommandUsageError, ParsedMangaCommand, SourceId

USAGE_TEXT = (
    "📖 指令用法：manga <漫画名> <第几话>（支持单话与批量话数）\n"
    "示例：\n"
    "  manga 电锯人 第1话\n"
    "  manga 电锯人 1-5话          （连续多话批量发送）\n"
    "  manga 电锯人 1,3,5话        （指定多话批量发送）\n"
    "  manga 黑暗集会 最新3话      （最近 N 话批量发送）\n"
    "  manga 异剑战记 第二部 1-3话\n"
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

_NUM_CHARS = r"0-9零〇一壹二贰两兩三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟."

# 识别独立的篇章前缀 token（如 "第二部"、"第二章"、"第2季"）
_PART_TOKEN_RE = re.compile(
    rf"^第\s*[{_NUM_CHARS}]+\s*[部季篇章]$",
    re.IGNORECASE,
)

# 识别独立的子篇章后缀 token（如 "前篇"、"中篇"、"后篇"、"上"、"下"、"①"）
_SUBPART_TOKEN_RE = re.compile(
    r"^[（(【\[]?(?:前篇|中篇|后篇|後篇|上篇?|中篇?|下篇?|[①②③④⑤⑥])[）)】\]]?$"
)

# 识别区间连接词 token（支持用户在区间连接符两侧留空格，如 `1 - 5话`、`第1话 到 第5话`）
_RANGE_CONN_TOKEN_RE = re.compile(r"^(?:[-~～—–]|(?:\.\.)|至|到)$")

# 识别话数主 token（如 "13"、"13话"、"1-5话"、"1,3,5话"、"最新3话"、"最新话"）
_CHAPTER_LIKE_TOKEN_RE = re.compile(
    rf"^(?:(?:第\s*)?[{_NUM_CHARS}]+\s*(?:话|話|回|章|集|卷|册|冊)?.*|ch\.?\d+.*|ep\.?\d+.*|vol\.?\d+.*|(?:最新|最近|最后|最後)[{_NUM_CHARS}]*[话話章回集]?|latest\d*|序[章话話]?)$",
    re.IGNORECASE,
)

# 支持无空格粘连形式兜底：如 "电锯人第12话" / "电锯人第1-5话" / "电锯人最新3话" / "电锯人最新话"
_GLUED_CHAPTER_RE = re.compile(
    rf"^(.+?)((?:第[{_NUM_CHARS}]+[部季篇章]\s*)?(?:第[{_NUM_CHARS}]+[话話章回卷\-~～至到,，、].*|(?:最新|最近|最后|最後)[{_NUM_CHARS}]*[话話章回集]?))$"
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
    tokens = raw.split(maxsplit=1)
    if tokens and tokens[0].lstrip("/!！.。").lower() == "manga":
        return tokens[1].strip() if len(tokens) > 1 else ""
    return raw


def _split_unquoted_manga_and_chapter(text: str) -> tuple[str, str]:
    """智能切分未加引号的 `<漫画名> <第几话>`，兼容复合话数（如 `第二章 13话`、`第04话 前篇`）。"""
    tokens = text.split()
    if len(tokens) < 2:
        return "", ""

    # 情况 A：末尾为子篇章后缀，如 `异剑战记 第二章 12话 前篇` 或 `异剑战记 第04话 前篇`
    if (
        len(tokens) >= 3
        and _SUBPART_TOKEN_RE.match(tokens[-1])
        and _CHAPTER_LIKE_TOKEN_RE.match(tokens[-2])
    ):
        if len(tokens) >= 4 and _PART_TOKEN_RE.match(tokens[-3]):
            return _strip_outer_quotes(" ".join(tokens[:-3])), " ".join(tokens[-3:])
        return _strip_outer_quotes(" ".join(tokens[:-2])), " ".join(tokens[-2:])

    # 情况 B：末尾为带空格的区间连接符，如 `电锯人 1 - 5话`、`异剑战记 第二部 1 到 3话`
    if (
        len(tokens) >= 4
        and _RANGE_CONN_TOKEN_RE.match(tokens[-2])
        and _CHAPTER_LIKE_TOKEN_RE.match(tokens[-3])
        and _CHAPTER_LIKE_TOKEN_RE.match(tokens[-1])
    ):
        merged_range = f"{tokens[-3]}-{tokens[-1]}"
        if len(tokens) >= 5 and _PART_TOKEN_RE.match(tokens[-4]):
            return _strip_outer_quotes(" ".join(tokens[:-4])), f"{tokens[-4]} {merged_range}"
        return _strip_outer_quotes(" ".join(tokens[:-3])), merged_range

    # 情况 C：末尾两个 token 构成 `<第X部/章/季/篇> <第Y话/区间>`，如 `异剑战记 第二章 13话` / `异剑战记 第二部 1-3话`
    if (
        len(tokens) >= 3
        and _PART_TOKEN_RE.match(tokens[-2])
        and _CHAPTER_LIKE_TOKEN_RE.match(tokens[-1])
    ):
        return _strip_outer_quotes(" ".join(tokens[:-2])), f"{tokens[-2]} {tokens[-1]}"

    # 情况 D：常规右侧单 token 切分（左侧完整保留可能含空格的漫画名）
    parts = text.rsplit(maxsplit=1)
    return _strip_outer_quotes(parts[0]), parts[1].strip()


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

    # 2. 空格分隔切分（支持复合部/章/前篇后缀）
    if not manga_part and (" " in text or "\t" in text):
        manga_part, chapter_part = _split_unquoted_manga_and_chapter(text)

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
