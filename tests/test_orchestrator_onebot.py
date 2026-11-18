"""跨源优先级回退、并发下载严格保序、OneBot v11 分批合并转发与插件生命周期端到端测试。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
import pytest
import yaml

from core.config import DeliveryConfig, PluginConfig
from core.http_client import HttpResponseData
from core.image_downloader import ChapterImageDownloader, TempDirManager
from core.models import (
    AllSourcesFailedError,
    ChapterComment,
    ChapterInfo,
    ChapterKind,
    MangaCandidate,
    MangaNotFoundError,
    PageSpec,
    ParsedMangaCommand,
    SourceId,
    SourceQuotaError,
    SourceThrottleError,
)
from core.onebot_sender import build_forward_node_batches
from core.orchestrator import MangaForwardOrchestrator
from core.sources.base import BaseMangaSource
from main import MangaForwardPlugin

# 最小合法 PNG 魔数头图片字节
VALID_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde"
)


class _DelayedImageHttp:
    """故意让后面的页码更快返回、第 1 页最慢返回，检验并发下载器是否严格保持 1..N 顺序。"""

    async def get(self, url: str, **_: Any) -> HttpResponseData:
        if "402" in url:
            return HttpResponseData(status=402, headers={}, cookies={}, body=b"", url=url)
        page_num = int(url.rsplit("/", 1)[-1].split(".")[0])
        # 页码越小延迟越长：p1 延迟 40ms，p4 延迟 5ms
        await asyncio.sleep(max(0.005, (5 - page_num) * 0.01))
        payload = VALID_PNG_BYTES + f"PAGE_{page_num}".encode("ascii")
        return HttpResponseData(status=200, headers={}, cookies={}, body=payload, url=url)


class _StubSource(BaseMangaSource):
    def __init__(
        self,
        source_id: SourceId,
        *,
        fail_exc: Exception | None = None,
        comments: list[ChapterComment] | None = None,
        comment_exc: Exception | None = None,
        page_count: int = 4,
    ) -> None:
        self.source_id = source_id
        self._fail_exc = fail_exc
        self._comments = comments or []
        self._comment_exc = comment_exc
        self._page_count = page_count

    async def search_manga(self, keyword: str) -> list[MangaCandidate]:
        if self._fail_exc is not None:
            raise self._fail_exc
        return [
            MangaCandidate(
                source=self.source_id,
                manga_id="m1",
                title=keyword,
                match_score=1.0,
            )
        ]

    async def get_manga_by_id(self, manga_id: str) -> MangaCandidate:
        return MangaCandidate(
            source=self.source_id,
            manga_id=manga_id,
            title=f"ID-{manga_id}",
            match_score=1.0,
        )

    async def fetch_chapters(self, manga: MangaCandidate) -> list[ChapterInfo]:
        return [
            ChapterInfo(
                chapter_id="ch1",
                title="第1话",
                number=1.0,
                kind=ChapterKind.CHAPTER,
                url="https://example.com/ch1",
            )
        ]

    async def fetch_chapter_pages(
        self, manga: MangaCandidate, chapter: ChapterInfo
    ) -> list[PageSpec]:
        return [
            PageSpec(index=i, url=f"https://img.example.com/{i + 1}.png")
            for i in range(self._page_count)
        ]

    async def fetch_chapter_comments(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        max_count: int = 20,
    ) -> list[ChapterComment]:
        if self._comment_exc is not None:
            raise self._comment_exc
        return self._comments[:max_count]


@pytest.mark.asyncio
async def test_priority_fallback_order_preservation_and_forward_nodes(tmp_path: Path) -> None:
    from jinja2 import Environment

    rendered_calls: list[dict[str, Any]] = []

    class _MockHtmlRenderer:
        async def render_custom_template(
            self,
            tmpl_str: str,
            tmpl_data: dict[str, Any],
            return_url: bool = False,
            options: dict[str, Any] | None = None,
            endpoint: str | None = None,
        ) -> str:
            # 真实编译并渲染 Jinja2 HTML 模板，确保 comments_card.html 零语法错误且转义安全
            env = Environment(autoescape=True)
            html_out = env.from_string(tmpl_str).render(**tmpl_data)
            assert tmpl_data["manga_title"] in html_out
            assert f"PAGE {tmpl_data['page_no']} OF {tmpl_data['total_pages']}" in html_out
            rendered_calls.append(
                {"tmpl_data": tmpl_data, "options": options, "endpoint": endpoint}
            )
            out_tmp = tmp_path / f"astrbot_t2i_out_{len(rendered_calls)}.png"
            out_tmp.write_bytes(VALID_PNG_BYTES + f"CMT_PAGE_{len(rendered_calls)}".encode("ascii"))
            return str(out_tmp)

    mock_ctx = type(
        "Ctx",
        (),
        {
            "html_renderer": _MockHtmlRenderer(),
            "get_config": lambda self, umo=None: {"t2i_endpoint": "http://127.0.0.1:8999"},
        },
    )()

    # 配置优先级：拷贝漫画 -> 再漫画 -> Komiic
    # 模拟 45 条章末吐槽，按每页 20 条切分为 3 页图片（20 + 20 + 5）
    roast_45 = [
        ChapterComment(
            content=f"第{i}条章末神吐槽！", author=f"读者{i}" if i % 2 == 0 else "", likes=i
        )
        for i in range(1, 46)
    ]
    raw_cfg = {
        "source_priority": ["copymanga", "zaimanhua", "komiic"],
        "comments": {
            "enable_comments": True,
            "comments_per_page": 20,
            "max_comments": 60,
            "t2i_endpoint": "",
            "theme": "paper",
        },
        "delivery": {"include_header_node": True, "max_pages_per_forward": 10},
    }
    orch = MangaForwardOrchestrator(tmp_path, lambda: raw_cfg, context=mock_ctx)
    orch._downloader = ChapterImageDownloader(_DelayedImageHttp())  # type: ignore[arg-type]
    orch._sources = {
        SourceId.COPYMANGA: _StubSource(
            SourceId.COPYMANGA, fail_exc=SourceThrottleError("code=210 限流")
        ),
        SourceId.ZAIMANHUA: _StubSource(
            SourceId.ZAIMANHUA,
            comments=roast_45,
            page_count=4,
        ),
        SourceId.KOMIIC: _StubSource(SourceId.KOMIIC),
    }

    temp_mgr = TempDirManager(tmp_path / "temp")
    scope = temp_mgr.create_scope()
    try:
        bundle = await orch.resolve_and_download(
            ParsedMangaCommand(manga_query="葬送的芙莉莲", chapter_spec="第1话"),
            scope,
            umo="aiocqhttp:GroupMessage:654321",
        )
        assert bundle.source == SourceId.ZAIMANHUA
        assert [p.index for p in bundle.downloaded_pages] == [0, 1, 2, 3]
        # 验证 45 条吐槽被精确切分为 20 + 20 + 5 共 3 张渲染图片，且默认复用了本机 AstrBot 全局 t2i_endpoint
        assert len(bundle.comment_image_paths) == 3
        assert [len(c["tmpl_data"]["items"]) for c in rendered_calls] == [20, 20, 5]
        assert rendered_calls[0]["endpoint"] == "http://127.0.0.1:8999/text2img"

        for expected_page_no, dp in enumerate(bundle.downloaded_pages, start=1):
            assert dp.local_path is not None
            raw_bytes = dp.local_path.read_bytes()
            assert raw_bytes.endswith(f"PAGE_{expected_page_no}".encode("ascii"))

        batches = build_forward_node_batches(
            bundle,
            PluginConfig.from_mapping(raw_cfg).delivery,
            bot_uin="123456789",
        )
        assert len(batches) == 1
        nodes = batches[0]
        # 1 个概览头节点 + 4 个漫画单图节点 + 3 个章末吐槽图片节点 = 8 个节点
        assert len(nodes) == 8
        assert isinstance(nodes[0].content[0], Comp.Plain)
        for img_idx in range(1, 5):
            assert len(nodes[img_idx].content) == 1
            assert isinstance(nodes[img_idx].content[0], Comp.Image)
            assert f"({img_idx}/4)" in (nodes[img_idx].name or "")
        for cmt_idx in range(1, 4):
            c_node = nodes[4 + cmt_idx]
            assert len(c_node.content) == 1
            assert isinstance(c_node.content[0], Comp.Image)
            assert f"章末吐槽 ({cmt_idx}/3)" in (c_node.name or "")
    finally:
        await scope.cleanup()
        await orch.close()
    assert not scope.path.exists()


@pytest.mark.asyncio
async def test_comment_render_custom_endpoint_and_fallback_to_text(tmp_path: Path) -> None:
    """验证自定义 t2i_endpoint 直连渲染，以及当远程 T2I 异常时平滑降级为文本节点。"""
    posted_urls: list[str] = []

    class _MockT2iHttp(_DelayedImageHttp):
        async def post(self, url: str, **kwargs: Any) -> HttpResponseData:
            posted_urls.append(url)
            if "broken-t2i" in url:
                return HttpResponseData(
                    status=502, headers={}, cookies={}, body=b"Bad Gateway", url=url
                )
            return HttpResponseData(
                status=200,
                headers={"content-type": "image/png"},
                cookies={},
                body=VALID_PNG_BYTES + b"DIRECT_T2I",
                url=url,
            )

    # 1. 自定义 t2i_endpoint 直连成功
    cfg_ok = {
        "source_priority": ["copymanga"],
        "comments": {
            "enable_comments": True,
            "comments_per_page": 20,
            "t2i_endpoint": "http://custom-t2i.local:8999",
        },
    }
    orch_ok = MangaForwardOrchestrator(tmp_path, lambda: cfg_ok, context=None)
    mock_http = _MockT2iHttp()
    orch_ok._downloader = ChapterImageDownloader(mock_http)  # type: ignore[arg-type]
    orch_ok._comment_renderer._http = mock_http  # type: ignore[assignment]
    orch_ok._sources[SourceId.COPYMANGA] = _StubSource(
        SourceId.COPYMANGA,
        comments=[ChapterComment(content="自定义T2I渲染测试", author="路人A")],
        page_count=2,
    )
    scope1 = TempDirManager(tmp_path / "temp").create_scope()
    try:
        b1 = await orch_ok.resolve_and_download(
            ParsedMangaCommand(manga_query="异剑战记", chapter_spec="第1话"),
            scope1,
        )
        assert posted_urls == ["http://custom-t2i.local:8999/text2img/generate"]
        assert len(b1.comment_image_paths) == 1
    finally:
        await scope1.cleanup()
        await orch_ok.close()

    # 2. 远程 T2I 服务 502 故障时，自动回退为纯文本章末吐槽节点，绝不中断漫画发送
    cfg_fb = {
        "source_priority": ["copymanga"],
        "comments": {
            "enable_comments": True,
            "t2i_endpoint": "http://broken-t2i.local:8999",
            "fallback_to_text": True,
        },
    }
    orch_fb = MangaForwardOrchestrator(tmp_path, lambda: cfg_fb, context=None)
    orch_fb._downloader = ChapterImageDownloader(mock_http)  # type: ignore[arg-type]
    orch_fb._comment_renderer._http = mock_http  # type: ignore[assignment]
    orch_fb._sources[SourceId.COPYMANGA] = _StubSource(
        SourceId.COPYMANGA,
        comments=[ChapterComment(content="降级文本吐槽仍可达", author="路人B")],
        page_count=2,
    )
    scope2 = TempDirManager(tmp_path / "temp").create_scope()
    try:
        b2 = await orch_fb.resolve_and_download(
            ParsedMangaCommand(manga_query="异剑战记", chapter_spec="第1话"),
            scope2,
        )
        assert b2.comment_image_paths == []
        assert len(b2.comments) == 1
        batches = build_forward_node_batches(
            b2, PluginConfig.from_mapping(cfg_fb).delivery, bot_uin="10000"
        )
        assert isinstance(batches[0][-1].content[0], Comp.Plain)
        assert "降级文本吐槽仍可达" in batches[0][-1].content[0].text
    finally:
        await scope2.cleanup()
        await orch_fb.close()


@pytest.mark.asyncio
async def test_komiic_no_comments_and_batch_splitting(tmp_path: Path) -> None:
    raw_cfg = {
        "source_priority": ["komiic"],
        "comments": {"enable_comments": True},
        "delivery": {"include_header_node": False, "max_pages_per_forward": 10},
    }
    orch = MangaForwardOrchestrator(tmp_path, lambda: raw_cfg)
    orch._downloader = ChapterImageDownloader(_DelayedImageHttp())  # type: ignore[arg-type]
    orch._sources[SourceId.KOMIIC] = _StubSource(
        SourceId.KOMIIC,
        comments=[ChapterComment(content="不应出现的评论")],
        page_count=25,
    )

    temp_mgr = TempDirManager(tmp_path / "temp")
    scope = temp_mgr.create_scope()
    try:
        bundle = await orch.resolve_and_download(
            ParsedMangaCommand(manga_query="电锯人", chapter_spec="1"),
            scope,
        )
        assert bundle.source == SourceId.KOMIIC
        assert bundle.comments == []

        batches = build_forward_node_batches(
            bundle,
            DeliveryConfig(
                include_header_node=False, max_pages_per_forward=10, max_mb_per_forward=28
            ),
            bot_uin="99999",
        )
        # 25 页按每批 10 页切分为 10 + 10 + 5 三批，无头尾文本节点
        assert [len(b) for b in batches] == [10, 10, 5]
        assert "(1/25)" in (batches[0][0].name or "")
        assert "(11/25)" in (batches[1][0].name or "")
        assert "(25/25)" in (batches[2][-1].name or "")
    finally:
        await scope.cleanup()
        await orch.close()


@pytest.mark.asyncio
async def test_all_sources_fail_and_402_quota_redaction(tmp_path: Path) -> None:
    raw_cfg = {
        "source_priority": ["zaimanhua", "copymanga", "komiic"],
        "zaimanhua": {"password": "my_super_secret_pwd"},
    }
    orch = MangaForwardOrchestrator(tmp_path, lambda: raw_cfg)
    orch._downloader = ChapterImageDownloader(_DelayedImageHttp())  # type: ignore[arg-type]

    class _QuotaKomiic(_StubSource):
        async def fetch_chapter_pages(
            self, manga: MangaCandidate, chapter: ChapterInfo
        ) -> list[PageSpec]:
            return [PageSpec(index=0, url="https://komiic.com/api/image/402")]

    orch._sources = {
        SourceId.ZAIMANHUA: _StubSource(
            SourceId.ZAIMANHUA,
            fail_exc=MangaNotFoundError("认证错误 my_super_secret_pwd 未找到"),
        ),
        SourceId.COPYMANGA: _StubSource(
            SourceId.COPYMANGA,
            fail_exc=SourceThrottleError("限流"),
        ),
        SourceId.KOMIIC: _QuotaKomiic(SourceId.KOMIIC),
    }

    scope = TempDirManager(tmp_path / "temp").create_scope()
    try:
        with pytest.raises(AllSourcesFailedError) as exc_info:
            await orch.resolve_and_download(
                ParsedMangaCommand(manga_query="测试漫画", chapter_spec="1"),
                scope,
            )
        err_text = str(exc_info.value)
        assert "my_super_secret_pwd" not in err_text
        assert "***" in err_text
        assert "再漫画" in err_text and "拷贝漫画" in err_text and "Komiic" in err_text
    finally:
        await scope.cleanup()
        await orch.close()
    _ = SourceQuotaError


class _DummyEvent:
    def __init__(self, platform: str, message_str: str) -> None:
        self._platform = platform
        self.message_str = message_str
        self.sent_chains: list[Any] = []
        self.deleted_msg_ids: list[int] = []
        self.message_obj = type("M", (), {"raw_message": {"self_id": 888888}})()
        self.bot = type("B", (), {"api": self})()

    def get_platform_name(self) -> str:
        return self._platform

    def get_message_str(self) -> str:
        return self.message_str

    def get_group_id(self) -> str:
        return "654321"

    def get_sender_id(self) -> str:
        return "111222"

    async def call_action(self, action: str, **params: Any) -> dict[str, Any]:
        if action == "send_group_msg":
            return {"message_id": 777}
        if action == "delete_msg":
            self.deleted_msg_ids.append(int(params["message_id"]))
            return {}
        return {}

    async def send(self, chain: Any) -> None:
        self.sent_chains.append(chain)

    def plain_result(self, text: str) -> str:
        return text


@pytest.mark.asyncio
async def test_plugin_lifecycle_and_platform_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import main as main_mod

    monkeypatch.setattr(main_mod, "_resolve_plugin_data_dir", lambda: tmp_path)
    plugin = MangaForwardPlugin(context=object(), config={})  # type: ignore[arg-type]
    await plugin.initialize()
    try:
        # 非 aiocqhttp 平台必须静默忽略，不产生任何回复或异常
        tg_event = _DummyEvent("telegram", "/manga 电锯人 1")
        tg_outputs = [res async for res in plugin.cmd_manga(tg_event)]  # type: ignore[arg-type]
        assert tg_outputs == []
        assert tg_event.sent_chains == []

        # aiocqhttp 平台空参数返回清晰用法提示
        qq_event = _DummyEvent("aiocqhttp", "/manga")
        qq_outputs = [res async for res in plugin.cmd_manga(qq_event)]  # type: ignore[arg-type]
        assert len(qq_outputs) == 1
        assert "manga <漫画名> <第几话>" in qq_outputs[0]
    finally:
        # 验证重复调用 terminate() 幂等安全无泄漏
        await plugin.terminate()
        await plugin.terminate()


def test_metadata_and_conf_schema_integrity() -> None:
    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / "_conf_schema.json").read_text(encoding="utf-8"))
    meta = yaml.safe_load((root / "metadata.yaml").read_text(encoding="utf-8"))

    assert meta["name"] == "astrbot_plugin_manga_forward"
    assert meta["support_platforms"] == ["aiocqhttp"]
    logo_file = root / "logo.png"
    assert logo_file.exists() and logo_file.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    # 校验所有顶层与二级配置项均包含合法的 type、description、default，且敏感凭据均标注 secret=True
    for key, spec in schema.items():
        assert "type" in spec and "description" in spec, f"{key} 缺少必填描述"
        if spec["type"] == "object":
            for sub_key, sub_spec in spec["items"].items():
                assert "type" in sub_spec and "description" in sub_spec
                assert "default" in sub_spec, f"{key}.{sub_key} 缺少 default"
                if sub_key in ("password", "cookie", "token"):
                    assert sub_spec.get("secret") is True, f"{key}.{sub_key} 必须设置 secret: true"
        else:
            assert "default" in spec


@pytest.mark.asyncio
async def test_temp_dir_active_scope_protection_and_budget_eviction(tmp_path: Path) -> None:
    """验证 GC 清理崩溃残留与超限缓存时，绝不误删正在下载/发送中的活跃作用域文件。"""
    temp_root = tmp_path / "temp"
    mgr = TempDirManager(temp_root, ttl_sec=180.0, max_total_bytes=100)

    active_scope = mgr.create_scope()
    active_file = active_scope.subdir("copymanga") / "0001.png"
    active_file.write_bytes(VALID_PNG_BYTES)

    # 模拟进程上次崩溃遗留的非活跃目录与半截 .part 文件
    orphan_dir = temp_root / "req_1700000000_deadbeef"
    orphan_dir.mkdir(parents=True)
    (orphan_dir / "leftover.jpg").write_bytes(b"x" * 200)
    stray_part = temp_root / "dangling.part"
    stray_part.write_bytes(b"partial")

    removed = await mgr.cleanup_expired(force_all_inactive=True)
    assert removed == 2
    assert not orphan_dir.exists()
    assert not stray_part.exists()
    # 活跃作用域及其图片完好无损
    assert active_file.exists()

    await active_scope.cleanup()
    assert not active_scope.path.exists()
    await mgr.cleanup_expired()
    assert not temp_root.exists()


@pytest.mark.asyncio
async def test_bounded_task_limiter_backpressure() -> None:
    """验证并发排队背压控制器在队列满载时立即快速拒绝，防止协程无限堆积导致 OOM。"""
    from core.models import MangaForwardError
    from core.orchestrator import BoundedTaskLimiter

    limiter = BoundedTaskLimiter(max_concurrent=1, max_waiting=1)
    entered_1 = asyncio.Event()
    release_1 = asyncio.Event()
    order: list[str] = []

    async def _worker_1() -> None:
        async with limiter:
            order.append("w1_in")
            entered_1.set()
            await release_1.wait()
            order.append("w1_out")

    async def _worker_2() -> None:
        async with limiter:
            order.append("w2_in")

    t1 = asyncio.create_task(_worker_1())
    await entered_1.wait()

    t2 = asyncio.create_task(_worker_2())
    await asyncio.sleep(0.02)

    # 此时 active=1 (t1), waiting=1 (t2)，第 3 个并发请求必须立即触发背压报错
    with pytest.raises(MangaForwardError, match="队列繁忙"):
        async with limiter:
            pass

    release_1.set()
    await asyncio.gather(t1, t2)
    assert order == ["w1_in", "w1_out", "w2_in"]


@pytest.mark.asyncio
async def test_streaming_download_and_incremental_batch_file_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证 64KB 流式落盘及多批合并转发每发完一批立即删除该批磁盘图片释放内存/磁盘。"""
    import core.onebot_sender as sender_mod
    from core.http_client import HttpStreamResult
    from core.models import DownloadedPage, ResolvedChapterBundle
    from core.onebot_sender import send_chapter_forward_batches

    # 避免测试等待批次间 1 秒间隔
    async def _fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(sender_mod.asyncio, "sleep", _fast_sleep)

    class _StreamingStubHttp:
        async def stream_to_file(
            self,
            method: str,
            url: str,
            dest_path: Path,
            *,
            headers: dict[str, str] | None = None,
            max_bytes: int = 1024 * 1024,
            **_: Any,
        ) -> HttpStreamResult:
            payload = VALID_PNG_BYTES + b"S" * 64
            assert len(payload) <= max_bytes
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(payload)
            return HttpStreamResult(
                status=200,
                headers={},
                size_bytes=len(payload),
                head_bytes=payload[:32],
            )

    dl = ChapterImageDownloader(_StreamingStubHttp())  # type: ignore[arg-type]
    stub_src = _StubSource(SourceId.COPYMANGA, page_count=12)
    cand = MangaCandidate(source=SourceId.COPYMANGA, manga_id="m1", title="异剑战记")
    chap = ChapterInfo(chapter_id="c2", title="第02话", number=2.0)
    specs = await stub_src.fetch_chapter_pages(cand, chap)

    pages_dir = tmp_path / "pages"
    pages: list[DownloadedPage] = await dl.download_chapter_pages(
        source=stub_src,
        manga=cand,
        chapter=chap,
        specs=specs,
        dest_dir=pages_dir,
        concurrency=4,
    )
    assert len(pages) == 12
    assert all(p.local_path is not None and p.local_path.exists() for p in pages)

    cmt_img = tmp_path / "comments_p1.jpg"
    cmt_img.write_bytes(VALID_PNG_BYTES)

    bundle = ResolvedChapterBundle(
        source=SourceId.COPYMANGA,
        manga=cand,
        chapter=chap,
        downloaded_pages=pages,
        comments=[ChapterComment(content="好耶")],
        comment_image_paths=[cmt_img],
    )

    batch_disk_snapshots: list[tuple[bool, bool]] = []

    class _InspectingEvent(_DummyEvent):
        async def send(self, chain: Any) -> None:
            self.sent_chains.append(chain)
            if len(self.sent_chains) == 1:
                # 第 1 批发送瞬间：第 1 批图片尚在，第 2 批图片也在
                batch_disk_snapshots.append(
                    (pages[0].local_path.exists(), pages[10].local_path.exists())  # type: ignore[union-attr]
                )
            elif len(self.sent_chains) == 2:
                # 进入第 2 批发送时：第 1 批的 10 张图片已在上一批 finally 中被立即删除！
                batch_disk_snapshots.append(
                    (pages[0].local_path.exists(), pages[10].local_path.exists())  # type: ignore[union-attr]
                )

    ev = _InspectingEvent("aiocqhttp", "/manga 异剑战记 第二话")
    await send_chapter_forward_batches(
        ev,
        bundle,
        DeliveryConfig(include_header_node=True, max_pages_per_forward=10, max_mb_per_forward=20),
    )
    assert len(ev.sent_chains) == 2
    assert batch_disk_snapshots == [(True, True), (False, True)]
    # 全部批次发完后，所有正文图片与吐槽渲染图均已被即时回收
    assert all(p.local_path is not None and not p.local_path.exists() for p in pages)
    assert not cmt_img.exists()


