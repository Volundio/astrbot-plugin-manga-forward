"""核心领域数据模型与异常层级定义。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SourceId(str, Enum):
    """支持的漫画源唯一标识。"""

    ZAIMANHUA = "zaimanhua"
    COPYMANGA = "copymanga"
    KOMIIC = "komiic"

    @property
    def display_name(self) -> str:
        if self == SourceId.ZAIMANHUA:
            return "再漫画"
        if self == SourceId.COPYMANGA:
            return "拷贝漫画"
        return "Komiic"

    @property
    def supports_comments(self) -> bool:
        """仅再漫画与拷贝漫画支持附带单话评论/章末吐槽。"""
        return self in (SourceId.ZAIMANHUA, SourceId.COPYMANGA)

    @classmethod
    def from_alias(cls, raw: str) -> SourceId | None:
        token = (raw or "").strip().lower()
        alias_map: dict[str, SourceId] = {
            "zaimanhua": cls.ZAIMANHUA,
            "zmh": cls.ZAIMANHUA,
            "再漫画": cls.ZAIMANHUA,
            "动漫之家": cls.ZAIMANHUA,
            "dmzj": cls.ZAIMANHUA,
            "copymanga": cls.COPYMANGA,
            "copy": cls.COPYMANGA,
            "mangacopy": cls.COPYMANGA,
            "拷贝漫画": cls.COPYMANGA,
            "拷贝": cls.COPYMANGA,
            "拷貝漫畫": cls.COPYMANGA,
            "komiic": cls.KOMIIC,
        }
        return alias_map.get(token)


class ChapterKind(str, Enum):
    """章节类型：连载单话、单行本整卷、番外/特殊篇。"""

    CHAPTER = "chapter"
    VOLUME = "volume"
    SPECIAL = "special"


class TargetMode(str, Enum):
    """用户请求的章节匹配模式。"""

    CHAPTER = "chapter"
    VOLUME = "volume"
    LATEST = "latest"
    SPECIAL = "special"


@dataclass(slots=True)
class ParsedMangaCommand:
    """解析后的用户 `manga` 指令结构。"""

    manga_query: str
    chapter_spec: str
    forced_source: SourceId | None = None
    explicit_manga_id: str | None = None


@dataclass(slots=True)
class ChapterTarget:
    """归一化后的章节定位目标。"""

    mode: TargetMode
    number: float | None = None
    part_number: int | None = None
    sub_order: int | None = None
    raw_text: str = ""
    normalized_text: str = ""


@dataclass(slots=True)
class MangaCandidate:
    """漫画检索候选词条。"""

    source: SourceId
    manga_id: str
    title: str
    aliases: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    cover_url: str = ""
    need_login: bool = False
    match_score: float = 0.0
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChapterInfo:
    """单话或单卷章节元数据。"""

    chapter_id: str
    title: str
    number: float | None = None
    part_number: int | None = None
    sub_order: int = 0
    is_trial: bool = False
    kind: ChapterKind = ChapterKind.CHAPTER
    group_name: str = ""
    order_index: int = 0
    page_count: int = 0
    updated_at: int = 0
    url: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PageSpec:
    """单张漫画图片拉取描述（严格按 index 0..N-1 排序）。"""

    index: int
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ChapterComment:
    """单话评论 / 章末吐槽条目。"""

    content: str
    author: str = ""
    likes: int = 0


@dataclass(slots=True)
class DownloadedPage:
    """已下载到本地临时作用域的单页结果（严格保留原始页码 index）。"""

    index: int
    local_path: Path | None
    size_bytes: int = 0
    error: str = ""


@dataclass(slots=True)
class ResolvedChapterBundle:
    """完成源解析、图片落盘与评论拉取渲染后的整话投递数据包。"""

    source: SourceId
    manga: MangaCandidate
    chapter: ChapterInfo
    downloaded_pages: list[DownloadedPage]
    comments: list[ChapterComment] = field(default_factory=list)
    comment_image_paths: list[Path] = field(default_factory=list)


class MangaForwardError(Exception):
    """插件基础业务异常。"""


class CommandUsageError(MangaForwardError):
    """用户指令格式不正确。"""


class MangaNotFoundError(MangaForwardError):
    """当前漫画源未检索到匹配的漫画。"""


class ChapterNotFoundError(MangaForwardError):
    """找到了漫画，但未找到用户指定的话数。"""

    def __init__(self, message: str, latest_hint: str = "") -> None:
        super().__init__(message)
        self.latest_hint = latest_hint


class SourceAuthError(MangaForwardError):
    """源站要求登录、登录态失效且无法自动刷新，或账号等级权限不足。"""


class SourceThrottleError(MangaForwardError):
    """源站触发防爬频控或临时限制（如拷贝漫画 code=210）。"""


class SourceQuotaError(MangaForwardError):
    """源站未登录免费阅读图片次数耗尽（如 Komiic HTTP 402）。"""


class AllSourcesFailedError(MangaForwardError):
    """所有启用的漫画源均未能完成本次解析。"""

    def __init__(self, attempts: list[tuple[SourceId, str]]) -> None:
        self.attempts = attempts
        lines = [f"• {src.display_name}：{reason}" for src, reason in attempts]
        detail = "\n".join(lines) if lines else "没有已启用的漫画源，请在插件设置中启用至少一个源。"
        super().__init__(f"未能获取指定漫画章节：\n{detail}")
