"""再漫画 (Zaimanhua) 漫画源实现。

特性：
1. 严格通过 `zmh.shadows.de5.net` 静态索引站检索漫画 ID，绝不请求官方搜索接口；
2. 支持账号密码自动登录持久化与失效自动重登，或直接配置 Cookie/Token；
3. 支持 v4 App 接口 + PC comic2 接口双通道获取隐藏漫画与隐藏章节；
4. 支持单页防盗链签名过期自动重刷重试；
5. 支持拉取 `/viewpoint/list` 单话评论/章末吐槽。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from astrbot.api import logger

from ..auth_manager import AuthManager
from ..chapter_matcher import analyze_chapter_meta
from ..config import ZaimanhuaConfig
from ..http_client import AsyncHttpClient
from ..models import (
    ChapterComment,
    ChapterInfo,
    MangaCandidate,
    MangaNotFoundError,
    PageSpec,
    SourceAuthError,
    SourceId,
)
from .base import BaseMangaSource
from .zmh_shadow_index import ZmhShadowSearchClient

ZMH_V4_API_BASE = "https://v4api.zaimanhua.com/app/v1"
ZMH_PC_API_BASE = "https://manhua.zaimanhua.com/api/v1/comic2"
ZMH_REFERER = "https://manhua.zaimanhua.com/"


class ZaimanhuaSource(BaseMangaSource):
    """再漫画数据源适配器。"""

    source_id = SourceId.ZAIMANHUA

    def __init__(
        self,
        cfg_getter: Any,
        http: AsyncHttpClient,
        auth: AuthManager,
        shadow_index: ZmhShadowSearchClient,
    ) -> None:
        self._get_cfg = cfg_getter
        self._http = http
        self._auth = auth
        self._shadow_index = shadow_index
        self._refresh_pages_lock = asyncio.Lock()
        self._refreshed_pages_cache: dict[tuple[str, str], tuple[float, list[PageSpec]]] = {}

    @property
    def cfg(self) -> ZaimanhuaConfig:
        return self._get_cfg()

    def _has_relogin_capability(self) -> bool:
        c = self.cfg
        return bool(c.username.strip() and c.password)

    async def _build_headers(
        self, *, platform: str = "pc", force_refresh: bool = False
    ) -> dict[str, str]:
        auth_headers = await self._auth.get_zaimanhua_auth_headers(
            self.cfg, force_refresh=force_refresh
        )
        headers = {
            "Referer": ZMH_REFERER,
            "Origin": "https://manhua.zaimanhua.com",
            "X-Requested-With": "XMLHttpRequest",
        }
        if platform:
            headers["Platform"] = platform
        headers.update(auth_headers)
        return headers

    async def search_manga(self, keyword: str) -> list[MangaCandidate]:
        c = self.cfg
        candidates = await self._shadow_index.search(
            keyword,
            site_url=c.search_site_url,
            need_login_filter=c.need_login_filter,
            limit=12,
        )
        if not candidates:
            raise MangaNotFoundError(f"未检索到与「{keyword}」匹配的漫画")
        return candidates

    async def get_manga_by_id(self, manga_id: str) -> MangaCandidate:
        headers = await self._build_headers(platform="pc")
        resp = await self._http.get(
            f"{ZMH_V4_API_BASE}/comic/detail/{manga_id}",
            params={"_v": "2.2.5"},
            headers=headers,
        )
        title = f"再漫画#{manga_id}"
        if resp.status == 200:
            try:
                payload = resp.json()
                inner = ((payload.get("data") or {}).get("data")) or {}
                if inner.get("title"):
                    title = str(inner["title"])
            except Exception:
                pass
        return MangaCandidate(
            source=SourceId.ZAIMANHUA,
            manga_id=str(manga_id),
            title=title,
            match_score=1.0,
            url=f"https://manhua.zaimanhua.com/details/{manga_id}",
        )

    @staticmethod
    def _parse_chapter_groups(
        manga_id: str,
        raw_groups: list[dict[str, Any]],
    ) -> list[ChapterInfo]:
        chapters: list[ChapterInfo] = []
        seq = 0
        for grp in raw_groups:
            if not isinstance(grp, dict):
                continue
            group_title = str(grp.get("title") or "连载").strip()
            items = grp.get("data")
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                cid = str(item.get("chapter_id") or "").strip()
                ctitle = str(item.get("chapter_title") or "").strip()
                if not cid or not ctitle:
                    continue
                meta = analyze_chapter_meta(ctitle, group_title)
                upd = int(item.get("updatetime") or 0)
                order_val = int(item.get("chapter_order") or seq)
                seq += 1
                chapters.append(
                    ChapterInfo(
                        chapter_id=cid,
                        title=ctitle,
                        number=meta.number,
                        part_number=meta.part_number,
                        sub_order=meta.sub_order,
                        is_trial=meta.is_trial,
                        kind=meta.kind,
                        group_name=group_title,
                        order_index=order_val,
                        updated_at=upd,
                        url=f"https://m.zaimanhua.com/pages/comic/page?comic_id={manga_id}&chapter_id={cid}",
                    )
                )
        return chapters

    async def fetch_chapters(self, manga: MangaCandidate) -> list[ChapterInfo]:
        manga_id = manga.manga_id
        for attempt in range(2):
            force = attempt > 0
            headers = await self._build_headers(platform="pc", force_refresh=force)

            v4_data: dict[str, Any] = {}
            need_reauth = False
            resp = await self._http.get(
                f"{ZMH_V4_API_BASE}/comic/detail/{manga_id}",
                params={"_v": "2.2.5"},
                headers=headers,
            )
            if resp.status == 401:
                need_reauth = True
            elif resp.status == 200:
                payload = resp.json()
                errno = payload.get("errno", 0)
                if errno == 99:
                    need_reauth = True
                else:
                    wrapper = payload.get("data")
                    if isinstance(wrapper, dict):
                        v4_data = (
                            wrapper.get("data")
                            if isinstance(wrapper.get("data"), dict)
                            else wrapper
                        )

            if need_reauth and attempt == 0 and self._has_relogin_capability():
                logger.info(f"再漫画登录态已失效，正在自动重登后重试详情 (id={manga_id})…")
                continue

            if v4_data.get("title") and manga.title.startswith("再漫画#"):
                manga.title = str(v4_data["title"])

            raw_groups = v4_data.get("chapters") or v4_data.get("chapterList") or []
            is_hide_chapter = int(v4_data.get("isHideChapter") or 0) == 1
            can_read = v4_data.get("canRead", True)

            if can_read is False and attempt == 0 and self._has_relogin_capability():
                logger.info(f"再漫画章节可读状态受限，尝试刷新登录态重试 (id={manga_id})…")
                continue

            # 若标记隐藏章节或 v4 章节为空，调用 PC 端 comic2/comic/detail 补全完整章节
            if is_hide_chapter or not raw_groups:
                pc_resp = await self._http.get(
                    f"{ZMH_PC_API_BASE}/comic/detail",
                    params={"id": manga_id},
                    headers=headers,
                )
                if pc_resp.status == 200:
                    pc_payload = pc_resp.json()
                    if (
                        pc_payload.get("errno") == 99
                        and attempt == 0
                        and self._has_relogin_capability()
                    ):
                        continue
                    comic_info = (pc_payload.get("data") or {}).get("comicInfo") or {}
                    pc_groups = comic_info.get("chapterList") or []
                    if pc_groups:
                        raw_groups = pc_groups
                    if comic_info.get("title") and manga.title.startswith("再漫画#"):
                        manga.title = str(comic_info["title"])

            chapters = self._parse_chapter_groups(manga_id, raw_groups)
            if chapters:
                return chapters

            if attempt == 0 and self._has_relogin_capability():
                continue

        if manga.need_login and not (self.cfg.username or self.cfg.cookie):
            raise SourceAuthError(
                f"《{manga.title}》为再漫画需登录隐藏漫画，请在插件设置中配置再漫画账号密码或 Cookie"
            )
        raise SourceAuthError(f"《{manga.title}》章节列表为空（可能需要更高用户等级或登录权限）")

    async def fetch_chapter_pages(
        self, manga: MangaCandidate, chapter: ChapterInfo
    ) -> list[PageSpec]:
        return await self._fetch_chapter_pages_internal(manga, chapter, bust_cache=False)

    async def _fetch_chapter_pages_internal(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        *,
        bust_cache: bool,
    ) -> list[PageSpec]:
        manga_id = manga.manga_id
        chapter_id = chapter.chapter_id

        for attempt in range(2):
            force = attempt > 0
            h5_headers = await self._build_headers(platform="h5", force_refresh=force)
            params: dict[str, Any] = {"_v": "2.2.5"}
            if bust_cache:
                params["_t"] = int(time.time() * 1000)

            urls: list[str] = []
            need_reauth = False
            denied_msg = ""

            resp = await self._http.get(
                f"{ZMH_V4_API_BASE}/comic/chapter/{manga_id}/{chapter_id}",
                params=params,
                headers=h5_headers,
            )
            if resp.status == 401:
                need_reauth = True
            elif resp.status == 200:
                payload = resp.json()
                errno = payload.get("errno", 0)
                if errno == 99:
                    need_reauth = True
                else:
                    inner = ((payload.get("data") or {}).get("data")) or {}
                    can_read = inner.get("canRead", True)
                    if can_read is False:
                        need_reauth = True
                        denied_msg = "再漫画当前章节不可读（未登录或账号等级权限不足）"
                    raw_hd = inner.get("page_url_hd")
                    raw_std = inner.get("page_url")
                    chosen = raw_hd if (isinstance(raw_hd, list) and raw_hd) else raw_std
                    if isinstance(chosen, list):
                        urls = [str(u).strip() for u in chosen if str(u).strip()]

            # 若 App h5 接口未拿到图片，尝试 PC comic2/chapter/detail 兜底
            if not urls:
                pc_headers = await self._build_headers(platform="pc", force_refresh=force)
                pc_params: dict[str, Any] = {
                    "comic_id": manga_id,
                    "chapter_id": chapter_id,
                }
                if bust_cache:
                    pc_params["_t"] = int(time.time() * 1000)
                pc_resp = await self._http.get(
                    f"{ZMH_PC_API_BASE}/chapter/detail",
                    params=pc_params,
                    headers=pc_headers,
                )
                if pc_resp.status == 200:
                    pc_json = pc_resp.json()
                    if pc_json.get("errno") == 99:
                        need_reauth = True
                    ch_info = ((pc_json.get("data") or {}).get("chapterInfo")) or {}
                    pc_urls = ch_info.get("page_url_hd") or ch_info.get("page_url")
                    if isinstance(pc_urls, list):
                        urls = [str(u).strip() for u in pc_urls if str(u).strip()]

            if urls:
                img_headers = {"Referer": ZMH_REFERER}
                return [
                    PageSpec(index=i, url=u, headers=dict(img_headers)) for i, u in enumerate(urls)
                ]

            if need_reauth and attempt == 0 and self._has_relogin_capability():
                logger.info(
                    f"再漫画章节鉴权失效，正在自动重新登录并重试 ({manga.title} {chapter.title})…"
                )
                continue

            if denied_msg:
                raise SourceAuthError(denied_msg)

        raise SourceAuthError(
            f"再漫画未能获取到《{manga.title}》{chapter.title} 的图片列表（可能需要登录或提升账号等级）"
        )

    async def refresh_single_page_url(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        page_index: int,
    ) -> PageSpec | None:
        key = (manga.manga_id, chapter.chapter_id)
        try:
            async with self._refresh_pages_lock:
                now = time.monotonic()
                cached = self._refreshed_pages_cache.get(key)
                if cached is not None and (now - cached[0]) < 15.0:
                    refreshed = cached[1]
                else:
                    refreshed = await self._fetch_chapter_pages_internal(
                        manga, chapter, bust_cache=True
                    )
                    if len(self._refreshed_pages_cache) >= 8:
                        self._refreshed_pages_cache.clear()
                    self._refreshed_pages_cache[key] = (now, refreshed)
            if 0 <= page_index < len(refreshed):
                return refreshed[page_index]
        except Exception as exc:
            logger.debug(f"再漫画刷新单页签名 URL 失败 (page={page_index}): {exc}")
        return None

    async def fetch_chapter_comments(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        max_count: int = 20,
    ) -> list[ChapterComment]:
        """拉取再漫画单话章末吐槽 (`/app/v1/viewpoint/list`)。"""
        headers = await self._build_headers(platform="h5")
        resp = await self._http.get(
            f"{ZMH_V4_API_BASE}/viewpoint/list",
            params={"comicId": manga.manga_id, "chapterId": chapter.chapter_id},
            headers=headers,
            timeout_sec=10.0,
        )
        if resp.status != 200:
            return []
        payload = resp.json()
        raw_list = ((payload.get("data") or {}).get("list")) or []
        if not isinstance(raw_list, list):
            return []

        comments: list[ChapterComment] = []
        seen: set[str] = set()
        for item in raw_list:
            text = ""
            likes = 0
            author = ""
            if isinstance(item, list) and item:
                # Mihon ZaimanhuaDto.kt 规范：数组最后一项为吐槽正文，第 4 项 (index=3) 通常为点赞热度
                last_val = item[-1]
                if isinstance(last_val, str):
                    text = last_val.strip()
                if len(item) > 3 and isinstance(item[3], int):
                    likes = item[3]
            elif isinstance(item, dict):
                text = str(item.get("content") or item.get("title") or "").strip()
                likes = int(item.get("num") or item.get("like_amount") or 0)
                author = str(item.get("nickname") or "").strip()

            if not text or text in seen:
                continue
            seen.add(text)
            comments.append(ChapterComment(content=text, author=author, likes=likes))
            if len(comments) >= max_count:
                break
        return comments
