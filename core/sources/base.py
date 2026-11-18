"""抽象漫画源适配器基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ChapterComment, ChapterInfo, MangaCandidate, PageSpec, SourceId


class BaseMangaSource(ABC):
    """所有漫画源统一遵循的异步协议接口。"""

    source_id: SourceId

    @abstractmethod
    async def search_manga(self, keyword: str) -> list[MangaCandidate]:
        """按漫画名称检索候选列表，结果按匹配度从高到低排序。"""

    @abstractmethod
    async def get_manga_by_id(self, manga_id: str) -> MangaCandidate:
        """按显式源漫画 ID 直接构造或拉取漫画候选信息。"""

    @abstractmethod
    async def fetch_chapters(self, manga: MangaCandidate) -> list[ChapterInfo]:
        """拉取指定漫画的全部章节列表。"""

    @abstractmethod
    async def fetch_chapter_pages(
        self, manga: MangaCandidate, chapter: ChapterInfo
    ) -> list[PageSpec]:
        """拉取指定话数的全部图片规格，必须严格按真实页序 0..N-1 返回。"""

    async def refresh_all_page_urls(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
    ) -> list[PageSpec] | None:
        """当整卷/长章节下载中途防盗链签名批量过期 (401/403) 时，一次性重刷整章签名 URL 列表。"""
        return None

    async def refresh_single_page_url(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        page_index: int,
    ) -> PageSpec | None:
        """当单页图片防盗链签名过期 (403) 时，可选重刷章节获取最新签名 URL。"""
        refreshed = await self.refresh_all_page_urls(manga, chapter)
        if refreshed is not None and 0 <= page_index < len(refreshed):
            return refreshed[page_index]
        return None

    async def fetch_chapter_comments(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        max_count: int = 20,
    ) -> list[ChapterComment]:
        """拉取单话评论 / 章末吐槽（不支持的源默认返回空列表）。"""
        return []
