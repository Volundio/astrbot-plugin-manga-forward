"""`zmh.shadows.de5.net` 静态二进制索引格式解码、缓存与过滤测试。"""

from __future__ import annotations

import gzip
import hashlib
import json
import struct
from pathlib import Path
from typing import Any

import pytest

from core.http_client import HttpResponseData
from core.sources.zmh_shadow_index import ZmhShadowSearchClient


def _encode_varint(val: int) -> bytes:
    out = bytearray()
    v = val
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            break
    return bytes(out)


def _pad4(buf: bytearray) -> None:
    while len(buf) % 4 != 0:
        buf.append(0)


def _write_pool(buf: bytearray, strings: list[str]) -> None:
    encoded = [s.encode("utf-8") for s in strings]
    offsets = [0]
    for b in encoded:
        offsets.append(offsets[-1] + len(b))
    for off in offsets:
        buf.extend(struct.pack("<I", off))
    for b in encoded:
        buf.extend(b)
    _pad4(buf)


def _build_synthetic_zmh_index() -> dict[str, bytes]:
    """按 `searchWorker.js` 规范构造完整的内存二进制索引资产 (ZMHa + ZMHm v5 + ZMHd v3 + postings)。"""
    # 1. ZMHa v1: authors (1 -> 藤本树, 2 -> 山田钟人)
    authors_buf = bytearray(b"ZMHa")
    authors_buf.extend(struct.pack("<HHI", 1, 0, 2))
    authors_buf.extend(struct.pack("<I", 0))  # pad to 16 bytes
    authors_buf.extend(struct.pack("<HH", 1, 2))
    _pad4(authors_buf)
    _write_pool(authors_buf, ["藤本树", "山田钟人"])

    # 2. ZMHm v5: 2 docs
    # doc 0: id=46598, title="电锯人", alias="链锯人", needLogin=True (flag=4), author=[1]
    # doc 1: id=57069, title="爱情是烤肉的滋味", alias="", needLogin=False (flag=0), author=[2]
    meta_buf = bytearray(b"ZMHm")
    meta_buf.extend(struct.pack("<HHIII", 5, ord("|"), 2, 1, 1))
    meta_buf.extend(_encode_varint(46598))
    meta_buf.extend(_encode_varint(57069 - 46598))
    _pad4(meta_buf)
    # tagBits (2 * 1 uint32)
    meta_buf.extend(struct.pack("<II", 0, 0))
    # flags: doc0=4 (needLogin), doc1=0
    meta_buf.extend(bytes([4, 0]))
    _pad4(meta_buf)
    # titles pool
    _write_pool(meta_buf, ["电锯人", "爱情是烤肉的滋味"])
    # cover bases pool (1 item: index 0 unused or base)
    _write_pool(meta_buf, ["https://images.zaimanhua.com/"])
    # cover_base_ids (count=2, <=255 -> uint8)
    meta_buf.extend(bytes([0, 0]))
    _pad4(meta_buf)
    # cover_paths pool
    _write_pool(meta_buf, ["a.jpg", "b.jpg"])
    # author_ids_offsets (byte offsets into uint16 pool)
    meta_buf.extend(struct.pack("<III", 0, 2, 4))
    meta_buf.extend(struct.pack("<HH", 1, 2))
    _pad4(meta_buf)
    # aliases pool
    _write_pool(meta_buf, ["链锯人|ChainsawMan", "烤肉"])

    # 3. Postings & ZMHd v3
    # Build 2-grams for doc 0 and doc 1
    doc_grams = {
        0: ["电锯", "锯人", "链锯"],
        1: ["爱情", "烤肉", "滋味"],
    }
    gram_to_docs: dict[int, list[int]] = {}
    for doc_idx, grams in doc_grams.items():
        for g in grams:
            key = (ord(g[0]) << 16) | ord(g[1])
            gram_to_docs.setdefault(key, []).append(doc_idx)

    sorted_keys = sorted(gram_to_docs.keys())
    postings_buf = bytearray()
    offsets: list[int] = []
    lengths: list[int] = []
    dfs: list[int] = []

    for k in sorted_keys:
        docs = sorted(gram_to_docs[k])
        start_off = len(postings_buf)
        prev = -1
        for d in docs:
            postings_buf.extend(_encode_varint(d - prev))
            prev = d
        offsets.append(start_off)
        lengths.append(len(postings_buf) - start_off)
        dfs.append(len(docs))

    dict_buf = bytearray(b"ZMHd")
    dict_buf.extend(struct.pack("<HHI", 3, 2, len(sorted_keys)))
    dict_buf.extend(struct.pack("<I", 0))  # pad header to 16 bytes
    for k in sorted_keys:
        dict_buf.extend(struct.pack("<I", k))
    # shard_ids (all shard 0)
    dict_buf.extend(bytes([0] * len(sorted_keys)))
    _pad4(dict_buf)
    for off in offsets:
        dict_buf.extend(struct.pack("<I", off))
    for ln in lengths:
        dict_buf.extend(struct.pack("<H", ln))
    for df in dfs:
        dict_buf.extend(struct.pack("<H", df))

    def _asset(path: str, raw: bytes, compress: bool = False) -> tuple[dict[str, Any], bytes]:
        sha = hashlib.sha256(raw).hexdigest()
        wire = gzip.compress(raw) if compress else raw
        return {"path": path, "sha256": sha, "bytes": len(raw)}, wire

    a_info, a_wire = _asset("assets/authors.bin", bytes(authors_buf))
    d_info, d_wire = _asset("assets/dict.bin", bytes(dict_buf), compress=True)
    m_info, m_wire = _asset("assets/meta0.bin", bytes(meta_buf), compress=True)
    p_info, p_wire = _asset("assets/index0.bin", bytes(postings_buf))

    manifest = {
        "generatedAt": "2026-11-18T00:00:00Z",
        "stats": {"count": 2, "metaShardDocs": 2},
        "assets": {
            "authors": a_info,
            "dict": d_info,
            "metaShards": [m_info],
            "indexShards": [p_info],
        },
    }

    return {
        "https://zmh.shadows.de5.net/assets/manifest.json": json.dumps(manifest).encode("utf-8"),
        f"https://zmh.shadows.de5.net/{a_info['path']}": a_wire,
        f"https://zmh.shadows.de5.net/{d_info['path']}": d_wire,
        f"https://zmh.shadows.de5.net/{m_info['path']}": m_wire,
        f"https://zmh.shadows.de5.net/{p_info['path']}": p_wire,
    }


