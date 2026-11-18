"""OneBot v11 (`aiocqhttp`) 合并转发消息构建、超长分批投递与等待提示撤回模块。

严格满足核心需求：
1. 仅针对 OneBot v11 (`aiocqhttp`) 平台生效；
2. 漫画正文每张图片独占一条合并转发节点消息 (`Node`)，严格保持 `1..N` 页码顺序；
3. 当推送源为再漫画或拷贝漫画时，在合并转发最末尾附上单话评论 / 章末吐槽节点；
4. 超多页或超大体积章节自动按序切分为多批合并转发，杜绝单包溢出失败。
"""

from __future__ import annotations

import asyncio
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain

from .config import DeliveryConfig
from .models import ChapterComment, DownloadedPage, ResolvedChapterBundle

SUPPORTED_PLATFORM = "aiocqhttp"


def is_onebot11_event(event: Any) -> bool:
    """判断事件是否来自 OneBot v11 (`aiocqhttp`) 适配器。"""
    get_pf = getattr(event, "get_platform_name", None)
    if callable(get_pf):
        try:
            return str(get_pf() or "").strip().lower() == SUPPORTED_PLATFORM
        except Exception:
            return False
    return False


def resolve_bot_uin(event: AstrMessageEvent) -> str:
    """提取合法纯数字 QQ 号作为合并转发节点 `uin`。"""
    msg_obj = getattr(event, "message_obj", None)
    raw = getattr(msg_obj, "raw_message", None)
    if raw is not None and callable(getattr(raw, "get", None)):
        raw_self = str(raw.get("self_id") or "").strip()
        if raw_self.isdigit() and raw_self != "0":
            return raw_self

    get_self_id = getattr(event, "get_self_id", None)
    if callable(get_self_id):
        sid = str(get_self_id() or "").strip()
        if sid.isdigit() and sid != "0":
            return sid

    sender_id = str(event.get_sender_id() or "").strip()
    if sender_id.isdigit() and sender_id != "0":
        return sender_id
    return "10000"


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return True
    detail = f"{type(exc).__name__}: {exc}".lower()
    return "timeout" in detail or "timed out" in detail


async def send_wait_tip(event: AstrMessageEvent, text: str) -> int | None:
    """通过 OneBot API 发送解析等待提示并返回 `message_id` 以便完成后撤回。"""
    bot = getattr(event, "bot", None)
    if bot is not None and hasattr(bot, "api"):
        try:
            group_id = str(event.get_group_id() or "").strip()
            params: dict[str, Any] = {
                "message": [{"type": "text", "data": {"text": text}}],
            }
            uin = resolve_bot_uin(event)
            raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
            if raw is not None and callable(getattr(raw, "get", None)) and raw.get("self_id"):
                params["self_id"] = raw["self_id"]

            if group_id and group_id.isdigit():
                params["group_id"] = int(group_id)
                res = await bot.api.call_action("send_group_msg", **params)
            else:
                sender = str(event.get_sender_id() or "").strip()
                if sender.isdigit():
                    params["user_id"] = int(sender)
                    res = await bot.api.call_action("send_private_msg", **params)
                else:
                    res = None
            if isinstance(res, dict) and res.get("message_id") is not None:
                return int(res["message_id"])
            _ = uin
        except Exception as exc:
            logger.debug(f"经 OneBot call_action 发送等待提示失败，回退普通发送：{exc}")

    try:
        await event.send(MessageChain(chain=[Comp.Plain(text)]))
    except Exception:
        pass
    return None


async def recall_wait_tip(event: AstrMessageEvent, message_id: int | None) -> None:
    """静默撤回已发送的等待提示消息。"""
    if message_id is None:
        return
    bot = getattr(event, "bot", None)
    api = getattr(bot, "api", bot) if bot is not None else None
    if api is None or not hasattr(api, "call_action"):
        return
    try:
        params: dict[str, Any] = {"message_id": message_id}
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if raw is not None and callable(getattr(raw, "get", None)) and raw.get("self_id"):
            params["self_id"] = raw["self_id"]
        await api.call_action("delete_msg", **params)
    except Exception:
        pass


def format_comments_text(
    comments: list[ChapterComment],
    source_display_name: str,
) -> str:
    """将再漫画/拷贝漫画的单话评论或章末吐槽格式化为易读文本块。"""
    if not comments:
        return ""
    lines = [f"💬 {source_display_name} · 章末吐槽 / 单话评论（精选 {len(comments)} 条）", ""]
    for idx, c in enumerate(comments, start=1):
        prefix = f"{idx}. "
        if c.author:
            body = f"@{c.author}：{c.content}"
        else:
            body = c.content
        if c.likes > 0:
            body = f"{body}  (👍{c.likes})"
        lines.append(f"{prefix}{body}")
    return "\n".join(lines)