def test_restored_batch_defaults_and_adjustable_image_limits() -> None:
    """验证单批默认上限已还原为 60 页 / 28MB，且单图、整话与分批上限均可通过设置自由调整。"""
    default_cfg = PluginConfig.from_mapping(None)
    assert default_cfg.delivery.max_pages_per_forward == 60
    assert default_cfg.delivery.max_mb_per_forward == 28
    assert default_cfg.network.download_concurrency == 6
    assert default_cfg.network.max_single_image_mb == 18
    assert default_cfg.network.max_chapter_mb == 300

    custom_cfg = PluginConfig.from_mapping(
        {
            "delivery": {"max_pages_per_forward": 120, "max_mb_per_forward": 80},
            "network": {"max_single_image_mb": 40, "max_chapter_mb": 600},
        }
    )
    assert custom_cfg.delivery.max_pages_per_forward == 120
    assert custom_cfg.delivery.max_mb_per_forward == 80
    assert custom_cfg.network.max_single_image_mb == 40
    assert custom_cfg.network.max_chapter_mb == 600


@pytest.mark.asyncio
async def test_tankobon_200_pages_mid_volume_sign_expiry_batch_refresh(tmp_path: Path) -> None:
    """模拟拉取 198 页单行本整卷且中途跨越 2 次防盗链签名过期窗口，验证整章批量重刷零丢页且无重刷风暴。"""
    from core.http_client import HttpStreamResult

    total_vol_pages = 198
    refresh_calls = 0

    class _ExpiringSignTankobonSource(_StubSource):
        async def refresh_all_page_urls(
            self, manga: MangaCandidate, chapter: ChapterInfo
        ) -> list[PageSpec] | None:
            nonlocal refresh_calls
            refresh_calls += 1
            gen = refresh_calls + 1
            return [
                PageSpec(index=i, url=f"https://images.zaimanhua.com/v34/{i + 1:03d}.jpg?gen={gen}")
                for i in range(total_vol_pages)
            ]

    class _ExpiringSignHttp:
        async def stream_to_file(
            self,
            method: str,
            url: str,
            dest_path: Path,
            **_: Any,
        ) -> HttpStreamResult:
            page_no = int(url.split("/v34/")[1].split(".")[0])
            gen = int(url.split("gen=")[1])
            # 模拟第 65 页起第 1 代签名过期返回 403，第 140 页起第 2 代签名再次过期返回 403
            if (page_no >= 65 and gen < 2) or (page_no >= 140 and gen < 3):
                return HttpStreamResult(status=403, headers={}, size_bytes=0, head_bytes=b"")
            payload = VALID_PNG_BYTES + f"VOL_P_{page_no}".encode("ascii")
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_bytes(payload)
            return HttpStreamResult(
                status=200,
                headers={},
                size_bytes=len(payload),
                head_bytes=payload[:32],
            )

    src = _ExpiringSignTankobonSource(SourceId.ZAIMANHUA, page_count=total_vol_pages)
    dl = ChapterImageDownloader(_ExpiringSignHttp())  # type: ignore[arg-type]
    cand = MangaCandidate(source=SourceId.ZAIMANHUA, manga_id="40956", title="女朋友、借我一下")
    vol = ChapterInfo(chapter_id="156539", title="34卷", number=34.0, kind=ChapterKind.VOLUME)
    init_specs = [
        PageSpec(index=i, url=f"https://images.zaimanhua.com/v34/{i + 1:03d}.jpg?gen=1")
        for i in range(total_vol_pages)
    ]

    pages = await dl.download_chapter_pages(
        source=src,
        manga=cand,
        chapter=vol,
        specs=init_specs,
        dest_dir=tmp_path / "vol34",
        concurrency=6,
    )
    assert len(pages) == total_vol_pages
    assert all(p.local_path is not None and p.local_path.exists() for p in pages)
    assert [p.index for p in pages] == list(range(total_vol_pages))
    # 跨越 2 次签名过期窗口仅触发 2 次整章批量刷新（而非 130+ 次单页刷新风暴）
    assert refresh_calls == 2


