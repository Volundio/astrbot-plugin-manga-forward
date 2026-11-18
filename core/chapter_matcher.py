"""跨源章节分类与目标话数精准匹配模块。"""

from __future__ import annotations

import math
import re

from .models import ChapterInfo, ChapterKind, ChapterNotFoundError, ChapterTarget, TargetMode
from .text_utils import (
    extract_number_from_text,
    format_chapter_num,
    normalize_for_match,
    parse_chinese_integer,
    to_simplified,
)

_LATEST_KEYWORDS = {"最新", "最新话", "最新章", "最新回", "最后一话", "最终话_latest", "latest"}
_VOLUME_GROUP_KEYWORDS = ("单行本", "卷", "volume", "book", "tankobon")
_SPECIAL_GROUP_KEYWORDS = ("番外", "特别", "外传", "其他", "附录", "同人", "短篇")

# 优先提取显式“第X话/回/章/集”或“X话”中的数字，避免副标题里的数字干扰（如 "第1话 100万円的梦想"）
_EXPLICIT_CHAP_NUM_RE = re.compile(
    r"(?:第\s*)?(\d+(?:\.\d+)?|[零〇一壹二贰两兩三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+)\s*(?:话|話|回|章|集|p\b)",
    re.IGNORECASE,
)
_EXPLICIT_VOL_NUM_RE = re.compile(
    r"(?:第\s*|vol\.?\s*|卷\s*)(\d+(?:\.\d+)?|[零〇一壹二贰两兩三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+)\s*(?:卷|册|冊|单行本)?",
    re.IGNORECASE,
)
_CH_PREFIX_RE = re.compile(r"^(?:ch\.?|ep\.?|c)\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_PURE_NUM_RE = re.compile(r"^#?\s*(\d+(?:\.\d+)?)\s*$")
_LEADING_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:\s+|[：:.\-_]|$)")

_SPECIAL_KEYWORDS = (
    "番外",
    "特别篇",
    "外传",
    "序章",
    "序话",
    "前篇",
    "后篇",
    "附录",
    "休刊",
    "通知",
    "预告",
)


def _parse_num_token(token: str) -> float | None:
    tok = token.strip()
    if not tok:
        return None
    try:
        return float(tok)
    except ValueError:
        cn = parse_chinese_integer(tok)
        return float(cn) if cn is not None else None


def classify_chapter_meta(
    title: str,
    group_name: str = "",
    hint_kind: ChapterKind | None = None,
) -> tuple[ChapterKind, float | None]:
    """根据源站章节标题与所属分组名，推断标准 `ChapterKind` 与章节序号。"""
    simp_title = to_simplified(title).strip()
    simp_group = to_simplified(group_name).strip().lower()

    # 1. 优先识别显式 "第X话 / X话"（即使标题后半段带“之卷”也归为单话）
    m_chap = _EXPLICIT_CHAP_NUM_RE.search(simp_title)
    if m_chap:
        num = _parse_num_token(m_chap.group(1))
        if num is not None:
            # 若分组明确是番外且标题含番外，标记为 SPECIAL，否则为 CHAPTER
            if "番外" in simp_title:
                return ChapterKind.SPECIAL, num
            return ChapterKind.CHAPTER, num

    # 2. 识别单行本/卷（分组名明确为单行本/book，或标题含 "第X卷" / "Vol.X" / "卷X"）
    is_vol_title = bool(
        re.search(
            r"(?:第\s*[\d零〇一二两三四五六七八九十百千.]+\s*[卷册]|^卷\s*\d|^vol\.?\s*\d|单行本)",
            simp_title,
            re.I,
        )
    )
    is_vol_group = hint_kind == ChapterKind.VOLUME or any(
        kw in simp_group for kw in _VOLUME_GROUP_KEYWORDS
    )
    if is_vol_title or is_vol_group:
        m_vol = _EXPLICIT_VOL_NUM_RE.search(simp_title)
        num = _parse_num_token(m_vol.group(1)) if m_vol else extract_number_from_text(simp_title)
        return ChapterKind.VOLUME, num

    # 3. 识别 Ch.1 / Ep.1 / 纯数字标题 "001" / 前导数字 "01 标题"
    for pattern in (_CH_PREFIX_RE, _PURE_NUM_RE, _LEADING_NUM_RE):
        m_num = pattern.match(simp_title)
        if m_num:
            num = _parse_num_token(m_num.group(1))
            if num is not None:
                kind = (
                    ChapterKind.SPECIAL
                    if any(kw in simp_group for kw in _SPECIAL_GROUP_KEYWORDS)
                    else (hint_kind or ChapterKind.CHAPTER)
                )
                return kind, num

    # 4. 识别番外/序章/特别篇等
    if any(kw in simp_title for kw in _SPECIAL_KEYWORDS) or any(
        kw in simp_group for kw in _SPECIAL_GROUP_KEYWORDS
    ):
        if simp_title in ("序章", "序话", "序", "第0话", "0话"):
            return ChapterKind.CHAPTER, 0.0
        return ChapterKind.SPECIAL, extract_number_from_text(simp_title)

    # 5. 兜底提取任意数字
    fallback_num = extract_number_from_text(simp_title)
    return (hint_kind or ChapterKind.CHAPTER), fallback_num


