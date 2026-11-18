"""跨源章节分类与目标话数精准匹配模块。

核心解决真实漫画源复杂章节命名歧义：
1. 严格区分篇章/部前缀与真实话数：如 `第二章 13话`、`第二部第2话` 中，`第二章/第二部` 解析为 `part_number=2`，
   `13话/第2话` 解析为 `number=13.0 / 2.0`，绝不因从左到右把 `第二章` 误判为第 2 话。
2. 优先匹配无篇章前缀的第一部正篇：用户请求 `第二话` 时优先命中 `第02话`，不会误发 `第二部第2话` 或 `第二章 13话`；
   用户显式请求 `第二部 13话` / `第二章 13话` 时精准命中对应篇章。
3. 前置隔离杂项非正篇：如 `短篇小剧场2`、`03卷情报&发表`、`45-54话的附加彩色插画` 归为 `SPECIAL`，不污染正篇话数。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from .models import (
    ChapterInfo,
    ChapterKind,
    ChapterNotFoundError,
    ChapterTarget,
    CommandUsageError,
    TargetMode,
)
from .text_utils import (
    extract_number_from_text,
    format_chapter_num,
    normalize_for_match,
    parse_chinese_integer,
    to_simplified,
)

_NUM_PAT = r"(?:\d+(?:\.\d+)?|[零〇一壹二贰两兩三叁四肆五伍六陆七柒八捌九玖十拾百佰千仟]+)"

_LATEST_KEYWORDS = {"最新", "最新话", "最新章", "最新回", "最后一话", "latest"}
_VOLUME_GROUP_KEYWORDS = ("单行本", "卷", "volume", "book", "tankobon")
_SPECIAL_GROUP_KEYWORDS = (
    "番外",
    "特别",
    "外传",
    "其他",
    "其它",
    "附录",
    "同人",
    "短篇",
    "画集",
    "特典",
)

# 非正篇杂项词：一旦标题命中这些词且不是标准“第X话”正篇，一律归入 SPECIAL 防止数字误匹配
_EXTRA_OMAKE_KEYWORDS = (
    "番外",
    "特别篇",
    "外传",
    "小剧场",
    "短篇",
    "四格",
    "附录",
    "附赠",
    "附加",
    "特典",
    "画集",
    "公式书",
    "粉丝书",
    "插画",
    "彩页",
    "尾页",
    "贺图",
    "生日",
    "纪念",
    "祝贺",
    "圣诞",
    "新年",
    "情人节",
    "万圣节",
    "合集",
    "情报",
    "发表",
    "休刊",
    "通知",
    "公告",
    "预告",
    "宣传",
    "壁纸",
    "后记",
    "补完",
    "设定集",
    "访谈",
    "角色介绍",
    "人气投票",
)

# 识别区间合集标题，如 "45-54话的附加彩色插画" / "1~10话合集"
_RANGE_CHAPTER_RE = re.compile(
    r"\d+\s*[-~～—至到]\s*\d+\s*(?:话|話|回|章|集|卷)",
    re.IGNORECASE,
)

# 首部篇章/部/季前缀（仅在后续仍存在话数标记或数字时剥离为 part_number）
# 例："第二章 13话" -> part=2, rest="13话"；"第二部第2话" -> part=2, rest="第2话"
_LEADING_PART_PREFIX_RE = re.compile(
    rf"^[\s【\[「『(（]*(?:连载版?\s*)?第\s*({_NUM_PAT})\s*[部季篇章][\s】\]」』)）:：·\-_]*",
    re.IGNORECASE,
)

# 一级正篇单位：严格仅匹配 "话 / 話 / 回"（优先级最高，不受前文“第X章”干扰）
_PRIMARY_HUA_RE = re.compile(
    rf"(?:第\s*)?({_NUM_PAT})\s*(?:话|話|回)",
    re.IGNORECASE,
)

# 二级正篇单位：仅当标题完全不含 "话/回" 时，才匹配 "第X章 / 第X集 / X章 / X集"
_SECONDARY_ZHANG_RE = re.compile(
    rf"(?:第\s*)?({_NUM_PAT})\s*(?:章|集|幕|讲)",
    re.IGNORECASE,
)

_EXPLICIT_VOL_NUM_RE = re.compile(
    rf"(?:第\s*|vol\.?\s*|卷\s*)({_NUM_PAT})\s*(?:卷|册|冊|单行本)?",
    re.IGNORECASE,
)
_CH_PREFIX_RE = re.compile(r"^(?:ch\.?|ep\.?|c)\s*(\d+(?:\.\d+)?)\b", re.IGNORECASE)
_PURE_NUM_RE = re.compile(r"^#?\s*(\d+(?:\.\d+)?)\s*$")
_LEADING_NUM_RE = re.compile(r"^(\d+(?:\.\d+)?)(?:\s+|[：:.\-_①②③④⑤(（]|$)")


@dataclass(slots=True)
class ClassifiedChapterMeta:
    """解析章节标题得到的完整结构化分类元数据。"""

    kind: ChapterKind
    number: float | None = None
    part_number: int | None = None
    sub_order: int = 0
    is_trial: bool = False


def _parse_num_token(token: str) -> float | None:
    tok = token.strip()
    if not tok:
        return None
    try:
        return float(tok)
    except ValueError:
        cn = parse_chinese_integer(tok)
        return float(cn) if cn is not None else None


def _extract_sub_order(simp_title: str) -> int:
    """提取同话拆分子序号（0=完整未拆分，1=前篇/上/①，2=中篇/中/②，3=后篇/下/③…）。"""
    for idx, ch in enumerate(("①", "②", "③", "④", "⑤", "⑥"), start=1):
        if ch in simp_title:
            return idx
    if re.search(r"(?:前篇|(?<![a-z0-9])上篇|[(（\s]上[)）\s]?$)", simp_title):
        return 1
    if re.search(r"(?:中篇|[(（\s]中[)）\s]?$)", simp_title):
        return 2
    if re.search(r"(?:后篇|(?<![a-z0-9])下篇|[(（\s]下[)）\s]?$)", simp_title):
        return 3
    m_paren = re.search(r"[(（](\d)[)）]\s*$", simp_title)
    if m_paren:
        return int(m_paren.group(1))
    return 0


def analyze_chapter_meta(
    title: str,
    group_name: str = "",
    hint_kind: ChapterKind | None = None,
) -> ClassifiedChapterMeta:
    """深度解析章节标题与分组，准确提取种类、部/章前缀、真实话数及拆分子序。"""
    simp_title = to_simplified(title).strip()
    simp_group = to_simplified(group_name).strip().lower()
    is_trial = "试看" in simp_title or "情报" in simp_title
    sub_order = _extract_sub_order(simp_title)

    # 0. 序章 / 序话 优先归为第 0 话
    if simp_title in ("序章", "序话", "序", "第0话", "第00话", "0话", "00话"):
        return ClassifiedChapterMeta(kind=ChapterKind.CHAPTER, number=0.0)

    # 1. 区间合集（如 "45-54话的附加彩色插画"）或显式杂项/短篇/附录一律归入 SPECIAL
    if _RANGE_CHAPTER_RE.search(simp_title):
        return ClassifiedChapterMeta(kind=ChapterKind.SPECIAL, number=None, is_trial=is_trial)

    if any(kw in simp_title for kw in _EXTRA_OMAKE_KEYWORDS):
        return ClassifiedChapterMeta(
            kind=ChapterKind.SPECIAL,
            number=extract_number_from_text(simp_title),
            sub_order=sub_order,
            is_trial=is_trial,
        )

    # 2. 尝试剥离首部的 "第X部 / 第X季 / 第X篇 / 第X章" 篇章前缀（仅当后续仍有话数/数字时生效）
    part_number: int | None = None
    work_body = simp_title
    m_part = _LEADING_PART_PREFIX_RE.match(simp_title)
    if m_part:
        remainder = simp_title[m_part.end() :].strip()
        # 只有当剩余部分还包含数字或“话/回/章/集”时，首部的“第X章/部”才是篇章前缀；
        # 若标题本身就是“第二章”或“第二章 王都风云”（无后续话数），则“第二章”本身就是章节号！
        if remainder and (
            _PRIMARY_HUA_RE.search(remainder)
            or _PURE_NUM_RE.match(remainder)
            or _LEADING_NUM_RE.match(remainder)
        ):
            parsed_part = _parse_num_token(m_part.group(1))
            if parsed_part is not None:
                part_number = int(parsed_part)
                work_body = remainder

    # 3. 优先提取一级单位 "话 / 回"（彻底免疫 "第二章 13话" 把 2 当成话数的问题）
    m_hua = _PRIMARY_HUA_RE.search(work_body)
    if m_hua:
        num = _parse_num_token(m_hua.group(1))
        if num is not None:
            kind = (
                ChapterKind.SPECIAL
                if any(kw in simp_group for kw in _SPECIAL_GROUP_KEYWORDS)
                else ChapterKind.CHAPTER
            )
            return ClassifiedChapterMeta(
                kind=kind,
                number=num,
                part_number=part_number,
                sub_order=sub_order,
                is_trial=is_trial,
            )

    # 4. 识别单行本/卷（分组名为单行本/book，或标题含 "第X卷" / "34卷" / "Vol.X" / "卷X"）
    is_vol_title = bool(
        re.search(
            rf"(?:(?:第\s*)?{_NUM_PAT}\s*[卷册冊]|^卷\s*{_NUM_PAT}|^vol\.?\s*\d|单行本)",
            work_body,
            re.IGNORECASE,
        )
    )
    is_vol_group = hint_kind == ChapterKind.VOLUME or any(
        kw in simp_group for kw in _VOLUME_GROUP_KEYWORDS
    )
    if is_vol_title or is_vol_group:
        m_vol_unit = re.search(rf"(?:第\s*)?({_NUM_PAT})\s*[卷册冊]", work_body, re.IGNORECASE)
        m_vol = m_vol_unit or _EXPLICIT_VOL_NUM_RE.search(work_body)
        num = _parse_num_token(m_vol.group(1)) if m_vol else extract_number_from_text(work_body)
        return ClassifiedChapterMeta(
            kind=ChapterKind.VOLUME,
            number=num,
            part_number=part_number,
            sub_order=sub_order,
            is_trial=is_trial,
        )

    # 5. 二级正篇单位：标题完全不含 "话/回" 时，支持以 "第X章 / 第X集" 命名的条漫/国漫
    m_zhang = _SECONDARY_ZHANG_RE.search(work_body)
    if m_zhang:
        num = _parse_num_token(m_zhang.group(1))
        if num is not None:
            return ClassifiedChapterMeta(
                kind=hint_kind or ChapterKind.CHAPTER,
                number=num,
                part_number=part_number,
                sub_order=sub_order,
                is_trial=is_trial,
            )

    # 6. Ch.1 / Ep.1 / 纯数字 "001" / 前导数字 "13 ①"
    for pattern in (_CH_PREFIX_RE, _PURE_NUM_RE, _LEADING_NUM_RE):
        m_num = pattern.match(work_body)
        if m_num:
            num = _parse_num_token(m_num.group(1))
            if num is not None:
                kind = (
                    ChapterKind.SPECIAL
                    if any(kw in simp_group for kw in _SPECIAL_GROUP_KEYWORDS)
                    else (hint_kind or ChapterKind.CHAPTER)
                )
                return ClassifiedChapterMeta(
                    kind=kind,
                    number=num,
                    part_number=part_number,
                    sub_order=sub_order,
                    is_trial=is_trial,
                )

    # 7. 未命中任何标准话数/卷数格式的杂项标题（如 "2026和也生日"、"角色设定"）：
    # 严禁将年份/纪念数字误提取为正篇话数（防止 2026 > 448 污染“最新话”），统一归入 SPECIAL
    return ClassifiedChapterMeta(
        kind=ChapterKind.VOLUME if hint_kind == ChapterKind.VOLUME else ChapterKind.SPECIAL,
        number=None,
        part_number=part_number,
        sub_order=sub_order,
        is_trial=is_trial,
    )


def classify_chapter_meta(
    title: str,
    group_name: str = "",
    hint_kind: ChapterKind | None = None,
) -> tuple[ChapterKind, float | None]:
    """向后兼容的二元组包装接口 `(kind, number)`。"""
    meta = analyze_chapter_meta(title, group_name, hint_kind=hint_kind)
    return meta.kind, meta.number


def parse_chapter_target(spec: str) -> ChapterTarget:
    """将用户输入的 `<第几话>` 字符串解析为结构化 `ChapterTarget`（含篇章号与子序号）。"""
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

    sub_raw = _extract_sub_order(simp)
    target_sub: int | None = sub_raw if sub_raw > 0 else None

    # 判断用户是否明确指定了“卷”
    if any(tok in lower for tok in ("卷", "册", "单行本", "vol")):
        num = extract_number_from_text(simp)
        if num is not None:
            return ChapterTarget(
                mode=TargetMode.VOLUME,
                number=num,
                sub_order=target_sub,
                raw_text=raw,
                normalized_text=f"第{format_chapter_num(num)}卷",
            )

    # 判断用户是否指定了番外/特别篇/小剧场等特殊章节
    if any(kw in simp for kw in ("番外", "特别", "外传", "小剧场", "短篇", "附录")):
        num = extract_number_from_text(simp)
        return ChapterTarget(
            mode=TargetMode.SPECIAL,
            number=num,
            sub_order=target_sub,
            raw_text=raw,
            normalized_text=normalize_for_match(simp),
        )

    # 检查是否带有 "第二部 / 第二章 / 第2季" 前缀 + 后续话数
    part_num: int | None = None
    body = simp
    m_part = _LEADING_PART_PREFIX_RE.match(simp)
    if m_part:
        rem = simp[m_part.end() :].strip()
        if rem and (
            _PRIMARY_HUA_RE.search(rem)
            or _SECONDARY_ZHANG_RE.search(rem)
            or extract_number_from_text(rem) is not None
        ):
            p_val = _parse_num_token(m_part.group(1))
            if p_val is not None:
                part_num = int(p_val)
                body = rem

    m_hua = _PRIMARY_HUA_RE.search(body)
    if m_hua:
        num = _parse_num_token(m_hua.group(1))
    else:
        m_zhang = _SECONDARY_ZHANG_RE.search(body)
        num = _parse_num_token(m_zhang.group(1)) if m_zhang else extract_number_from_text(body)

    if num is not None:
        part_prefix = f"第{part_num}部 " if part_num is not None else ""
        return ChapterTarget(
            mode=TargetMode.CHAPTER,
            number=num,
            part_number=part_num,
            sub_order=target_sub,
            raw_text=raw,
            normalized_text=f"{part_prefix}第{format_chapter_num(num)}话",
        )

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


def _effective_meta(c: ChapterInfo) -> tuple[int | None, int, bool]:
    """若调用方手工构造 `ChapterInfo` 未填 `part_number/sub_order`，自动按标题补齐。"""
    if c.part_number is not None or c.sub_order != 0 or c.is_trial:
        return c.part_number, c.sub_order, c.is_trial
    analyzed = analyze_chapter_meta(c.title, c.group_name, hint_kind=c.kind)
    return analyzed.part_number, analyzed.sub_order, analyzed.is_trial


def _pick_latest_chapter(chapters: list[ChapterInfo]) -> ChapterInfo:
    """选择真实最新连载正篇：严格隔离卷与特典杂项，按篇章号 -> 话数优先，免疫旧话补页导致的更新时间戳倒挂。"""
    regular = [c for c in chapters if c.kind == ChapterKind.CHAPTER]
    pool = regular if regular else chapters
    non_trial = [c for c in pool if not _effective_meta(c)[2]]
    if non_trial:
        pool = non_trial

    numbered = [c for c in pool if c.number is not None]
    if numbered:
        return max(
            numbered,
            key=lambda c: (
                (_effective_meta(c)[0] or 1),
                c.number if c.number is not None else -1.0,
                _effective_meta(c)[1],
                1 if c.group_name in ("", "连载", "默认", "默認", "default", "chapter") else 0,
                c.updated_at if c.updated_at > 0 else 0,
                c.order_index,
            ),
        )

    return max(
        pool,
        key=lambda c: (
            c.updated_at if c.updated_at > 0 else 0,
            c.order_index,
        ),
    )


def _describe_latest(chapters: list[ChapterInfo]) -> str:
    if not chapters:
        return ""
    return _pick_latest_chapter(chapters).title


def select_matching_chapter(
    chapters: list[ChapterInfo],
    target: ChapterTarget,
    manga_title: str = "",
) -> ChapterInfo:
    """在漫画章节列表中精准匹配目标章节；未命中时抛出带最新进度提示的 `ChapterNotFoundError`。"""
    if not chapters:
        raise ChapterNotFoundError(f"《{manga_title}》暂无可用章节列表")

    # 1. 最新话模式：严格筛选正篇并按最新篇章与最高话数定位
    if target.mode == TargetMode.LATEST:
        return _pick_latest_chapter(chapters)

    # 2. 单行本整卷模式
    if target.mode == TargetMode.VOLUME and target.number is not None:
        vol_matches = [
            c
            for c in chapters
            if c.kind == ChapterKind.VOLUME and _num_equals(c.number, target.number)
        ]
        if vol_matches:
            return vol_matches[0]
        title_vol_matches = [
            c
            for c in chapters
            if "卷" in to_simplified(c.title)
            and c.kind != ChapterKind.SPECIAL
            and _num_equals(c.number, target.number)
        ]
        if title_vol_matches:
            return title_vol_matches[0]

    # 3. 常规单话模式（严格隔离卷/番外，精准区分第一部正篇与第二部/第二章）
    if target.mode == TargetMode.CHAPTER and target.number is not None:
        chap_candidates = [
            c
            for c in chapters
            if c.kind == ChapterKind.CHAPTER and _num_equals(c.number, target.number)
        ]

        if target.part_number is not None:
            # 用户显式指定了“第X部/第X章”：优先筛选 part_number == target.part_number 的章节；
            # 若该漫画本身已是独立词条（所有章节均无 part_number），允许回退到无 part_number 章节
            exact_part = [c for c in chap_candidates if _effective_meta(c)[0] == target.part_number]
            has_any_part_in_manga = any(_effective_meta(c)[0] is not None for c in chapters)
            if exact_part:
                chap_candidates = exact_part
            elif has_any_part_in_manga:
                chap_candidates = []

        if chap_candidates:

            def _rank_chapter(c: ChapterInfo) -> tuple[int, int, int, int, int]:
                c_part, c_sub, c_trial = _effective_meta(c)
                # (1) 未指定篇章时，无前缀/第一部 (part in (None, 1)) 绝对优先于第二部及以后
                if target.part_number is None:
                    part_rank = 0 if (c_part is None or c_part == 1) else (10 + c_part)
                else:
                    part_rank = 0 if c_part == target.part_number else 1

                # (2) 子篇章匹配：若用户指定了前/中/后篇则精确匹配最优先；否则完整话(0) > 前篇/①(1) > 后篇(3)
                if target.sub_order is not None:
                    sub_rank = (
                        0 if c_sub == target.sub_order else (10 + abs(c_sub - target.sub_order))
                    )
                else:
                    sub_rank = c_sub

                # (3) 正式版优先于“试看”版
                trial_rank = 1 if c_trial else 0

                # (4) 主连载分组优先于其他分组
                grp_rank = (
                    0 if c.group_name in ("", "连载", "默认", "默認", "default", "chapter") else 1
                )

                return (part_rank, sub_rank, trial_rank, grp_rank, c.order_index)

            chap_candidates.sort(key=_rank_chapter)
            return chap_candidates[0]

        # 序章 (0话) 兼容标题为 "序章"/"序" 的章节
        if _num_equals(target.number, 0.0) and target.part_number is None:
            for c in chapters:
                if to_simplified(c.title).strip() in ("序章", "序话", "序"):
                    return c

    # 4. 特殊篇章 / 标题文本精确匹配兜底（如 "番外1", "短篇小剧场2", "特别篇"）
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


_LATEST_N_RE = re.compile(
    rf"^(?:(?:最新|最近|最后)\s*({_NUM_PAT})\s*(?:话|回|章|集)?|latest\s*(\d+))$",
    re.IGNORECASE,
)
_BATCH_SEG_ATOM_PAT = (
    rf"(?:第\s*|ch\.?\s*|ep\.?\s*|vol\.?\s*|卷\s*)?({_NUM_PAT})\s*(?:话|回|章|集|卷|册|单行本)?"
)
_BATCH_RANGE_SEG_RE = re.compile(
    rf"^\s*{_BATCH_SEG_ATOM_PAT}\s*(?:[-~～—–]|(?:\.\.)|至|到)\s*{_BATCH_SEG_ATOM_PAT}\s*$",
    re.IGNORECASE,
)
_LIST_SEP_RE = re.compile(r"[,，、;；]+")


def _strip_leading_part_for_batch(simp: str) -> tuple[int | None, str]:
    m_part = _LEADING_PART_PREFIX_RE.match(simp)
    if m_part:
        rem = simp[m_part.end() :].strip()
        if rem and extract_number_from_text(rem) is not None:
            p_val = _parse_num_token(m_part.group(1))
            if p_val is not None:
                return int(p_val), rem
    return None, simp


def is_batch_chapter_spec(spec: str) -> bool:
    """判断用户输入的话数描述是否为多话批量表达式（区间、逗号/顿号列表或最新N话）。"""
    simp = to_simplified(spec or "").strip()
    if not simp or simp.lower() in _LATEST_KEYWORDS:
        return False
    if _LATEST_N_RE.match(simp):
        return True
    _, body = _strip_leading_part_for_batch(simp)
    if _LIST_SEP_RE.search(body):
        return True
    return bool(_BATCH_RANGE_SEG_RE.match(body))


def _dedupe_numbered_pool(pool: list[ChapterInfo]) -> list[ChapterInfo]:
    """按 `(part, number, sub_order)` 去重多汉化组重复章节，并自动合并/保留真实上下篇。"""
    best_by_key: dict[tuple[int, float, int], ChapterInfo] = {}

    def _cand_rank(c: ChapterInfo) -> tuple[int, int, int]:
        _, _, c_trial = _effective_meta(c)
        grp_rank = 0 if c.group_name in ("", "连载", "默认", "默認", "default", "chapter") else 1
        return (1 if c_trial else 0, grp_rank, c.order_index)

    for c in pool:
        if c.number is None:
            continue
        c_part, c_sub, _ = _effective_meta(c)
        key = (c_part or 1, round(c.number, 4), c_sub)
        prev = best_by_key.get(key)
        if prev is None or _cand_rank(c) < _cand_rank(prev):
            best_by_key[key] = c

    has_whole = {(p, n) for (p, n, sub) in best_by_key if sub == 0}
    filtered = [
        chap for (p, n, sub), chap in best_by_key.items() if sub == 0 or (p, n) not in has_whole
    ]
    filtered.sort(
        key=lambda c: (
            (_effective_meta(c)[0] or 1),
            round(c.number or 0.0, 4),
            _effective_meta(c)[1],
            c.order_index,
        )
    )
    return filtered


def _expand_range_in_chapters(
    chapters: list[ChapterInfo],
    lo: float,
    hi: float,
    *,
    is_vol: bool,
    part_num: int | None,
    manga_title: str,
) -> list[ChapterInfo]:
    if is_vol:
        pool = [
            c
            for c in chapters
            if (
                c.kind == ChapterKind.VOLUME
                or ("卷" in to_simplified(c.title) and c.kind != ChapterKind.SPECIAL)
            )
        ]
    else:
        pool = [c for c in chapters if c.kind == ChapterKind.CHAPTER]
        if part_num is not None:
            exact_part = [c for c in pool if _effective_meta(c)[0] == part_num]
            has_any_part = any(_effective_meta(c)[0] is not None for c in chapters)
            if exact_part:
                pool = exact_part
            elif has_any_part:
                pool = []
        else:
            p1_pool = [c for c in pool if _effective_meta(c)[0] in (None, 1)]
            if p1_pool:
                pool = p1_pool

    in_range = [c for c in pool if c.number is not None and (lo - 1e-4) <= c.number <= (hi + 1e-4)]
    has_lo = any(_num_equals(c.number, lo) for c in in_range)
    has_hi = any(_num_equals(c.number, hi) for c in in_range)
    if not in_range or not has_lo or not has_hi:
        unit = "卷" if is_vol else "话"
        missing_num = lo if not has_lo else hi
        latest_hint = _describe_latest(chapters)
        suffix = f"（当前最新：{latest_hint}）" if latest_hint else ""
        raise ChapterNotFoundError(
            f"未找到《{manga_title}》区间内的第{format_chapter_num(missing_num)}{unit}{suffix}",
            latest_hint=latest_hint,
        )

    return _dedupe_numbered_pool(in_range)


def select_matching_chapters_batch(
    chapters: list[ChapterInfo],
    spec: str,
    *,
    manga_title: str = "",
    max_chapters: int = 10,
) -> list[ChapterInfo]:
    """解析单话或批量话数表达式（如 `1-5话`、`1,3,5话`、`1,3-5话`、`最新3话`、`第二部 1-3话`），按阅读顺序返回目标章节列表。"""
    raw = (spec or "").strip()
    simp = to_simplified(raw).strip()
    limit = max(1, int(max_chapters))

    # 1. `最新N话 / 最近N话 / 最后N话`
    m_latest_n = _LATEST_N_RE.match(simp)
    if m_latest_n:
        tok = m_latest_n.group(1) or m_latest_n.group(2) or ""
        parsed_n = _parse_num_token(tok)
        count = int(parsed_n) if parsed_n is not None else 0
        if count <= 0:
            raise CommandUsageError("批量最新话数量必须大于 0。")
        if count > limit:
            raise CommandUsageError(
                f"单次批量请求共 {count} 话，超出当前单次批量上限（{limit} 话）；"
                f"请缩小范围或在插件设置中调大 delivery.max_batch_chapters。"
            )
        if not chapters:
            raise ChapterNotFoundError(f"《{manga_title}》暂无可用章节列表")

        regular = [c for c in chapters if c.kind == ChapterKind.CHAPTER]
        pool = regular if regular else chapters
        non_trial = [c for c in pool if not _effective_meta(c)[2]]
        if non_trial:
            pool = non_trial
        canonical = _dedupe_numbered_pool(pool)
        if canonical:
            return canonical[-count:]
        ordered_fallback = sorted(
            pool,
            key=lambda c: (c.updated_at if c.updated_at > 0 else 0, c.order_index),
        )
        return ordered_fallback[-count:]

    # 2. 检查是否为区间或多话列表（如 `1-5话`、`1,3,5话`、`第二部 1-3话`、`1-3卷`）
    part_num, body = _strip_leading_part_for_batch(simp)
    has_list_sep = bool(_LIST_SEP_RE.search(body))
    m_whole_range = None if has_list_sep else _BATCH_RANGE_SEG_RE.match(body)

    if not has_list_sep and m_whole_range is None:
        # 常规单话模式，100% 保持原行为
        return [
            select_matching_chapter(chapters, parse_chapter_target(raw), manga_title=manga_title)
        ]

    if not chapters:
        raise ChapterNotFoundError(f"《{manga_title}》暂无可用章节列表")

    is_vol = any(tok in body.lower() for tok in ("卷", "册", "单行本", "vol"))
    segments = [s.strip() for s in _LIST_SEP_RE.split(body) if s.strip()]
    if not segments:
        raise CommandUsageError(f"无法解析批量话数「{raw}」")

    collected: list[ChapterInfo] = []
    seen_ids: set[str] = set()

    for seg in segments:
        m_seg_rng = _BATCH_RANGE_SEG_RE.match(seg)
        if m_seg_rng:
            n1 = _parse_num_token(m_seg_rng.group(1))
            n2 = _parse_num_token(m_seg_rng.group(2))
            if n1 is None or n2 is None:
                raise CommandUsageError(f"无法识别区间话数「{seg}」")
            lo, hi = (n1, n2) if n1 <= n2 else (n2, n1)
            if (hi - lo) > max(500, limit * 5):
                raise CommandUsageError(
                    f"请求的区间跨度过大（{format_chapter_num(lo)}~{format_chapter_num(hi)}），"
                    f"当前单次批量上限为 {limit} 话。"
                )
            seg_chaps = _expand_range_in_chapters(
                chapters,
                lo,
                hi,
                is_vol=is_vol,
                part_num=part_num,
                manga_title=manga_title,
            )
            for ch in seg_chaps:
                if ch.chapter_id not in seen_ids:
                    seen_ids.add(ch.chapter_id)
                    collected.append(ch)
                if len(collected) > limit:
                    raise CommandUsageError(
                        f"单次批量匹配超过当前上限（最多 {limit} 话）；"
                        f"请缩小区间或在插件设置中调大 delivery.max_batch_chapters。"
                    )
        else:
            if is_vol and not any(k in seg.lower() for k in ("卷", "册", "单行本", "vol")):
                sub_spec = f"第{seg}卷"
            elif part_num is not None:
                sub_spec = f"第{part_num}部 {seg}"
            else:
                sub_spec = seg
            ch = select_matching_chapter(
                chapters,
                parse_chapter_target(sub_spec),
                manga_title=manga_title,
            )
            if ch.chapter_id not in seen_ids:
                seen_ids.add(ch.chapter_id)
                collected.append(ch)
            if len(collected) > limit:
                raise CommandUsageError(
                    f"单次批量请求超出当前上限（最多 {limit} 话）；"
                    f"请减少话数或在插件设置中调大 delivery.max_batch_chapters。"
                )

    return collected
