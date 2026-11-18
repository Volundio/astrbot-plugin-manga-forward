"""章节图片流式有界并发保序下载器与防泄漏临时磁盘作用域管理。"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .http_client import AsyncHttpClient, HttpStreamResult
from .models import (
    ChapterInfo,
    DownloadedPage,
    MangaCandidate,
    PageSpec,
    SourceAuthError,
    SourceId,
    SourceQuotaError,
)
from .sources.base import BaseMangaSource

MAX_SINGLE_IMAGE_BYTES = 18 * 1024 * 1024
DEFAULT_MAX_CHAPTER_BYTES = 150 * 1024 * 1024
MAX_TOTAL_TEMP_DIR_BYTES = 320 * 1024 * 1024


def detect_image_extension(data: bytes) -> str | None:
    """校验二进制魔数并识别真实图片扩展名，拦截伪装成图片的 HTML/JSON 错误页。"""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[4:12] in (b"ftypavif", b"ftypavis"):
        return ".avif"
    return None


def _dir_size_sync(path: Path) -> int:
    total = 0
    try:
        for root, _, files in os.walk(path):
            for fname in files:
                try:
                    total += os.path.getsize(os.path.join(root, fname))
                except OSError:
                    pass
    except OSError:
        pass
    return total


class TempScope:
    """单次指令执行专属的临时落盘目录，活跃态受保护、结束即整目录原子清理。"""

    def __init__(
        self,
        path: Path,
        on_release: Callable[[Path], None] | None = None,
    ) -> None:
        self.path = path
        self._on_release = on_release
        self._cleaned = False

    def subdir(self, name: str) -> Path:
        sub = self.path / name
        sub.mkdir(parents=True, exist_ok=True)
        return sub

    async def clear_subdir(self, name: str) -> None:
        sub = self.path / name
        if sub.exists():
            await asyncio.to_thread(shutil.rmtree, sub, True)

    async def cleanup(self) -> None:
        if self._cleaned:
            return
        self._cleaned = True
        if self._on_release is not None:
            self._on_release(self.path)
        if self.path.exists():
            await asyncio.to_thread(shutil.rmtree, self.path, True)


class TempDirManager:
    """管理 `temp/` 根目录分配、启动时崩溃残留清扫、活跃作用域保护及总容量上限驱逐。"""

    def __init__(
        self,
        base_dir: Path,
        *,
        ttl_sec: float = 180.0,
        max_total_bytes: int = MAX_TOTAL_TEMP_DIR_BYTES,
    ) -> None:
        self._base_dir = base_dir
        self._ttl_sec = ttl_sec
        self._max_total_bytes = max_total_bytes
        self._active_scopes: set[Path] = set()

    def _release_scope(self, path: Path) -> None:
        self._active_scopes.discard(path)

    def create_scope(self) -> TempScope:
        scope_dir = (self._base_dir / f"req_{int(time.time())}_{uuid.uuid4().hex[:8]}").resolve()
        scope_dir.mkdir(parents=True, exist_ok=True)
        self._active_scopes.add(scope_dir)
        return TempScope(scope_dir, on_release=self._release_scope)

    async def cleanup_expired(self, *, force_all_inactive: bool = False) -> int:
        """清理过期或异常残留的临时目录与 `.part`/`.tmp` 垃圾文件（严格跳过活跃中的任务作用域）。"""
        active_snapshot = set(self._active_scopes)
        return await asyncio.to_thread(self._cleanup_sync, active_snapshot, force_all_inactive)

    def _cleanup_sync(
        self,
        active_paths: set[Path],
        force_all_inactive: bool,
    ) -> int:
        if not self._base_dir.exists():
            return 0
        now = time.time()
        removed = 0
        surviving_inactive: list[tuple[float, int, Path]] = []

        for child in list(self._base_dir.iterdir()):
            try:
                resolved = child.resolve()
            except OSError:
                continue

            if resolved in active_paths:
                continue

            if child.is_file():
                child.unlink(missing_ok=True)
                removed += 1
                continue

            if not child.is_dir():
                continue

            try:
                mtime = child.stat().st_mtime
            except OSError:
                continue

            if force_all_inactive or (now - mtime) >= self._ttl_sec:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
            else:
                surviving_inactive.append((mtime, _dir_size_sync(child), child))

        # 总磁盘预算驱逐：若非活跃残留目录累计体积超限，按最旧优先回收
        total_inactive_bytes = sum(sz for _, sz, _ in surviving_inactive)
        if total_inactive_bytes > self._max_total_bytes:
            surviving_inactive.sort(key=lambda item: item[0])
            for _, sz, dir_path in surviving_inactive:
                if total_inactive_bytes <= self._max_total_bytes:
                    break
                shutil.rmtree(dir_path, ignore_errors=True)
                total_inactive_bytes -= sz
                removed += 1

        # 若 temp/ 根目录已完全为空，移除空目录保持整洁
        try:
            if not any(self._base_dir.iterdir()):
                self._base_dir.rmdir()
        except OSError:
            pass
        return removed


async def _stream_or_fallback_get(
    http: Any,
    url: str,
    dest_path: Path,
    *,
    headers: dict[str, str],
    max_bytes: int,
) -> HttpStreamResult:
    """优先调用 `stream_to_file` 64KB 流式落盘；兼容单元测试中仅实现 `.get()` 的轻量桩对象。"""
    stream_fn = getattr(http, "stream_to_file", None)
    if callable(stream_fn):
        return await stream_fn(
            "GET",
            url,
            dest_path,
            headers=headers,
            max_bytes=max_bytes,
        )

    resp = await http.get(url, headers=headers, max_bytes=max_bytes)
    if resp.status != 200:
        return HttpStreamResult(
            status=resp.status,
            headers=resp.headers,
            size_bytes=0,
            head_bytes=b"",
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    await asyncio.to_thread(dest_path.write_bytes, resp.body)
    return HttpStreamResult(
        status=200,
        headers=resp.headers,
        size_bytes=len(resp.body),
        head_bytes=resp.body[:32],
    )


class ChapterImageDownloader:
    """流式并发下载整话漫画图片，严格保证输出列表顺序为 `0..N-1` 且内存占用恒定极低。"""

    def __init__(self, http: AsyncHttpClient) -> None:
        self._http = http

    async def download_chapter_pages(
        self,
        *,
        source: BaseMangaSource,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        specs: list[PageSpec],
        dest_dir: Path,
        concurrency: int = 5,
        max_chapter_bytes: int = DEFAULT_MAX_CHAPTER_BYTES,
    ) -> list[DownloadedPage]:
        if not specs:
            raise SourceAuthError("章节图片规格列表为空")

        dest_dir.mkdir(parents=True, exist_ok=True)
        ordered_specs = sorted(specs, key=lambda s: s.index)
        total_pages = len(ordered_specs)
        slots: list[DownloadedPage | None] = [None] * total_pages

        sem = asyncio.Semaphore(max(1, min(16, concurrency)))
        refresh_lock = asyncio.Lock()
        refreshed_once = False
        total_bytes = 0
        abort_exc: Exception | None = None

        async def _download_one(slot_idx: int, spec: PageSpec) -> None:
            nonlocal refreshed_once, total_bytes, abort_exc
            if abort_exc is not None:
                return

            async with sem:
                if abort_exc is not None:
                    return

                cur_spec = spec
                last_err = ""
                raw_dest = dest_dir / f"{slot_idx + 1:04d}.raw"

                for attempt in range(3):
                    if abort_exc is not None:
                        return
                    remaining_budget = max_chapter_bytes - total_bytes
                    if remaining_budget <= 0:
                        last_err = "超出单话累计体积安全上限"
                        break

                    per_file_cap = min(MAX_SINGLE_IMAGE_BYTES, remaining_budget)
                    try:
                        st_res = await _stream_or_fallback_get(
                            self._http,
                            cur_spec.url,
                            raw_dest,
                            headers=cur_spec.headers,
                            max_bytes=per_file_cap,
                        )
                    except Exception as exc:
                        raw_dest.unlink(missing_ok=True)
                        last_err = str(exc)
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    if st_res.status == 402:
                        raw_dest.unlink(missing_ok=True)
                        async with refresh_lock:
                            if not refreshed_once:
                                refreshed_once = True
                                new_spec = await source.refresh_single_page_url(
                                    manga, chapter, slot_idx
                                )
                                if new_spec is not None:
                                    cur_spec = new_spec
                                    continue
                        abort_exc = SourceQuotaError(
                            "Komiic 今日免费图片读取次数已达上限，请配置有效登录账号/Cookie 或明日再试"
                        )
                        return

                    if st_res.status in (401, 403) and attempt == 0:
                        raw_dest.unlink(missing_ok=True)
                        new_spec = await source.refresh_single_page_url(manga, chapter, slot_idx)
                        if new_spec is not None:
                            cur_spec = new_spec
                            continue

                    if st_res.status != 200:
                        raw_dest.unlink(missing_ok=True)
                        last_err = f"HTTP {st_res.status}"
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    ext = detect_image_extension(st_res.head_bytes)
                    if ext is None:
                        raw_dest.unlink(missing_ok=True)
                        last_err = "响应内容非有效图片格式"
                        if attempt == 0:
                            new_spec = await source.refresh_single_page_url(
                                manga, chapter, slot_idx
                            )
                            if new_spec is not None:
                                cur_spec = new_spec
                                continue
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    final_path = dest_dir / f"{slot_idx + 1:04d}{ext}"
                    await asyncio.to_thread(os.replace, raw_dest, final_path)
                    total_bytes += st_res.size_bytes
                    slots[slot_idx] = DownloadedPage(
                        index=slot_idx,
                        local_path=final_path,
                        size_bytes=st_res.size_bytes,
                    )
                    return

                raw_dest.unlink(missing_ok=True)
                logger.warning(
                    f"[{source.source_id.display_name}] 第 {slot_idx + 1}/{total_pages} 页下载失败 "
                    f"({manga.title} {chapter.title}): {last_err}"
                )
                slots[slot_idx] = DownloadedPage(
                    index=slot_idx,
                    local_path=None,
                    error=last_err or "下载失败",
                )

        tasks = [
            asyncio.create_task(_download_one(slot_i, sp))
            for slot_i, sp in enumerate(ordered_specs)
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        if abort_exc is not None:
            raise abort_exc

        final_pages: list[DownloadedPage] = [
            p if p is not None else DownloadedPage(index=i, local_path=None, error="未完成")
            for i, p in enumerate(slots)
        ]
        ok_count = sum(1 for p in final_pages if p.local_path is not None)
        if ok_count == 0:
            first_err = final_pages[0].error if final_pages else "未知错误"
            if source.source_id == SourceId.KOMIIC and "402" in first_err:
                raise SourceQuotaError("Komiic 今日图片读取额度已满")
            raise SourceAuthError(f"整话共 {total_pages} 页图片全部下载失败（{first_err}）")

        return final_pages