def parse_chapter_target(spec: str) -> ChapterTarget:
    """将用户输入的 `<第几话>` 字符串解析为结构化 `ChapterTarget`。"""
    raw = (spec or "").strip()
    simp = to_simplified(raw).strip()
    lower = simp.lower()

    if lower in _LATEST_KEYWORDS:
        return ChapterTarget(
            mode=TargetMode.LATEST,
            number=None,
            raw_text=raw,
            normalized_text="最新话",
        )

    if simp in ("序章", "序话", "序"):
        return ChapterTarget(
            mode=TargetMode.CHAPTER,
            number=0.0,
            raw_text=raw,
            normalized_text=simp,
        )

    # 判断用户是否明确指定了“卷”
    if any(tok in lower for tok in ("卷", "册", "单行本", "vol")):
        num = extract_number_from_text(simp)
        if num is not None:
            return ChapterTarget(
                mode=TargetMode.VOLUME,
                number=num,
                raw_text=raw,
                normalized_text=f"第{format_chapter_num(num)}卷",
            )

    # 判断用户是否指定了番外/特别篇等特殊章节
    if any(kw in simp for kw in ("番外", "特别", "外传", "前篇", "后篇", "附录")):
        num = extract_number_from_text(simp)
        return ChapterTarget(
            mode=TargetMode.SPECIAL,
            number=num,
            raw_text=raw,
            normalized_text=normalize_for_match(simp),
        )

    # 默认按单话序号解析（支持 "1", "01", "第1话", "12.5", "第一百话", "ch1"）
    m_chap = _EXPLICIT_CHAP_NUM_RE.search(simp)
    num = _parse_num_token(m_chap.group(1)) if m_chap else extract_number_from_text(simp)
    if num is not None:
        return ChapterTarget(
            mode=TargetMode.CHAPTER,
            number=num,
            raw_text=raw,
            normalized_text=f"第{format_chapter_num(num)}话",
        )

    # 纯文本标题匹配兜底（如特殊短篇名）
    return ChapterTarget(
        mode=TargetMode.SPECIAL,
        number=None,
        raw_text=raw,
        normalized_text=normalize_for_match(simp),
    )


def _num_equals(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return False
    return math.isclose(a, b, abs_tol=1e-4)


def _describe_latest(chapters: list[ChapterInfo]) -> str:
    if not chapters:
        return ""
    regular = [c for c in chapters if c.kind == ChapterKind.CHAPTER]
    pool = regular if regular else chapters
    best = max(
        pool,
        key=lambda c: (c.number if c.number is not None else -1.0, c.updated_at, c.order_index),
    )
    return best.title


def select_matching_chapter(
    chapters: list[ChapterInfo],
    target: ChapterTarget,
    manga_title: str = "",
) -> ChapterInfo:
    """在漫画章节列表中精准匹配目标章节；未命中时抛出带最新进度提示的 `ChapterNotFoundError`。"""
    if not chapters:
        raise ChapterNotFoundError(f"《{manga_title}》暂无可用章节列表")

    # 1. 最新话模式：优先连载单话（排除整卷），按话数/更新时间/顺序取最大者
    if target.mode == TargetMode.LATEST:
        regular = [c for c in chapters if c.kind == ChapterKind.CHAPTER]
        pool = regular if regular else chapters
        return max(
            pool,
            key=lambda c: (
                c.updated_at if c.updated_at > 0 else 0,
                c.number if c.number is not None else -1.0,
                c.order_index,
            ),
        )

    # 2. 单行本整卷模式
    if target.mode == TargetMode.VOLUME and target.number is not None:
        vol_matches = [
            c
            for c in chapters
            if c.kind == ChapterKind.VOLUME and _num_equals(c.number, target.number)
        ]
        if vol_matches:
            return vol_matches[0]
        # 若源站未标注卷分组，检查标题含“卷”且数字一致的条目
        title_vol_matches = [
            c
            for c in chapters
            if "卷" in to_simplified(c.title) and _num_equals(c.number, target.number)
        ]
        if title_vol_matches:
            return title_vol_matches[0]

    # 3. 常规单话模式（严格优先连载话，绝不把“第1卷”误当成“第1话”）
    if target.mode == TargetMode.CHAPTER and target.number is not None:
        chap_matches = [
            c
            for c in chapters
            if c.kind == ChapterKind.CHAPTER and _num_equals(c.number, target.number)
        ]
        if chap_matches:
            # 若存在多个汉化组分组同话数，优先默认/连载分组或页数完整者
            chap_matches.sort(
                key=lambda c: (
                    0 if c.group_name in ("", "连载", "默认", "默認", "default", "chapter") else 1,
                    -c.page_count,
                )
            )
            return chap_matches[0]

        # 序章 (0话) 兼容标题为 "序章"/"序" 的 SPECIAL 章节
        if _num_equals(target.number, 0.0):
            for c in chapters:
                if to_simplified(c.title).strip() in ("序章", "序话", "序", "预告"):
                    return c

        # 兼容部分短篇/单卷漫画仅有一个章节且源站归入其他非卷分组的情况
        non_vol_matches = [
            c
            for c in chapters
            if c.kind != ChapterKind.VOLUME and _num_equals(c.number, target.number)
        ]
        if non_vol_matches:
            return non_vol_matches[0]

    # 4. 特殊篇章 / 标题文本匹配（如 "番外1", "特别篇", "前篇"）
    raw_norm = normalize_for_match(target.raw_text)
    if raw_norm:
        for c in chapters:
            if normalize_for_match(c.title) == raw_norm:
                return c
        if target.mode == TargetMode.SPECIAL:
            for c in chapters:
                c_norm = normalize_for_match(c.title)
                if raw_norm in c_norm:
                    if target.number is None or _num_equals(c.number, target.number):
                        return c

    latest_hint = _describe_latest(chapters)
    suffix = f"（当前最新：{latest_hint}）" if latest_hint else ""
    raise ChapterNotFoundError(
        f"未找到《{manga_title}》的「{target.raw_text}」{suffix}",
        latest_hint=latest_hint,
    )