def _split_pages_into_batches(
    pages: list[DownloadedPage],
    *,
    max_pages_per_batch: int,
    max_bytes_per_batch: int,
) -> list[list[DownloadedPage]]:
    """在严格保持 `0..N-1` 页码顺序的前提下，按最大页数与字节预算切分批次。"""
    ordered = sorted(pages, key=lambda p: p.index)
    batches: list[list[DownloadedPage]] = []
    cur_batch: list[DownloadedPage] = []
    cur_bytes = 0

    for page in ordered:
        p_bytes = max(0, page.size_bytes)
        exceed_count = len(cur_batch) >= max_pages_per_batch
        exceed_bytes = bool(cur_batch) and (cur_bytes + p_bytes > max_bytes_per_batch)
        if exceed_count or exceed_bytes:
            batches.append(cur_batch)
            cur_batch = []
            cur_bytes = 0
        cur_batch.append(page)
        cur_bytes += p_bytes

    if cur_batch:
        batches.append(cur_batch)
    return batches


def build_forward_node_batches(
    bundle: ResolvedChapterBundle,
    delivery_cfg: DeliveryConfig,
    *,
    bot_uin: str,
) -> list[list[Comp.Node]]:
    """构建符合 OneBot v11 规范的合并转发批次。

    保证：
    - 每一张漫画图片严格对应一个独立 `Comp.Node`（一张图片一条消息）；
    - 所有图片节点严格按照原始页序 `1..N` 排列；
    - 再漫画或拷贝漫画存在单话评论/章末吐槽时，追加在最后一个批次的最末尾。
    """
    total_pages = len(bundle.downloaded_pages)
    max_pages = max(10, delivery_cfg.max_pages_per_forward)
    max_bytes = max(5, delivery_cfg.max_mb_per_forward) * 1024 * 1024

    page_batches = _split_pages_into_batches(
        bundle.downloaded_pages,
        max_pages_per_batch=max_pages,
        max_bytes_per_batch=max_bytes,
    )
    total_batches = len(page_batches)
    node_batches: list[list[Comp.Node]] = []

    manga_title = bundle.manga.title
    chap_title = bundle.chapter.title
    src_name = bundle.source.display_name

    for b_idx, p_batch in enumerate(page_batches, start=1):
        nodes: list[Comp.Node] = []

        if delivery_cfg.include_header_node:
            part_suffix = f"（第 {b_idx}/{total_batches} 部分）" if total_batches > 1 else ""
            header_lines = [
                f"📖 《{manga_title}》 {chap_title}{part_suffix}",
                f"来源：{src_name}  |  本话共 {total_pages} 页",
            ]
            if bundle.chapter.url:
                header_lines.append(f"🔗 {bundle.chapter.url}")
            nodes.append(
                Comp.Node(
                    uin=bot_uin,
                    name=src_name,
                    content=[Comp.Plain("\n".join(header_lines))],
                )
            )

        # 核心要求：每一页图片严格独占一条转发消息 Node，顺序 100% 与原话页码一致
        for page in p_batch:
            page_no = page.index + 1
            node_title = f"{manga_title} · {chap_title} ({page_no}/{total_pages})"
            if page.local_path is not None and page.local_path.exists():
                img_comp = (
                    Comp.Image.fromFileSystem(str(page.local_path))
                    if hasattr(Comp.Image, "fromFileSystem")
                    else Comp.Image(file=str(page.local_path))
                )
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=node_title,
                        content=[img_comp],
                    )
                )
            else:
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=node_title,
                        content=[Comp.Plain(f"⚠️ 第 {page_no}/{total_pages} 页图片源站加载失败")],
                    )
                )

        # 在最后一个批次的最末尾附上单话评论 / 章末吐槽（仅再漫画与拷贝漫画源）
        is_last_batch = b_idx == total_batches
        if is_last_batch and bundle.source.supports_comments and bundle.comments:
            comments_text = format_comments_text(bundle.comments, src_name)
            if comments_text:
                nodes.append(
                    Comp.Node(
                        uin=bot_uin,
                        name=f"{src_name} · 章末吐槽",
                        content=[Comp.Plain(comments_text)],
                    )
                )

        if nodes:
            node_batches.append(nodes)

    return node_batches


async def send_chapter_forward_batches(
    event: AstrMessageEvent,
    bundle: ResolvedChapterBundle,
    delivery_cfg: DeliveryConfig,
) -> None:
    """将构建好的合并转发批次依次投递至 OneBot v11 会话。"""
    bot_uin = resolve_bot_uin(event)
    batches = build_forward_node_batches(bundle, delivery_cfg, bot_uin=bot_uin)
    total_batches = len(batches)

    for idx, nodes in enumerate(batches, start=1):
        try:
            await event.send(MessageChain(chain=[Comp.Nodes(nodes)]))
        except Exception as exc:
            if _is_timeout_exception(exc):
                # 大批量高清漫画图片经协议端上传 QQ 服务器耗时较长，WebSocket 等待回包超时时协议端仍在后台继续上传，避免重复重发
                logger.warning(
                    f"合并转发批次 ({idx}/{total_batches}) 等待协议端回包超时，协议端后台继续上传中：{exc}"
                )
            else:
                raise
        if idx < total_batches:
            await asyncio.sleep(1.0)
