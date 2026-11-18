"""AstrBot 漫画整话跨源解析与合并转发插件入口 (`main.py`)。"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, StarTools

if __package__:
    from .core.command_parser import extract_args_after_command, parse_manga_command
    from .core.config import redact_secrets
    from .core.image_downloader import TempDirManager
    from .core.models import CommandUsageError, MangaForwardError
    from .core.onebot_sender import (
        is_onebot11_event,
        recall_wait_tip,
        send_chapter_forward_batches,
        send_wait_tip,
    )
    from .core.orchestrator import MangaForwardOrchestrator
else:
    from core.command_parser import extract_args_after_command, parse_manga_command
    from core.config import redact_secrets
    from core.image_downloader import TempDirManager
    from core.models import CommandUsageError, MangaForwardError
    from core.onebot_sender import (
        is_onebot11_event,
        recall_wait_tip,
        send_chapter_forward_batches,
        send_wait_tip,
    )
    from core.orchestrator import MangaForwardOrchestrator

PLUGIN_NAME = "astrbot_plugin_manga_forward"
TEMP_GC_INTERVAL_SEC = 60.0
TEMP_SCOPE_TTL_SEC = 180.0


def _resolve_plugin_data_dir() -> Path:
    """遵循 AstrBot 存储规范获取位于 `data/plugin_data/` 下的插件持久化目录。"""
    try:
        raw_dir = StarTools.get_data_dir(PLUGIN_NAME)
        path = Path(raw_dir)
        path.mkdir(parents=True, exist_ok=True)
        return path
    except Exception:
        fallback = Path("data") / "plugin_data" / PLUGIN_NAME
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


class MangaForwardPlugin(Star):
    """通过 `manga` 指令跨源检索再漫画、拷贝漫画、Komiic 整话图片并以合并转发推送。"""

    def __init__(
        self,
        context: Context,
        config: AstrBotConfig | None = None,
    ) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config if config is not None else {}
        self._data_dir: Path = _resolve_plugin_data_dir()
        self._temp_mgr = TempDirManager(self._data_dir / "temp", ttl_sec=TEMP_SCOPE_TTL_SEC)
        self._orchestrator = MangaForwardOrchestrator(
            self._data_dir,
            lambda: self.config,
            context=self.context,
        )
        self._gc_task: asyncio.Task[None] | None = None
        self._terminated = False

    async def initialize(self) -> None:
        """插件加载生命周期钩子：清扫上次异常崩溃残留目录/垃圾缓存并启动每分钟巡检守护协程。"""
        self._terminated = False
        with contextlib.suppress(Exception):
            await self._temp_mgr.cleanup_expired(force_all_inactive=True)
        with contextlib.suppress(Exception):
            await self._orchestrator.run_maintenance()
        if self._gc_task is None or self._gc_task.done():
            self._gc_task = asyncio.create_task(self._gc_loop(), name=f"{PLUGIN_NAME}_temp_gc")

    async def _gc_loop(self) -> None:
        try:
            while not self._terminated:
                await asyncio.sleep(TEMP_GC_INTERVAL_SEC)
                try:
                    removed_temp = await self._temp_mgr.cleanup_expired()
                    evicted_ram, removed_cache = await self._orchestrator.run_maintenance()
                    if removed_temp > 0 or removed_cache > 0:
                        logger.debug(
                            f"自动清理完成：回收临时目录/垃圾文件 {removed_temp} 项，"
                            f"索引缓存 {removed_cache} 项（内存索引卸载={evicted_ram}）。"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug(f"漫画缓存与临时目录巡检单轮异常：{exc}")
        except asyncio.CancelledError:
            return

    async def terminate(self) -> None:
        """插件停用/重载生命周期钩子：幂等取消后台任务、卸载内存索引并清扫全部非活跃临时目录。"""
        self._terminated = True
        if self._gc_task is not None and not self._gc_task.done():
            self._gc_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._gc_task
        self._gc_task = None
        await self._orchestrator.close()
        with contextlib.suppress(Exception):
            await self._temp_mgr.cleanup_expired(force_all_inactive=True)

    @filter.command("manga")
    @filter.platform_adapter_type(filter.PlatformAdapterType.AIOCQHTTP)
    async def cmd_manga(self, event: AstrMessageEvent):
        """解析整话漫画并通过合并消息按序推送。用法：manga <漫画名> <第几话>"""
        # 一期严格限定仅处理 OneBot v11 (aiocqhttp) 消息事件，其他平台静默忽略
        if not is_onebot11_event(event):
            return

        raw_msg = (
            event.get_message_str() if hasattr(event, "get_message_str") else event.message_str
        )
        raw_args = extract_args_after_command(raw_msg or "")
        try:
            parsed_cmd = parse_manga_command(raw_args)
        except CommandUsageError as exc:
            yield event.plain_result(str(exc))
            return

        cfg = self._orchestrator.current_config()
        secrets = [
            cfg.zaimanhua.password,
            cfg.zaimanhua.cookie,
            cfg.copymanga.token,
            cfg.komiic.password,
            cfg.komiic.cookie,
        ]

        wait_msg_id: int | None = None
        if cfg.delivery.send_wait_tip:
            src_hint = (
                f"（指定源：{parsed_cmd.forced_source.display_name}）"
                if parsed_cmd.forced_source
                else ""
            )
            tip_text = (
                f"⏳ 正在检索并拉取《{parsed_cmd.manga_query}》"
                f" {parsed_cmd.chapter_spec}{src_hint}，请稍候…"
            )
            wait_msg_id = await send_wait_tip(event, tip_text)

        temp_scope = self._temp_mgr.create_scope()
        umo = getattr(event, "unified_msg_origin", None)
        try:
            bundle = await self._orchestrator.resolve_and_download(
                parsed_cmd,
                temp_scope,
                umo=str(umo) if umo else None,
            )
            await send_chapter_forward_batches(event, bundle, cfg.delivery)
        except MangaForwardError as exc:
            safe_msg = redact_secrets(str(exc), secrets)
            yield event.plain_result(f"❌ {safe_msg}")
        except Exception as exc:
            safe_err = redact_secrets(f"{type(exc).__name__}: {exc}", secrets)
            logger.exception(f"处理 manga 指令发生未预期异常：{safe_err}")
            yield event.plain_result(f"❌ 解析或发送漫画时发生异常：{safe_err}")
        finally:
            if wait_msg_id is not None and cfg.delivery.recall_wait_tip:
                await recall_wait_tip(event, wait_msg_id)
            await temp_scope.cleanup()
