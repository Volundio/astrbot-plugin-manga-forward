"""多漫画源优先级调度、自动降级回退与结果汇总编排器。"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .auth_manager import AuthManager
from .chapter_matcher import parse_chapter_target, select_matching_chapter
from .config import PluginConfig, redact_secrets
from .http_client import AsyncHttpClient
from .image_downloader import ChapterImageDownloader, TempScope
from .models import (
    AllSourcesFailedError,
    ChapterComment,
    ChapterNotFoundError,
    MangaForwardError,
    MangaNotFoundError,
    ParsedMangaCommand,
    ResolvedChapterBundle,
    SourceId,
)
from .sources import BaseMangaSource, CopymangaSource, KomiicSource, ZaimanhuaSource
from .sources.zmh_shadow_index import ZmhShadowSearchClient

MIN_ACCEPT_MATCH_SCORE = 0.52


class MangaForwardOrchestrator:
    """核心业务编排器：按自定义源优先级检索、匹配章节、保序下载图片并拉取章末吐槽。"""

    def __init__(self, data_dir: Path, raw_config_getter: Any) -> None:
        self._data_dir = data_dir
        self._get_raw_cfg = raw_config_getter

        cfg = self.current_config()
        self._http = AsyncHttpClient(
            timeout_sec=cfg.network.request_timeout_sec,
            proxy=cfg.network.proxy,
        )
        self._auth = AuthManager(data_dir / "auth_state.json", self._http)
        self._zmh_shadow = ZmhShadowSearchClient(data_dir / "zmh_index_cache", self._http)
        self._downloader = ChapterImageDownloader(self._http)

        self._sources: dict[SourceId, BaseMangaSource] = {
            SourceId.ZAIMANHUA: ZaimanhuaSource(
                lambda: self.current_config().zaimanhua,
                self._http,
                self._auth,
                self._zmh_shadow,
            ),
            SourceId.COPYMANGA: CopymangaSource(
                lambda: self.current_config().copymanga,
                self._http,
            ),
            SourceId.KOMIIC: KomiicSource(
                lambda: self.current_config().komiic,
                self._http,
                self._auth,
            ),
        }

        self._max_tasks = cfg.network.max_concurrent_tasks
        self._task_sem = asyncio.Semaphore(self._max_tasks)

    def current_config(self) -> PluginConfig:
        raw = self._get_raw_cfg()
        return PluginConfig.from_mapping(raw if isinstance(raw, dict) else None)

    def _collect_secrets(self, cfg: PluginConfig) -> list[str]:
        return [
            cfg.zaimanhua.password,
            cfg.zaimanhua.cookie,
            cfg.copymanga.token,
            cfg.komiic.password,
            cfg.komiic.cookie,
        ]

    def _sync_runtime_limits(self, cfg: PluginConfig) -> None:
        self._http.update_settings(
            timeout_sec=cfg.network.request_timeout_sec,
            proxy=cfg.network.proxy,
        )
        if cfg.network.max_concurrent_tasks != self._max_tasks:
            self._max_tasks = cfg.network.max_concurrent_tasks
            self._task_sem = asyncio.Semaphore(self._max_tasks)

    async def close(self) -> None:
        await self._http.close()

    async def resolve_and_download(
        self,
        cmd: ParsedMangaCommand,
        temp_scope: TempScope,
    ) -> ResolvedChapterBundle:
        """在全局并发保护下按源优先级依次尝试解析并下载整话漫画。"""
        cfg = self.current_config()
        self._sync_runtime_limits(cfg)
        secrets = self._collect_secrets(cfg)
        target = parse_chapter_target(cmd.chapter_spec)
        sources_order = cfg.enabled_sources_in_order(cmd.forced_source)

        if not sources_order:
            raise AllSourcesFailedError([])

        async with self._task_sem:
            attempts: list[tuple[SourceId, str]] = []
            for src_id in sources_order:
                source = self._sources[src_id]
                try:
                    bundle = await self._try_single_source(
                        source=source,
                        cmd=cmd,
                        target=target,
                        cfg=cfg,
                        temp_scope=temp_scope,
                    )
                    return bundle
                except MangaForwardError as exc:
                    safe_reason = redact_secrets(str(exc), secrets)
                    logger.info(
                        f"[{src_id.display_name}] 未能完成《{cmd.manga_query}》"
                        f"({cmd.chapter_spec})，准备尝试下一优先级源：{safe_reason}"
                    )
                    attempts.append((src_id, safe_reason))
                    await temp_scope.clear_subdir(src_id.value)
                except Exception as exc:
                    safe_reason = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
                    logger.warning(
                        f"[{src_id.display_name}] 解析异常，回退下一优先级源：{safe_reason}"
                    )
                    attempts.append((src_id, safe_reason))
                    await temp_scope.clear_subdir(src_id.value)

            raise AllSourcesFailedError(attempts)

    async def _try_single_source(
        self,
        *,
        source: BaseMangaSource,
        cmd: ParsedMangaCommand,
        target: Any,
        cfg: PluginConfig,
        temp_scope: TempScope,
    ) -> ResolvedChapterBundle:
        # 1. 获取候选漫画（支持显式 ID 直达或关键词检索）
        if cmd.explicit_manga_id and cmd.forced_source == source.source_id:
            candidates = [await source.get_manga_by_id(cmd.explicit_manga_id)]
        else:
            raw_candidates = await source.search_manga(cmd.manga_query)
            candidates = [c for c in raw_candidates if c.match_score >= MIN_ACCEPT_MATCH_SCORE]
            if not candidates:
                top_hint = (
                    f"（最接近词条：《{raw_candidates[0].title}》）" if raw_candidates else ""
                )
                raise MangaNotFoundError(f"未找到匹配的漫画「{cmd.manga_query}」{top_hint}")

        # 尝试匹配度最高的前 2 个候选（防止源站存在同名空壳词条与连载正篇词条并存）
        last_err: Exception | None = None
        for cand in candidates[:2]:
            try:
                chapters = await source.fetch_chapters(cand)
                matched_chapter = select_matching_chapter(chapters, target, manga_title=cand.title)
                page_specs = await source.fetch_chapter_pages(cand, matched_chapter)

                dest_dir = temp_scope.subdir(source.source_id.value)
                # 并发下载图片 + 并发拉取单话评论/章末吐槽（仅再漫画与拷贝漫画）
                want_comments = cfg.comments.enable_comments and source.source_id.supports_comments

                async def _safe_fetch_comments(
                    _want: bool = want_comments,
                    _cand: Any = cand,
                    _chap: Any = matched_chapter,
                ) -> list[ChapterComment]:
                    if not _want:
                        return []
                    try:
                        return await source.fetch_chapter_comments(
                            _cand,
                            _chap,
                            max_count=cfg.comments.max_comments,
                        )
                    except Exception as c_exc:
                        logger.warning(
                            f"[{source.source_id.display_name}] 拉取章末吐槽失败，已忽略 "
                            f"({_cand.title} {_chap.title}): {c_exc}"
                        )
                        return []

                downloaded_pages, comments = await asyncio.gather(
                    self._downloader.download_chapter_pages(
                        source=source,
                        manga=cand,
                        chapter=matched_chapter,
                        specs=page_specs,
                        dest_dir=dest_dir,
                        concurrency=cfg.network.download_concurrency,
                    ),
                    _safe_fetch_comments(),
                )

                return ResolvedChapterBundle(
                    source=source.source_id,
                    manga=cand,
                    chapter=matched_chapter,
                    downloaded_pages=downloaded_pages,
                    comments=comments,
                )
            except ChapterNotFoundError as exc:
                last_err = exc
                # 若第 2 个候选标题匹配分显著低于第 1 个，不再盲目尝试无关衍生作
                if len(candidates) < 2 or (cand.match_score - candidates[1].match_score) > 0.12:
                    break
            except Exception as exc:
                last_err = exc
                await temp_scope.clear_subdir(source.source_id.value)
                if len(candidates) < 2 or (cand.match_score - candidates[1].match_score) > 0.12:
                    break

        assert last_err is not None
        raise last_err
