"""章节图片有界并发保序下载器与请求级临时磁盘作用域管理。"""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from pathlib import Path

from astrbot.api import logger

from .http_client import AsyncHttpClient
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
MAX_CHAPTER_TOTAL_BYTES = 220 * 1024 * 1024


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


class TempScope:
    """单次指令执行专属的临时落盘目录，发送结束后整目录原子清理。"""

    def __init__(self, path: Path) -> None:
        self.path = path
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
        if self.path.exists():
            await asyncio.to_thread(shutil.rmtree, self.path, True)


class TempDirManager:
    """管理 `temp/` 根目录分配与残留过期目录后台巡检回收。"""

    def __init__(self, base_dir: Path, *, ttl_sec: float = 600.0) -> None:
        self._base_dir = base_dir
        self._ttl_sec = ttl_sec

    def create_scope(self) -> TempScope:
        scope_dir = self._base_dir / f"req_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        scope_dir.mkdir(parents=True, exist_ok=True)
        return TempScope(scope_dir)

    async def cleanup_expired(self) -> int:
        return await asyncio.to_thread(self._cleanup_expired_sync)

    def _cleanup_expired_sync(self) -> int:
        if not self._base_dir.exists():
            return 0
        now = time.time()
        removed = 0
        for child in self._base_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                mtime = child.stat().st_mtime
                if (now - mtime) >= self._ttl_sec:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except Exception:
                pass
        return removed


class ChapterImageDownloader:
    """并发下载整话漫画图片，严格保证输出列表顺序为 `0..N-1`。"""

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
        concurrency: int = 6,
    ) -> list[DownloadedPage]:
        if not specs:
            raise SourceAuthError("章节图片规格列表为空")

        dest_dir.mkdir(parents=True, exist_ok=True)
        # 按 index 升序规范化输入，分配固定长度数组保证并发结果严格对位
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
                for attempt in range(3):
                    if abort_exc is not None:
                        return
                    try:
                        resp = await self._http.get(
                            cur_spec.url,
                            headers=cur_spec.headers,
                            max_bytes=MAX_SINGLE_IMAGE_BYTES,
                        )
                    except Exception as exc:
                        last_err = str(exc)
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    # Komiic HTTP 402：今日图片免费读取额度已达上限
                    if resp.status == 402:
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

                    # 401 / 403：防盗链签名过期或 Cookie 失效，尝试刷新一次 URL/凭证
                    if resp.status in (401, 403) and attempt == 0:
                        new_spec = await source.refresh_single_page_url(manga, chapter, slot_idx)
                        if new_spec is not None:
                            cur_spec = new_spec
                            continue

                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}"
                        if attempt < 2:
                            await asyncio.sleep(0.5 * (attempt + 1))
                        continue

                    ext = detect_image_extension(resp.body)
                    if ext is None:
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

                    if total_bytes + len(resp.body) > MAX_CHAPTER_TOTAL_BYTES:
                        last_err = "超出单话累计体积安全上限"
                        break

                    file_path = dest_dir / f"{slot_idx + 1:04d}{ext}"
                    await asyncio.to_thread(file_path.write_bytes, resp.body)
                    total_bytes += len(resp.body)
                    slots[slot_idx] = DownloadedPage(
                        index=slot_idx,
                        local_path=file_path,
                        size_bytes=len(resp.body),
                    )
                    return

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
