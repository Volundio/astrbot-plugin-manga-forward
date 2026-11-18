"""多漫画源优先级调度、自动降级回退与结果汇总编排器。"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .auth_manager import AuthManager
from .chapter_matcher import (
    parse_chapter_target,
    select_matching_chapter,
    select_matching_chapters_batch,
)
from .comment_renderer import CommentCardRenderer
from .config import PluginConfig, redact_secrets
from .http_client import AsyncHttpClient
from .image_downloader import ChapterImageDownloader, TempScope
from .models import (
    AllSourcesFailedError,
    ChapterComment,
    ChapterInfo,
    ChapterKind,
    ChapterNotFoundError,
    CommandUsageError,
    MangaCandidate,
    MangaForwardError,
    MangaNotFoundError,
    ParsedMangaCommand,
    ResolvedChapterBundle,
    SourceId,
)
from .sources import BaseMangaSource, CopymangaSource, KomiicSource, ZaimanhuaSource
from .sources.zmh_shadow_index import ZmhShadowSearchClient
from .text_utils import format_chapter_num

MIN_ACCEPT_MATCH_SCORE = 0.52


class BoundedTaskLimiter:
    """有界并发与等待队列背压控制器，防止极端刷屏请求无限堆积导致内存暴涨。"""

    def __init__(self, max_concurrent: int, max_waiting: int) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._max_waiting = max(1, max_waiting)
        self._active = 0
        self._waiting = 0
        self._cond = asyncio.Condition()

    def update_limits(self, max_concurrent: int, max_waiting: int) -> None:
        self._max_concurrent = max(1, max_concurrent)
        self._max_waiting = max(1, max_waiting)

    async def __aenter__(self) -> BoundedTaskLimiter:
        async with self._cond:
            if self._active >= self._max_concurrent:
                if self._waiting >= self._max_waiting:
                    raise MangaForwardError(
                        f"当前漫画解析队列繁忙（执行中 {self._active}，排队 {self._waiting}），请稍后再试"
                    )
                self._waiting += 1
                try:
                    while self._active >= self._max_concurrent:
                        await self._cond.wait()
                finally:
                    self._waiting -= 1
            self._active += 1
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        async with self._cond:
            self._active = max(0, self._active - 1)
            self._cond.notify(1)


class MangaForwardOrchestrator:
    """核心业务编排器：按自定义源优先级检索、匹配章节、保序下载图片并渲染章末吐槽卡片。"""

    def __init__(
        self,
        data_dir: Path,
        raw_config_getter: Any,
        *,
        context: Any = None,
    ) -> None:
        self._data_dir = data_dir
        self._get_raw_cfg = raw_config_getter
        self._context = context

        cfg = self.current_config()
        self._http = AsyncHttpClient(
            timeout_sec=cfg.network.request_timeout_sec,
            proxy=cfg.network.proxy,
        )
        self._auth = AuthManager(data_dir / "auth_state.json", self._http)
        self._zmh_shadow = ZmhShadowSearchClient(data_dir / "zmh_index_cache", self._http)
        self._downloader = ChapterImageDownloader(self._http)
        self._comment_renderer = CommentCardRenderer(context=context, http=self._http)

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

        self._task_limiter = BoundedTaskLimiter(
            cfg.network.max_concurrent_tasks,
            cfg.network.max_waiting_tasks,
        )

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
        self._task_limiter.update_limits(
            cfg.network.max_concurrent_tasks,
            cfg.network.max_waiting_tasks,
        )

    def _clean_root_tmp_files_sync(self) -> int:
        if not self._data_dir.exists():
            return 0
        removed = 0
        for entry in list(self._data_dir.iterdir()):
            if entry.is_file() and entry.suffix in (".tmp", ".part"):
                entry.unlink(missing_ok=True)
                removed += 1
        return removed

    async def run_maintenance(self) -> tuple[bool, int]:
        """后台周期巡检：释放长时间闲置的内存索引，清理过期索引分片与 `.tmp`/`.part` 垃圾文件。"""
        cfg = self.current_config()
        root_removed = await asyncio.to_thread(self._clean_root_tmp_files_sync)
        evicted_ram, cache_removed = await self._zmh_shadow.run_maintenance(
            idle_unload_sec=cfg.network.index_idle_unload_min * 60.0,
            disk_max_age_sec=cfg.network.cache_max_age_hours * 3600.0,
            source_enabled=cfg.zaimanhua.enabled,
        )
        return evicted_ram, root_removed + cache_removed

    async def close(self) -> None:
        cfg = self.current_config()
        try:
            await self._zmh_shadow.run_maintenance(
                idle_unload_sec=0.0,
                disk_max_age_sec=cfg.network.cache_max_age_hours * 3600.0,
                source_enabled=cfg.zaimanhua.enabled,
            )
        except Exception:
            pass
        await self._http.close()

    async def resolve_and_download(
        self,
        cmd: ParsedMangaCommand,
        temp_scope: TempScope,
        *,
        umo: str | None = None,
    ) -> ResolvedChapterBundle:
        """在全局并发保护下按源优先级依次尝试解析、下载整话图片并渲染章末吐槽图片。"""
        cfg = self.current_config()
        self._sync_runtime_limits(cfg)
        secrets = self._collect_secrets(cfg)
        target = parse_chapter_target(cmd.chapter_spec)
        sources_order = cfg.enabled_sources_in_order(cmd.forced_source)

        if not sources_order:
            raise AllSourcesFailedError([])

        async with self._task_limiter:
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
                        umo=umo,
                    )
                    return bundle
                except MangaForwardError as exc:
                    safe_reason = redact_secrets(str(exc), secrets)
                    logger.warning(
                        f"[{src_id.display_name}] 未能完成《{cmd.manga_query}》"
                        f"({cmd.chapter_spec})，准备尝试下一优先级源：{safe_reason}"
                    )
                    attempts.append((src_id, safe_reason))
                    await temp_scope.clear_subdir(src_id.value)
                    await temp_scope.clear_subdir(f"{src_id.value}_comments")
                except Exception as exc:
                    safe_reason = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
                    logger.warning(
                        f"[{src_id.display_name}] 解析异常，回退下一优先级源：{safe_reason}"
                    )
                    attempts.append((src_id, safe_reason))
                    await temp_scope.clear_subdir(src_id.value)
                    await temp_scope.clear_subdir(f"{src_id.value}_comments")

            raise AllSourcesFailedError(attempts)

    async def _download_and_render_chapter(
        self,
        *,
        source: BaseMangaSource,
        cand: MangaCandidate,
        matched_chapter: ChapterInfo,
        cfg: PluginConfig,
        temp_scope: TempScope,
        subdir_tag: str,
        umo: str | None = None,
    ) -> ResolvedChapterBundle:
        """下载单话全部图片并按需渲染章末吐槽卡片图片。"""
        page_specs = await source.fetch_chapter_pages(cand, matched_chapter)
        dest_dir = temp_scope.subdir(subdir_tag)
        want_comments = cfg.comments.enable_comments and source.source_id.supports_comments

        async def _safe_fetch_comments(
            _want: bool = want_comments,
            _cand: MangaCandidate = cand,
            _chap: ChapterInfo = matched_chapter,
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

        max_single_img_bytes = cfg.network.max_single_image_mb * 1024 * 1024
        downloaded_pages, comments = await asyncio.gather(
            self._downloader.download_chapter_pages(
                source=source,
                manga=cand,
                chapter=matched_chapter,
                specs=page_specs,
                dest_dir=dest_dir,
                concurrency=cfg.network.download_concurrency,
                max_single_image_bytes=max_single_img_bytes,
                max_chapter_bytes=cfg.network.max_chapter_mb * 1024 * 1024,
            ),
            _safe_fetch_comments(),
        )

        comment_image_paths: list[Path] = []
        if comments:
            try:
                comments_dir = temp_scope.subdir(f"{subdir_tag}_comments")
                comment_image_paths = await self._comment_renderer.render_comment_pages(
                    source=source.source_id,
                    manga=cand,
                    chapter=matched_chapter,
                    comments=comments,
                    comments_cfg=cfg.comments,
                    dest_dir=comments_dir,
                    umo=umo,
                    max_image_bytes=max_single_img_bytes,
                )
            except Exception as r_exc:
                logger.warning(
                    f"[{source.source_id.display_name}] 远程渲染章末吐槽图片失败 "
                    f"({cand.title} {matched_chapter.title}): {r_exc}"
                )
                comment_image_paths = []
                if not cfg.comments.fallback_to_text:
                    comments = []

        return ResolvedChapterBundle(
            source=source.source_id,
            manga=cand,
            chapter=matched_chapter,
            downloaded_pages=downloaded_pages,
            comments=comments,
            comment_image_paths=comment_image_paths,
        )

    async def iter_resolved_chapters(
        self,
        cmd: ParsedMangaCommand,
        temp_scope: TempScope,
        *,
        umo: str | None = None,
    ) -> AsyncIterator[tuple[int, int, ResolvedChapterBundle]]:
        """支持单话与批量多话的流式逐话解析生成器：每完成一话立即投递并释放本地文件，零内存/磁盘堆积。"""
        cfg = self.current_config()
        self._sync_runtime_limits(cfg)
        secrets = self._collect_secrets(cfg)
        probe_target = parse_chapter_target(cmd.chapter_spec)
        sources_order = cfg.enabled_sources_in_order(cmd.forced_source)

        if not sources_order:
            raise AllSourcesFailedError([])

        async with self._task_limiter:
            attempts: list[tuple[SourceId, str]] = []
            selected_source: BaseMangaSource | None = None
            selected_cand: MangaCandidate | None = None
            selected_chapters: list[ChapterInfo] = []
            first_bundle: ResolvedChapterBundle | None = None
            first_tag = ""

            for src_id in sources_order:
                source = self._sources[src_id]
                try:
                    if cmd.explicit_manga_id and cmd.forced_source == source.source_id:
                        candidates = [await source.get_manga_by_id(cmd.explicit_manga_id)]
                    else:
                        raw_cands = await source.search_manga(cmd.manga_query)
                        candidates = [
                            c for c in raw_cands if c.match_score >= MIN_ACCEPT_MATCH_SCORE
                        ]
                        if not candidates:
                            top_hint = (
                                f"（最接近词条：《{raw_cands[0].title}》）" if raw_cands else ""
                            )
                            raise MangaNotFoundError(
                                f"未找到匹配的漫画「{cmd.manga_query}」{top_hint}"
                            )

                    max_try = 3 if probe_target.part_number is not None else 2
                    score_gap_limit = 0.18 if probe_target.part_number is not None else 0.12
                    cand_err: Exception | None = None

                    for idx_c, cand in enumerate(candidates[:max_try]):
                        tag_1 = f"{source.source_id.value}_ch1"
                        try:
                            all_chapters = await source.fetch_chapters(cand)
                            matched_list = select_matching_chapters_batch(
                                all_chapters,
                                cmd.chapter_spec,
                                manga_title=cand.title,
                                max_chapters=cfg.delivery.max_batch_chapters,
                            )
                            b1 = await self._download_and_render_chapter(
                                source=source,
                                cand=cand,
                                matched_chapter=matched_list[0],
                                cfg=cfg,
                                temp_scope=temp_scope,
                                subdir_tag=tag_1,
                                umo=umo,
                            )
                            selected_source = source
                            selected_cand = cand
                            selected_chapters = matched_list
                            first_bundle = b1
                            first_tag = tag_1
                            cand_err = None
                            break
                        except CommandUsageError:
                            raise
                        except ChapterNotFoundError as exc:
                            cand_err = exc
                        except Exception as exc:
                            cand_err = exc
                            await temp_scope.clear_subdir(tag_1)
                            await temp_scope.clear_subdir(f"{tag_1}_comments")

                        next_idx = idx_c + 1
                        if (
                            next_idx >= len(candidates[:max_try])
                            or (cand.match_score - candidates[next_idx].match_score)
                            > score_gap_limit
                        ):
                            break

                    if first_bundle is not None:
                        break
                    assert cand_err is not None
                    raise cand_err
                except CommandUsageError:
                    raise
                except MangaForwardError as exc:
                    safe_reason = redact_secrets(str(exc), secrets)
                    logger.warning(
                        f"[{src_id.display_name}] 未能完成《{cmd.manga_query}》"
                        f"({cmd.chapter_spec})，准备尝试下一优先级源：{safe_reason}"
                    )
                    attempts.append((src_id, safe_reason))
                except Exception as exc:
                    safe_reason = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
                    logger.warning(
                        f"[{src_id.display_name}] 解析异常，回退下一优先级源：{safe_reason}"
                    )
                    attempts.append((src_id, safe_reason))

            if first_bundle is None or selected_source is None or selected_cand is None:
                raise AllSourcesFailedError(attempts)

            total_chaps = len(selected_chapters)
            try:
                yield 1, total_chaps, first_bundle
            finally:
                await temp_scope.clear_subdir(first_tag)
                await temp_scope.clear_subdir(f"{first_tag}_comments")

            # 逐话流式处理后续第 2..K 话，每完成一话即发即删
            for c_idx in range(2, total_chaps + 1):
                chap = selected_chapters[c_idx - 1]
                tag_k = f"{selected_source.source_id.value}_ch{c_idx}"
                bundle_k: ResolvedChapterBundle | None = None
                try:
                    bundle_k = await self._download_and_render_chapter(
                        source=selected_source,
                        cand=selected_cand,
                        matched_chapter=chap,
                        cfg=cfg,
                        temp_scope=temp_scope,
                        subdir_tag=tag_k,
                        umo=umo,
                    )
                except Exception as prim_exc:
                    await temp_scope.clear_subdir(tag_k)
                    await temp_scope.clear_subdir(f"{tag_k}_comments")
                    safe_p = redact_secrets(str(prim_exc), secrets)
                    logger.warning(
                        f"[{selected_source.source_id.display_name}] 批量第 {c_idx}/{total_chaps} 话 "
                        f"({chap.title}) 下载失败，尝试跨源回退补偿：{safe_p}"
                    )
                    if chap.number is not None and chap.sub_order == 0:
                        unit = "卷" if chap.kind == ChapterKind.VOLUME else "话"
                        num_str = format_chapter_num(chap.number)
                        sub_spec = (
                            f"第{chap.part_number}部 第{num_str}{unit}"
                            if chap.part_number
                            else f"第{num_str}{unit}"
                        )
                    else:
                        sub_spec = chap.title
                    sub_target = parse_chapter_target(sub_spec)
                    for fb_sid in sources_order:
                        if fb_sid == selected_source.source_id:
                            continue
                        fb_src = self._sources[fb_sid]
                        fb_tag = f"{fb_sid.value}_ch{c_idx}"
                        try:
                            fb_cmd = ParsedMangaCommand(
                                manga_query=cmd.manga_query,
                                chapter_spec=sub_spec,
                            )
                            bundle_k = await self._try_single_source(
                                source=fb_src,
                                cmd=fb_cmd,
                                target=sub_target,
                                cfg=cfg,
                                temp_scope=temp_scope,
                                umo=umo,
                                subdir_tag=fb_tag,
                            )
                            tag_k = fb_tag
                            break
                        except Exception:
                            await temp_scope.clear_subdir(fb_tag)
                            await temp_scope.clear_subdir(f"{fb_tag}_comments")
                    if bundle_k is None:
                        raise prim_exc

                try:
                    yield c_idx, total_chaps, bundle_k
                finally:
                    await temp_scope.clear_subdir(tag_k)
                    await temp_scope.clear_subdir(f"{tag_k}_comments")

    async def _try_single_source(
        self,
        *,
        source: BaseMangaSource,
        cmd: ParsedMangaCommand,
        target: Any,
        cfg: PluginConfig,
        temp_scope: TempScope,
        umo: str | None = None,
        subdir_tag: str | None = None,
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

        # 若用户指定了第X部/章（如 "第二章 13话"），兼容“同词条分部”与“独立第二部词条”两种源站收录方式
        max_try = 3 if target.part_number is not None else 2
        score_gap_limit = 0.18 if target.part_number is not None else 0.12
        eff_tag = subdir_tag or source.source_id.value

        last_err: Exception | None = None
        for idx_c, cand in enumerate(candidates[:max_try]):
            try:
                chapters = await source.fetch_chapters(cand)
                matched_chapter = select_matching_chapter(chapters, target, manga_title=cand.title)
                return await self._download_and_render_chapter(
                    source=source,
                    cand=cand,
                    matched_chapter=matched_chapter,
                    cfg=cfg,
                    temp_scope=temp_scope,
                    subdir_tag=eff_tag,
                    umo=umo,
                )
            except ChapterNotFoundError as exc:
                last_err = exc
                next_idx = idx_c + 1
                if (
                    next_idx >= len(candidates[:max_try])
                    or (cand.match_score - candidates[next_idx].match_score) > score_gap_limit
                ):
                    break
            except Exception as exc:
                last_err = exc
                await temp_scope.clear_subdir(eff_tag)
                await temp_scope.clear_subdir(f"{eff_tag}_comments")
                next_idx = idx_c + 1
                if (
                    next_idx >= len(candidates[:max_try])
                    or (cand.match_score - candidates[next_idx].match_score) > score_gap_limit
                ):
                    break

        assert last_err is not None
        raise last_err