class _FakeShadowHttp:
    def __init__(self, routes: dict[str, bytes]) -> None:
        self.routes = routes
        self.requested_urls: list[str] = []

    async def get(self, url: str, **_: Any) -> HttpResponseData:
        self.requested_urls.append(url)
        assert "zaimanhua.com" not in url, "严禁请求再漫画官方搜索域名！"
        body = self.routes.get(url, b"")
        status = 200 if url in self.routes else 404
        return HttpResponseData(status=status, headers={}, cookies={}, body=body, url=url)


@pytest.mark.asyncio
async def test_zmh_shadow_search_and_need_login_filters(tmp_path: Path) -> None:
    routes = _build_synthetic_zmh_index()
    fake_http = _FakeShadowHttp(routes)
    # 预先写入一个过期无用分片，验证加载索引后自动清理旧缓存
    stale_file = tmp_path / ("deadbeef" * 8 + ".bin")
    stale_file.write_bytes(b"old")

    client = ZmhShadowSearchClient(tmp_path, fake_http)  # type: ignore[arg-type]

    # 1. 支持繁体词输入检索到简体再漫画隐藏词条 "电锯人" (id=46598, needLogin=True)
    res_any = await client.search(
        "電鋸人",
        site_url="https://zmh.shadows.de5.net/?needLogin=only1",
        need_login_filter="any",
    )
    assert len(res_any) == 1
    assert res_any[0].manga_id == "46598"
    assert res_any[0].title == "电锯人"
    assert res_any[0].need_login is True
    assert res_any[0].authors == ["藤本树"]
    assert not stale_file.exists()

    # 2. only1 模式下仅返回 needLogin=True 漫画，"爱情是烤肉的滋味" (needLogin=False) 被过滤
    res_only1 = await client.search(
        "烤肉",
        site_url="https://zmh.shadows.de5.net/?needLogin=only1",
        need_login_filter="only1",
    )
    assert res_only1 == []

    # 3. only0 模式下可检索到公开漫画 (id=57069)
    res_only0 = await client.search(
        "爱情是烤肉的滋味",
        site_url="https://zmh.shadows.de5.net/?needLogin=only1",
        need_login_filter="only0",
    )
    assert len(res_only0) == 1
    assert res_only0[0].manga_id == "57069"

    # 4. 单字短词回退扫描
    res_short = await client.search(
        "电",
        site_url="https://zmh.shadows.de5.net/?needLogin=only1",
        need_login_filter="any",
    )
    assert any(c.manga_id == "46598" for c in res_short)


@pytest.mark.asyncio
async def test_zmh_shadow_idle_ram_unload_and_disk_cache_cleanup(tmp_path: Path) -> None:
    import os
    import time

    routes = _build_synthetic_zmh_index()
    fake_http = _FakeShadowHttp(routes)
    client = ZmhShadowSearchClient(tmp_path, fake_http)  # type: ignore[arg-type]

    res1 = await client.search(
        "电锯人",
        site_url="https://zmh.shadows.de5.net/?needLogin=only1",
        need_login_filter="any",
    )
    assert len(res1) == 1
    assert client.is_index_loaded is True

    # 模拟异常中断遗留的 .tmp/.part 垃圾文件以及超龄旧分片
    tmp_garbage = tmp_path / "interrupted.bin.tmp"
    part_garbage = tmp_path / "half.part"
    expired_bin = tmp_path / ("cafe0000" * 8 + ".bin")
    tmp_garbage.write_bytes(b"trash")
    part_garbage.write_bytes(b"trash")
    expired_bin.write_bytes(b"expired_shard")
    old_ts = time.time() - 10 * 24 * 3600
    os.utime(expired_bin, (old_ts, old_ts))

    # 空闲超时触发内存索引卸载 + 垃圾与超龄分片清理
    evicted, removed = await client.run_maintenance(
        idle_unload_sec=0.0,
        disk_max_age_sec=72 * 3600.0,
        source_enabled=True,
    )
    assert evicted is True
    assert client.is_index_loaded is False
    assert removed == 3
    assert not tmp_garbage.exists()
    assert not part_garbage.exists()
    assert not expired_bin.exists()

    # 再次检索时自动从本地有效 .bin 缓存分片快速重载，无需重新通过网络拉取分片内容
    fake_http.requested_urls.clear()
    res2 = await client.search(
        "电锯人",
        site_url="https://zmh.shadows.de5.net/?needLogin=only1",
        need_login_filter="any",
    )
    assert len(res2) == 1
    assert client.is_index_loaded is True
    assert fake_http.requested_urls == ["https://zmh.shadows.de5.net/assets/manifest.json"]

    # 当用户禁用再漫画源时，自动彻底清空内存与本地索引目录
    evicted_off, _ = await client.run_maintenance(source_enabled=False)
    assert evicted_off is True
    assert client.is_index_loaded is False
    assert not tmp_path.exists()
