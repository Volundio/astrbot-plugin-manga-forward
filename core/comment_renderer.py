"""章末吐槽远程 HTML 渲染模块（调用 AstrBot `remote` / `astrbot-t2i-service`，默认 20 条一页，流式落盘零残留）。"""

from __future__ import annotations

import asyncio
import inspect
import os
import shutil
from pathlib import Path
from typing import Any

from astrbot.api import logger

from .config import CommentsConfig
from .http_client import AsyncHttpClient
from .image_downloader import detect_image_extension
from .models import ChapterComment, ChapterInfo, MangaCandidate, SourceId

TEMPLATE_PATH = Path(__file__).resolve().parent / "templates" / "comments_card.html"
ASTRBOT_T2I_ENDPOINTS_DISCOVERY_URL = "https://api.soulter.top/astrbot/t2i-endpoints"
DEFAULT_ASTRBOT_T2I_ENDPOINT = "https://t2i.soulter.top/text2img"
COMMENT_CARD_VIEWPORT_WIDTH = 720
DEFAULT_MAX_RENDERED_COMMENT_IMAGE_BYTES = 18 * 1024 * 1024


def load_comments_template() -> str:
    return TEMPLATE_PATH.read_text(encoding="utf-8")


def _read_head_sync(path: Path, n: int = 32) -> bytes:
    with open(path, "rb") as f:
        return f.read(n)


def normalize_t2i_endpoint(url: str) -> str:
    """将用户配置的远程 T2I 地址规范化为以 `/text2img` 结尾的基础路径。"""
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    if not cleaned.startswith(("http://", "https://")):
        cleaned = f"http://{cleaned}"
    if cleaned.endswith("/text2img/generate"):
        cleaned = cleaned.removesuffix("/generate")
    elif not cleaned.endswith("/text2img"):
        cleaned = f"{cleaned}/text2img"
    return cleaned


def paginate_comments(
    comments: list[ChapterComment],
    per_page: int = 20,
) -> list[list[ChapterComment]]:
    """按固定单页条数（默认 20 条一页）切分章末吐槽列表。"""
    step = max(1, int(per_page))
    return [comments[i : i + step] for i in range(0, len(comments), step)]


