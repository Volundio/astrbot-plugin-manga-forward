"""Komiic 漫画源实现。

特性：
1. 基于 GraphQL `/api/query` 实现搜索、章节拉取（区分话 `chapter` 与卷 `book`）与整话图片解析；
2. 支持账号密码登录持久化、`/auth/refresh` 自动续期或直接配置 Cookie；
3. 图片请求自动注入页级防盗链 `Referer` 与登录态 Cookie，精准识别 `HTTP 402` 免费配额耗尽状态。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from astrbot.api import logger

from ..auth_manager import AuthManager
from ..chapter_matcher import classify_chapter_meta
from ..config import KomiicConfig
from ..http_client import AsyncHttpClient
from ..models import (
    ChapterInfo,
    ChapterKind,
    MangaCandidate,
    MangaNotFoundError,
    PageSpec,
    SourceAuthError,
    SourceId,
)
from ..text_utils import score_title_match, to_traditional
from .base import BaseMangaSource

_SEARCH_GQL = """
query searchComicsAndAuthors($keyword: String!) {
    searchComicsAndAuthors(keyword: $keyword) {
        comics {
            id
            title
            status
            imageUrl
            authors {
                id
                name
            }
        }
    }
}
""".strip()

_COMIC_BY_ID_GQL = """
query comicById($comicId: ID!) {
    comicById(comicId: $comicId) {
        id
        title
        imageUrl
    }
}
""".strip()

_CHAPTERS_GQL = """
query chapterByComicId($comicId: ID!) {
    chaptersByComicId(comicId: $comicId) {
        id
        serial
        type
        size
        dateUpdated
        dateCreated
    }
}
""".strip()

_IMAGES_GQL = """
query imagesByChapterId($chapterId: ID!) {
    imagesByChapterId(chapterId: $chapterId) {
        kid
    }
}
""".strip()


def _parse_iso_ts(val: str) -> int:
    s = (val or "").strip()
    if not s:
        return 0
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:
        return 0


class KomiicSource(BaseMangaSource):
    """Komiic 数据源适配器。"""

    source_id = SourceId.KOMIIC

    def __init__(
        self,
        cfg_getter: Any,
        http: AsyncHttpClient,
        auth: AuthManager,
    ) -> None:
        self._get_cfg = cfg_getter
        self._http = http
        self._auth = auth

    @property
    def cfg(self) -> KomiicConfig:
        return self._get_cfg()

    @property
    def base_url(self) -> str:
        return self.cfg.base_url.rstrip("/")

    def _has_relogin_capability(self) -> bool:
        c = self.cfg
        return bool(c.username.strip() and c.password)

    async def _graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any],
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        auth_headers = await self._auth.get_komiic_auth_headers(
            self.cfg, force_refresh=force_refresh
        )
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": self.base_url,
            "Referer": f"{self.base_url}/",
        }
        headers.update(auth_headers)

        resp = await self._http.post(
            f"{self.base_url}/api/query",
            headers=headers,
            json_body={
                "operationName": operation_name,
                "variables": variables,
                "query": query,
            },
        )
        if resp.status == 401 and not force_refresh and self._has_relogin_capability():
            logger.info("Komiic GraphQL 返回 401，正在自动刷新登录态后重试…")
            return await self._graphql(operation_name, query, variables, force_refresh=True)
        if resp.status != 200:
            raise RuntimeError(f"Komiic GraphQL 请求失败 ({operation_name}): HTTP {resp.status}")

        payload = resp.json()
        errors = payload.get("errors")
        if isinstance(errors, list) and errors and not payload.get("data"):
            msg = "; ".join(str(e.get("message") or e) for e in errors if isinstance(e, dict))
            raise RuntimeError(f"Komiic GraphQL 错误: {msg or errors}")
        return payload.get("data") or {}

    async def search_manga(self, keyword: str) -> list[MangaCandidate]:
        queries = [keyword.strip()]
        trad_kw = to_traditional(keyword).strip()
        if trad_kw and trad_kw not in queries:
            queries.append(trad_kw)

        merged: dict[str, MangaCandidate] = {}
        for q in queries:
            data = await self._graphql(
                "searchComicsAndAuthors",
                _SEARCH_GQL,
                {"keyword": q},
            )
            comics = ((data.get("searchComicsAndAuthors") or {}).get("comics")) or []
            if not isinstance(comics, list):
                continue
            for item in comics:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("id") or "").strip()
                title = str(item.get("title") or "").strip()
                if not cid or not title:
                    continue
                raw_authors = item.get("authors") or []
                authors = [
                    str(a.get("name") or "").strip()
                    for a in raw_authors
                    if isinstance(a, dict) and a.get("name")
                ]
                score = score_title_match(keyword, title)
                if score < 0.35:
                    continue
                cand = MangaCandidate(
                    source=SourceId.KOMIIC,
                    manga_id=cid,
                    title=title,
                    authors=authors,
                    cover_url=str(item.get("imageUrl") or ""),
                    match_score=score,
                    url=f"{self.base_url}/comic/{cid}",
                )
                prev = merged.get(cid)
                if prev is None or cand.match_score > prev.match_score:
                    merged[cid] = cand
            if any(c.match_score >= 0.90 for c in merged.values()):
                break

        results = sorted(merged.values(), key=lambda c: c.match_score, reverse=True)
        if not results:
            raise MangaNotFoundError(f"未检索到与「{keyword}」匹配的漫画")
        return results[:12]

    async def get_manga_by_id(self, manga_id: str) -> MangaCandidate:
        data = await self._graphql("comicById", _COMIC_BY_ID_GQL, {"comicId": str(manga_id)})
        comic = data.get("comicById") or {}
        title = str(comic.get("title") or f"Komiic#{manga_id}")
        return MangaCandidate(
            source=SourceId.KOMIIC,
            manga_id=str(manga_id),
            title=title,
            cover_url=str(comic.get("imageUrl") or ""),
            match_score=1.0,
            url=f"{self.base_url}/comic/{manga_id}",
        )

    async def fetch_chapters(self, manga: MangaCandidate) -> list[ChapterInfo]:
        data = await self._graphql(
            "chapterByComicId",
            _CHAPTERS_GQL,
            {"comicId": manga.manga_id},
        )
        raw_list = data.get("chaptersByComicId") or []
        if not isinstance(raw_list, list) or not raw_list:
            raise SourceAuthError(f"Komiic《{manga.title}》暂无可用章节")

        chapters: list[ChapterInfo] = []
        for idx, item in enumerate(raw_list):
            if not isinstance(item, dict):
                continue
            cid = str(item.get("id") or "").strip()
            serial = str(item.get("serial") or "").strip()
            ctype = str(item.get("type") or "chapter").strip().lower()
            if not cid or not serial:
                continue

            if ctype == "book":
                display_title = f"第 {serial} 卷"
                hint = ChapterKind.VOLUME
                grp = "单行本"
            elif ctype == "chapter":
                display_title = f"第 {serial} 话"
                hint = ChapterKind.CHAPTER
                grp = "连载"
            else:
                display_title = serial
                hint = None
                grp = ctype

            kind, num = classify_chapter_meta(display_title, grp, hint_kind=hint)
            upd = _parse_iso_ts(str(item.get("dateUpdated") or item.get("dateCreated") or ""))
            chapters.append(
                ChapterInfo(
                    chapter_id=cid,
                    title=display_title,
                    number=num,
                    kind=kind,
                    group_name=grp,
                    order_index=idx,
                    page_count=int(item.get("size") or 0),
                    updated_at=upd,
                    url=f"{self.base_url}/comic/{manga.manga_id}/chapter/{cid}/images/all",
                )
            )
        return chapters

    async def fetch_chapter_pages(
        self, manga: MangaCandidate, chapter: ChapterInfo
    ) -> list[PageSpec]:
        return await self._build_page_specs(manga, chapter, force_refresh=False)

    async def _build_page_specs(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        *,
        force_refresh: bool,
    ) -> list[PageSpec]:
        data = await self._graphql(
            "imagesByChapterId",
            _IMAGES_GQL,
            {"chapterId": chapter.chapter_id},
            force_refresh=force_refresh,
        )
        raw_images = data.get("imagesByChapterId") or []
        if not isinstance(raw_images, list) or not raw_images:
            raise SourceAuthError(f"Komiic 未返回《{manga.title}》{chapter.title} 的图片列表")

        auth_headers = await self._auth.get_komiic_auth_headers(
            self.cfg, force_refresh=force_refresh
        )
        specs: list[PageSpec] = []
        for idx, img in enumerate(raw_images):
            if not isinstance(img, dict):
                continue
            kid = str(img.get("kid") or "").strip()
            if not kid:
                continue
            img_headers = {
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Referer": (
                    f"{self.base_url}/comic/{manga.manga_id}/chapter/"
                    f"{chapter.chapter_id}/page/{idx + 1}"
                ),
            }
            img_headers.update(auth_headers)
            specs.append(
                PageSpec(
                    index=len(specs),
                    url=f"{self.base_url}/api/image/{kid}",
                    headers=img_headers,
                )
            )
        if not specs:
            raise SourceAuthError(f"Komiic《{manga.title}》{chapter.title} 图片列表为空")
        return specs

    async def refresh_single_page_url(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        page_index: int,
    ) -> PageSpec | None:
        if not self._has_relogin_capability():
            return None
        try:
            refreshed = await self._build_page_specs(manga, chapter, force_refresh=True)
            if 0 <= page_index < len(refreshed):
                return refreshed[page_index]
        except Exception as exc:
            logger.debug(f"Komiic 刷新单页鉴权 Cookie 失败 (page={page_index}): {exc}")
        return None