@pytest.mark.asyncio
async def test_http_stream_range_resume_on_content_length_truncation(tmp_path: Path) -> None:
    """模拟 CDN 跨境回源半截断流 (`ContentLengthError`: received 179343 of 315122 bytes)，验证 HTTP 206 Range 自动断点续传拼接完整图片。"""
    from core.http_client import AsyncHttpClient

    full_img = VALID_PNG_BYTES + (b"PNG_BODY_DATA_0123456789ABCDEF" * 10503)
    cut_offset = 179343
    assert len(full_img) > cut_offset
    received_ranges: list[str] = []

    async def _handle_raw_tcp(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            raw_req = await reader.readuntil(b"\r\n\r\n")
            req_text = raw_req.decode("latin1")
            range_hdr = ""
            for line in req_text.splitlines():
                if line.lower().startswith("range:"):
                    range_hdr = line.split(":", 1)[1].strip()
                    break
            received_ranges.append(range_hdr)

            if not range_hdr:
                # 第 1 次请求：声明完整 Content-Length，但在 179343 字节处直接断开连接复现 ContentLengthError
                hdr = (
                    f"HTTP/1.1 200 OK\r\n"
                    f"Content-Type: image/png\r\n"
                    f"Accept-Ranges: bytes\r\n"
                    f"Content-Length: {len(full_img)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode("ascii")
                writer.write(hdr + full_img[:cut_offset])
                await writer.drain()
            else:
                # 第 2 次请求：校验客户端携带 Range: bytes=179343-，返回 HTTP 206 剩余字节
                start_pos = int(range_hdr.removeprefix("bytes=").rstrip("-"))
                tail = full_img[start_pos:]
                hdr = (
                    f"HTTP/1.1 206 Partial Content\r\n"
                    f"Content-Type: image/png\r\n"
                    f"Content-Range: bytes {start_pos}-{len(full_img) - 1}/{len(full_img)}\r\n"
                    f"Content-Length: {len(tail)}\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode("ascii")
                writer.write(hdr + tail)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(_handle_raw_tcp, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    http = AsyncHttpClient(timeout_sec=10.0)
    out_file = tmp_path / "resumed_0033.png"
    try:
        res = await http.stream_to_file("GET", f"http://127.0.0.1:{port}/34.png", out_file)
        assert res.status == 200
        assert res.size_bytes == len(full_img)
        assert len(received_ranges) >= 2
        assert received_ranges[0] == ""
        assert received_ranges[-1].startswith("bytes=")
        resumed_from = int(received_ranges[-1].removeprefix("bytes=").rstrip("-"))
        assert 0 < resumed_from <= cut_offset
        assert out_file.read_bytes() == full_img
    finally:
        await http.close()
        server.close()
        await server.wait_closed()


def test_missing_page_header_summary_and_placeholder_node(tmp_path: Path) -> None:
    """验证中间某页经多次重试仍失败时，合并转发首条概览与对应页位均会明确提醒用户。"""
    from core.models import DownloadedPage, ResolvedChapterBundle

    p1 = tmp_path / "0001.png"
    p3 = tmp_path / "0003.png"
    p1.write_bytes(VALID_PNG_BYTES)
    p3.write_bytes(VALID_PNG_BYTES)

    bundle = ResolvedChapterBundle(
        source=SourceId.ZAIMANHUA,
        manga=MangaCandidate(source=SourceId.ZAIMANHUA, manga_id="60131", title="平屋小品"),
        chapter=ChapterInfo(chapter_id="117888", title="1话", number=1.0),
        downloaded_pages=[
            DownloadedPage(index=0, local_path=p1, size_bytes=len(VALID_PNG_BYTES)),
            DownloadedPage(index=1, local_path=None, error="HTTP 502"),
            DownloadedPage(index=2, local_path=p3, size_bytes=len(VALID_PNG_BYTES)),
        ],
    )
    batches = build_forward_node_batches(bundle, DeliveryConfig(), bot_uin="10000")
    nodes = batches[0]
    header_txt = nodes[0].content[0].text
    slot2_txt = nodes[2].content[0].text
    assert "⚠️ 第 2 页经多次重试仍加载失败" in header_txt
    assert "⚠️ 第 2/3 页图片经多次重试仍加载失败（HTTP 502）" in slot2_txt


@pytest.mark.asyncio
async def test_batch_multi_chapter_streaming_send_and_mid_batch_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """验证批量多话流式逐话发送：首条带〔批量 k/N〕标识、上一话目录在下一话下载前已清空、中途单话异常自动跨源回退。"""
    import main as main_mod

    async def _fast_sleep(_: float) -> None:
        return None

    monkeypatch.setattr(main_mod, "_resolve_plugin_data_dir", lambda: tmp_path)
    monkeypatch.setattr(main_mod.asyncio, "sleep", _fast_sleep)

    class _MultiChapSource(_StubSource):
        def __init__(self, source_id: SourceId, *, fail_on_ch2_pages: bool = False) -> None:
            super().__init__(source_id, page_count=2)
            self._fail_ch2 = fail_on_ch2_pages

        async def fetch_chapters(self, manga: MangaCandidate) -> list[ChapterInfo]:
            return [
                ChapterInfo(chapter_id="c1", title="第1话", number=1.0, kind=ChapterKind.CHAPTER),
                ChapterInfo(chapter_id="c2", title="第2话", number=2.0, kind=ChapterKind.CHAPTER),
                ChapterInfo(chapter_id="c3", title="第3话", number=3.0, kind=ChapterKind.CHAPTER),
            ]

        async def fetch_chapter_pages(
            self, manga: MangaCandidate, chapter: ChapterInfo
        ) -> list[PageSpec]:
            if self._fail_ch2 and chapter.chapter_id == "c2":
                raise SourceThrottleError("第2话主源临时故障")
            return await super().fetch_chapter_pages(manga, chapter)

    plugin = MangaForwardPlugin(
        context=object(),  # type: ignore[arg-type]
        config={
            "source_priority": ["zaimanhua", "copymanga"],
            "comments": {"enable_comments": False},
            "delivery": {"max_batch_chapters": 5, "send_wait_tip": False},
        },  # type: ignore[arg-type]
    )
    plugin._orchestrator._downloader = ChapterImageDownloader(_DelayedImageHttp())  # type: ignore[arg-type]
    plugin._orchestrator._sources[SourceId.ZAIMANHUA] = _MultiChapSource(
        SourceId.ZAIMANHUA, fail_on_ch2_pages=True
    )
    plugin._orchestrator._sources[SourceId.COPYMANGA] = _MultiChapSource(SourceId.COPYMANGA)

    await plugin.initialize()
    try:
        ev = _DummyEvent("aiocqhttp", "/manga 电锯人 1-3话")
        replies = [r async for r in plugin.cmd_manga(ev)]  # type: ignore[arg-type]
        assert replies == []
        # 连续发出 3 条合并转发（第1话走再漫画、第2话自动回退拷贝漫画补偿、第3话继续走再漫画）
        assert len(ev.sent_chains) == 3
        headers = [chain.chain[0].nodes[0].content[0].text for chain in ev.sent_chains]
        assert "第1话 〔批量 1/3〕" in headers[0] and "来源：再漫画" in headers[0]
        assert "第2话 〔批量 2/3〕" in headers[1] and "来源：拷贝漫画" in headers[1]
        assert "第3话 〔批量 3/3〕" in headers[2] and "来源：再漫画" in headers[2]
    finally:
        await plugin.terminate()