class CommentCardRenderer:
    """调用 AstrBot 远程 HTML 渲染服务将章末吐槽按 20 条/页流式渲染为图片。"""

    def __init__(self, *, context: Any, http: AsyncHttpClient) -> None:
        self._context = context
        self._http = http
        self._tmpl_str = load_comments_template()
        self._discovered_endpoint: str | None = None

    def resolve_effective_endpoint(
        self,
        plugin_t2i_endpoint: str = "",
        umo: str | None = None,
    ) -> str | None:
        """优先使用插件设置中的 `comments.t2i_endpoint`；留空时读取本机 AstrBot 全局配置的 `t2i_endpoint`。"""
        if plugin_t2i_endpoint and plugin_t2i_endpoint.strip():
            return normalize_t2i_endpoint(plugin_t2i_endpoint)

        if self._context is not None:
            get_cfg = getattr(self._context, "get_config", None)
            if callable(get_cfg):
                try:
                    ab_cfg = get_cfg(umo)
                except TypeError:
                    try:
                        ab_cfg = get_cfg()
                    except Exception:
                        ab_cfg = None
                except Exception:
                    ab_cfg = None

                if ab_cfg is not None and callable(getattr(ab_cfg, "get", None)):
                    ep = str(ab_cfg.get("t2i_endpoint") or "").strip()
                    if ep:
                        return normalize_t2i_endpoint(ep)
        return None

    async def _discover_official_endpoint(self) -> str:
        if self._discovered_endpoint:
            return self._discovered_endpoint
        try:
            resp = await self._http.get(
                ASTRBOT_T2I_ENDPOINTS_DISCOVERY_URL,
                timeout_sec=10.0,
            )
            if resp.status == 200:
                payload = resp.json()
                for item in payload.get("data") or []:
                    if isinstance(item, dict) and item.get("active") and item.get("url"):
                        ep = normalize_t2i_endpoint(str(item["url"]))
                        if ep:
                            self._discovered_endpoint = ep
                            return ep
        except Exception as exc:
            logger.debug(f"拉取官方远程 T2I 节点列表失败：{exc}")
        return DEFAULT_ASTRBOT_T2I_ENDPOINT

    async def render_comment_pages(
        self,
        *,
        source: SourceId,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        comments: list[ChapterComment],
        comments_cfg: CommentsConfig,
        dest_dir: Path,
        umo: str | None = None,
        max_image_bytes: int = DEFAULT_MAX_RENDERED_COMMENT_IMAGE_BYTES,
    ) -> list[Path]:
        """将单话评论/章末吐槽按每页 `comments_per_page` 条渲染为多张卡片图片。"""
        if not comments:
            return []

        per_page = max(1, comments_cfg.comments_per_page)
        pages = paginate_comments(comments, per_page=per_page)
        total_pages = len(pages)
        total_comments = len(comments)
        dest_dir.mkdir(parents=True, exist_ok=True)
        single_cap = max(64 * 1024, max_image_bytes)

        effective_endpoint = self.resolve_effective_endpoint(comments_cfg.t2i_endpoint, umo=umo)
        screenshot_options: dict[str, Any] = {
            "full_page": True,
            "type": "jpeg",
            "quality": 90,
            "viewport_width": COMMENT_CARD_VIEWPORT_WIDTH,
            "viewport_height": 120,
            "device_scale_factor_level": "high",
            "timeout": 30000,
        }

        rendered_paths: list[Path] = []
        for page_idx, page_items in enumerate(pages, start=1):
            start_rank = (page_idx - 1) * per_page + 1
            end_rank = start_rank + len(page_items) - 1
            tmpl_items = [
                {
                    "rank": start_rank + offset,
                    "rank_label": f"{start_rank + offset:02d}",
                    "author": item.author,
                    "content": item.content,
                    "likes": item.likes,
                }
                for offset, item in enumerate(page_items)
            ]
            tmpl_data: dict[str, Any] = {
                "viewport_width": COMMENT_CARD_VIEWPORT_WIDTH,
                "theme": comments_cfg.theme,
                "source_name": source.display_name,
                "manga_title": manga.title,
                "chapter_title": chapter.title,
                "page_no": page_idx,
                "total_pages": total_pages,
                "total_comments": total_comments,
                "range_start": start_rank,
                "range_end": end_rank,
                "items": tmpl_items,
            }

            out_file = await self._render_single_page(
                tmpl_data=tmpl_data,
                options=screenshot_options,
                effective_endpoint=effective_endpoint,
                dest_dir=dest_dir,
                page_idx=page_idx,
                max_image_bytes=single_cap,
            )
            rendered_paths.append(out_file)

        return rendered_paths

    async def _render_single_page(
        self,
        *,
        tmpl_data: dict[str, Any],
        options: dict[str, Any],
        effective_endpoint: str | None,
        dest_dir: Path,
        page_idx: int,
        max_image_bytes: int,
    ) -> Path:
        html_renderer = (
            getattr(self._context, "html_renderer", None) if self._context is not None else None
        )

        # 路径 1：优先复用本机 AstrBot 已初始化的 context.html_renderer (remote 策略)
        # 仅读取前 32 字节校验图片头，并确保从 AstrBot 全局 temp 目录移入当前请求专属 TempScope，绝不在全局 temp 留垃圾文件
        if html_renderer is not None and hasattr(html_renderer, "render_custom_template"):
            render_fn = html_renderer.render_custom_template
            sig = inspect.signature(render_fn)
            supports_endpoint_kw = "endpoint" in sig.parameters

            if supports_endpoint_kw or not effective_endpoint:
                kwargs: dict[str, Any] = {
                    "return_url": False,
                    "options": options,
                }
                if supports_endpoint_kw and effective_endpoint:
                    kwargs["endpoint"] = effective_endpoint

                raw_path_str = await render_fn(self._tmpl_str, tmpl_data, **kwargs)
                raw_path = Path(str(raw_path_str))
                try:
                    if raw_path.exists():
                        if raw_path.stat().st_size > max_image_bytes:
                            raise RuntimeError("章末吐槽渲染图片超出单张图片体积上限")
                        head = await asyncio.to_thread(_read_head_sync, raw_path, 32)
                        ext = detect_image_extension(head) or ".jpg"
                        target_path = dest_dir / f"comments_{page_idx:02d}{ext}"
                        await asyncio.to_thread(shutil.move, str(raw_path), str(target_path))
                        return target_path
                finally:
                    # 若 move 前发生异常，兜底清理 AstrBot 全局临时目录中可能残留的截图文件
                    if raw_path.exists():
                        raw_path.unlink(missing_ok=True)

        # 路径 2：直接向远程 astrbot-t2i-service (`/text2img/generate`) 发起异步流式 POST
        base_ep = effective_endpoint or await self._discover_official_endpoint()
        gen_url = f"{base_ep}/generate"
        post_payload = {
            "tmpl": self._tmpl_str,
            "json": False,
            "tmpldata": tmpl_data,
            "options": options,
        }

        raw_dest = dest_dir / f"comments_{page_idx:02d}.raw"
        stream_fn = getattr(self._http, "stream_to_file", None)
        if callable(stream_fn):
            st_res = await stream_fn(
                "POST",
                gen_url,
                raw_dest,
                json_body=post_payload,
                timeout_sec=35.0,
                max_bytes=max_image_bytes,
            )
            if st_res.status != 200:
                raw_dest.unlink(missing_ok=True)
                raise RuntimeError(
                    f"远程 HTML 渲染服务 ({gen_url}) 返回 HTTP {st_res.status}: {st_res.error_preview}"
                )
            ext = detect_image_extension(st_res.head_bytes)
            if ext is None:
                raw_dest.unlink(missing_ok=True)
                raise RuntimeError("远程 HTML 渲染服务返回的字节流非合法图片格式")
            target_path = dest_dir / f"comments_{page_idx:02d}{ext}"
            await asyncio.to_thread(os.replace, raw_dest, target_path)
            return target_path

        # 兼容单元测试中仅实现 `.post()` 的桩客户端
        resp = await self._http.post(
            gen_url,
            json_body=post_payload,
            timeout_sec=35.0,
        )
        if resp.status != 200:
            preview = resp.text()[:200]
            raise RuntimeError(f"远程 HTML 渲染服务 ({gen_url}) 返回 HTTP {resp.status}: {preview}")
        if len(resp.body) > max_image_bytes:
            raise RuntimeError("章末吐槽渲染图片体积超限")
        ext = detect_image_extension(resp.body)
        if ext is None:
            raise RuntimeError("远程 HTML 渲染服务返回的字节流非合法图片格式")

        target_path = dest_dir / f"comments_{page_idx:02d}{ext}"
        await asyncio.to_thread(target_path.write_bytes, resp.body)
        return target_path
