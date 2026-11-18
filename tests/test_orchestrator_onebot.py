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
    # 配置优先级：拷贝漫画 -> 再漫画 -> Komiic
    # 其中拷贝漫画触发限流，自动降级至再漫画并成功拉取 4 页图片 + 章末吐槽
    raw_cfg = {
        "source_priority": ["copymanga", "zaimanhua", "komiic"],
        "comments": {"enable_comments": True, "max_comments": 10},
        "delivery": {"include_header_node": True, "max_pages_per_forward": 10},
    }
    orch = MangaForwardOrchestrator(tmp_path, lambda: raw_cfg)
    orch._downloader = ChapterImageDownloader(_DelayedImageHttp())  # type: ignore[arg-type]
    orch._sources = {
        SourceId.COPYMANGA: _StubSource(
            SourceId.COPYMANGA, fail_exc=SourceThrottleError("code=210 限流")
        ),
        SourceId.ZAIMANHUA: _StubSource(
            SourceId.ZAIMANHUA,
            comments=[ChapterComment(content="神回预定！", likes=42)],
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
        )
        assert bundle.source == SourceId.ZAIMANHUA
        assert [p.index for p in bundle.downloaded_pages] == [0, 1, 2, 3]
        # 验证落盘文件字节严格对应 PAGE_1 .. PAGE_4，绝无因并发完成先后而乱序
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
        # 1 个概览头节点 + 4 个单图节点（每张图独占一条消息） + 1 个章末吐槽节点 = 6 个节点
        assert len(nodes) == 6
        assert isinstance(nodes[0].content[0], Comp.Plain)
        for img_idx in range(1, 5):
            assert len(nodes[img_idx].content) == 1
            assert isinstance(nodes[img_idx].content[0], Comp.Image)
            assert f"({img_idx}/4)" in (nodes[img_idx].name or "")
        assert isinstance(nodes[5].content[0], Comp.Plain)
        assert "神回预定！" in nodes[5].content[0].text
    finally:
        await scope.cleanup()
        await orch.close()
    assert not scope.path.exists()


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
