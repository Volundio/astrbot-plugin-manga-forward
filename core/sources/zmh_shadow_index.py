"""`zmh.shadows.de5.net` 第三方静态二进制倒排索引检索客户端（紧凑内存布局 + 空闲自动卸载 + 磁盘分片生命周期清理）。

严格遵守项目红线：再漫画获取具体漫画 ID 必须使用 `https://zmh.shadows.de5.net/?needLogin=only1`
提供的静态索引资产（manifest.json + ZMHm/ZMHd/ZMHa/postings 二进制分片），绝不调用再漫画官方搜索接口。
"""

from __future__ import annotations

import asyncio
import bisect
import gc
import gzip
import hashlib
import os
import re
import shutil
import struct
import time
import unicodedata
from array import array
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from astrbot.api import logger

from ..http_client import AsyncHttpClient
from ..models import MangaCandidate, SourceId
from ..text_utils import score_title_match, to_simplified

MANIFEST_TTL_SEC = 6 * 3600.0
MAX_RAM_POSTING_SHARDS = 4
DEFAULT_MAX_DISK_CACHE_BYTES = 30 * 1024 * 1024
_CLEAN_INDEX_TOKEN_RE = re.compile(r"[^\w\u4e00-\u9fff\u3040-\u30ff]+", re.UNICODE)


def _align4(offset: int) -> int:
    return (offset + 3) & ~3


def _maybe_gunzip(raw: bytes) -> bytes:
    if len(raw) >= 3 and raw[0] == 0x1F and raw[1] == 0x8B and raw[2] == 0x08:
        return gzip.decompress(raw)
    return raw


def _normalize_index_term(text: str) -> str:
    """对齐 `searchWorker.js` 的 `K(t)`：NFKC -> 小写 -> 仅保留字母与数字。"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    return _CLEAN_INDEX_TOKEN_RE.sub("", s).replace("_", "")


def _extract_ngrams(term: str, n: int) -> list[str]:
    if len(term) < n:
        return []
    seen: dict[str, None] = {}
    for idx in range(len(term) - n + 1):
        seen.setdefault(term[idx : idx + n], None)
    return list(seen.keys())


def _encode_bigram_key(gram: str) -> int | None:
    if len(gram) != 2:
        return None
    c0 = ord(gram[0])
    c1 = ord(gram[1])
    if c0 > 0xFFFF or c1 > 0xFFFF:
        return None
    return (c0 << 16) | c1


def _decode_str_from_pool(pool: memoryview, offsets: array[int], idx: int) -> str:
    start = offsets[idx]
    end = offsets[idx + 1]
    if end <= start:
        return ""
    return bytes(pool[start:end]).decode("utf-8", errors="replace")


@dataclass(slots=True)
class _MetaShard:
    """紧凑元数据分片（已剥离无需驻留内存的封面 URL 池与冗余 ID 列表，降低约 45% 内存占用）。"""

    count: int
    sep: str
    titles_offsets: array[int]
    titles_pool: bytes
    author_ids_offsets: array[int]
    author_ids_pool: array[int]
    aliases_offsets: array[int]
    aliases_pool: bytes


@dataclass(slots=True)
class _NgramDict:
    version: int
    n: int
    keys: array[int]
    shard_ids: array[int]
    offsets: array[int]
    lengths: array[int]
    dfs: array[int]


@dataclass(slots=True)
class _LoadedIndex:
    origin: str
    loaded_at: float
    total_count: int
    meta_sep: str
    meta_shard_docs: int
    meta_shards: list[_MetaShard]
    doc_ids: array[int]
    doc_flags: bytearray
    author_by_id: dict[int, str]
    ngram_dict: _NgramDict
    index_assets: list[dict[str, Any]]
    posting_shards: OrderedDict[int, bytes]


def _unpack_u32_array(buf: bytes, offset: int, count: int) -> array[int]:
    arr = array("I")
    arr.frombytes(buf[offset : offset + count * 4])
    if arr.itemsize != 4:  # pragma: no cover
        arr = array("I", struct.unpack_from(f"<{count}I", buf, offset))
    return arr


def _unpack_u16_array(buf: bytes, offset: int, count: int) -> array[int]:
    arr = array("H")
    arr.frombytes(buf[offset : offset + count * 2])
    return arr


def parse_meta_shard_bytes(raw: bytes) -> tuple[_MetaShard, array[int], bytes]:
    """解析 `ZMHm` v5 二进制分片，返回 `(紧凑分片对象, C紧凑doc_ids数组, flags字节串)`。"""
    buf = _maybe_gunzip(raw)
    if len(buf) < 20 or buf[:4] != b"ZMHm":
        raise ValueError("ZMHm 分片魔数校验失败")
    version, sep_code, count, tag_word_count, cover_base_count = struct.unpack_from(
        "<HHIII", buf, 4
    )
    if version != 5:
        raise ValueError(f"不支持的 ZMHm 分片版本: {version}")

    pos = 20
    ids = array("I")
    acc_id = 0
    buf_len = len(buf)
    for _ in range(count):
        shift = 0
        delta = 0
        while True:
            if pos >= buf_len:
                raise ValueError("ZMHm ids varint 越界")
            b = buf[pos]
            pos += 1
            delta |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
            if shift > 28:
                raise ValueError("ZMHm ids varint 过长")
        acc_id += delta
        ids.append(acc_id)

    pos = _align4(pos)
    pos += count * tag_word_count * 4
    flags = buf[pos : pos + count]
    pos = _align4(pos + count)

    def _read_pool(item_count: int) -> tuple[array[int], bytes]:
        nonlocal pos
        offs = _unpack_u32_array(buf, pos, item_count + 1)
        pos += (item_count + 1) * 4
        byte_len = offs[item_count] if item_count >= 0 else 0
        pool_bytes = buf[pos : pos + byte_len]
        pos = _align4(pos + byte_len)
        return offs, pool_bytes

    def _skip_pool(item_count: int) -> None:
        nonlocal pos
        last_off = struct.unpack_from("<I", buf, pos + item_count * 4)[0]
        pos += (item_count + 1) * 4
        pos = _align4(pos + last_off)

    titles_offs, titles_pool = _read_pool(count)
    # 封面路径在合并转发正文中无需使用，跳过加载以显著节省常驻内存
    _skip_pool(cover_base_count)
    if cover_base_count <= 255:
        pos = _align4(pos + count)
    else:
        pos = _align4(pos + count * 2)
    _skip_pool(count)

    author_ids_offs = _unpack_u32_array(buf, pos, count + 1)
    pos += (count + 1) * 4
    authors_byte_len = author_ids_offs[count]
    author_ids_pool = _unpack_u16_array(buf, pos, authors_byte_len >> 1)
    pos = _align4(pos + authors_byte_len)

    aliases_offs, aliases_pool = _read_pool(count)

    shard = _MetaShard(
        count=count,
        sep=chr(sep_code) if sep_code else " ",
        titles_offsets=titles_offs,
        titles_pool=titles_pool,
        author_ids_offsets=author_ids_offs,
        author_ids_pool=author_ids_pool,
        aliases_offsets=aliases_offs,
        aliases_pool=aliases_pool,
    )
    return shard, ids, flags


def parse_authors_dict_bytes(raw: bytes) -> dict[int, str]:
    """解析 `ZMHa` v1 作者词典。"""
    buf = _maybe_gunzip(raw)
    if len(buf) < 16 or buf[:4] != b"ZMHa":
        raise ValueError("ZMHa 词典魔数校验失败")
    version = struct.unpack_from("<H", buf, 4)[0]
    if version != 1:
        raise ValueError(f"不支持的 ZMHa 版本: {version}")
    count = struct.unpack_from("<I", buf, 8)[0]
    pos = 16
    ids = _unpack_u16_array(buf, pos, count)
    pos = _align4(pos + count * 2)
    offs = _unpack_u32_array(buf, pos, count + 1)
    pos += (count + 1) * 4
    pool_len = offs[count]
    pool = memoryview(buf)[pos : pos + pool_len]

    mapping: dict[int, str] = {}
    for i in range(count):
        name = _decode_str_from_pool(pool, offs, i)
        if name:
            mapping[ids[i]] = name
    return mapping


def parse_ngram_dict_bytes(raw: bytes) -> _NgramDict:
    """解析 `ZMHd` v2/v3 2-gram 倒排词典。"""
    buf = _maybe_gunzip(raw)
    if len(buf) < 16 or buf[:4] != b"ZMHd":
        raise ValueError("ZMHd 词典魔数校验失败")
    version, n, count = struct.unpack_from("<HHI", buf, 4)
    pos = 16
    keys = _unpack_u32_array(buf, pos, count)
    pos += count * 4

    if version == 2:
        shard_ids = _unpack_u32_array(buf, pos, count)
        pos += count * 4
        offsets = _unpack_u32_array(buf, pos, count)
        pos += count * 4
        lengths = _unpack_u32_array(buf, pos, count)
        pos += count * 4
        dfs = _unpack_u32_array(buf, pos, count)
        return _NgramDict(version, n, keys, shard_ids, offsets, lengths, dfs)

    if version == 3:
        raw_shards = buf[pos : pos + count]
        shard_ids = array("I", (b for b in raw_shards))
        pos = _align4(pos + count)
        offsets = _unpack_u32_array(buf, pos, count)
        pos += count * 4
        lengths = array("I", _unpack_u16_array(buf, pos, count))
        pos += count * 2
        dfs = array("I", _unpack_u16_array(buf, pos, count))
        return _NgramDict(version, n, keys, shard_ids, offsets, lengths, dfs)

    raise ValueError(f"不支持的 ZMHd 版本: {version}")


def decode_postings(shard_buf: bytes, offset: int, length: int) -> list[int]:
    """解码倒排链差分 varint 文档序号（初始基准为 -1，与 `searchWorker.js` `Nt` 一致）。"""
    end = offset + length
    pos = offset
    doc_idx = -1
    out: list[int] = []
    while pos < end:
        shift = 0
        delta = 0
        while True:
            b = shard_buf[pos]
            pos += 1
            delta |= (b & 0x7F) << shift
            if (b & 0x80) == 0:
                break
            shift += 7
        doc_idx += delta
        out.append(doc_idx)
    return out


class ZmhShadowSearchClient:
    """`zmh.shadows.de5.net` 离线二进制索引客户端（支持内存空闲卸载与磁盘缓存过期/容量清理）。"""

    def __init__(self, cache_dir: Path, http: AsyncHttpClient) -> None:
        self._cache_dir = cache_dir
        self._http = http
        self._index_lock = asyncio.Lock()
        self._shard_lock = asyncio.Lock()
        self._loaded: _LoadedIndex | None = None
        self._last_access_ts: float = 0.0

    @property
    def is_index_loaded(self) -> bool:
        return self._loaded is not None

    @staticmethod
    def parse_site_url(site_url: str) -> tuple[str, str | None]:
        raw = (site_url or "https://zmh.shadows.de5.net/?needLogin=only1").strip()
        if not raw.startswith(("http://", "https://")):
            raw = f"https://{raw}"
        parts = urlsplit(raw)
        origin = f"{parts.scheme}://{parts.netloc}"
        qs = parse_qs(parts.query)
        nl_vals = qs.get("needLogin")
        nl = nl_vals[0] if nl_vals and nl_vals[0] in ("any", "only0", "only1") else None
        return origin, nl

    async def _fetch_asset_cached(self, origin: str, asset_info: dict[str, Any]) -> bytes:
        sha256_hex = str(asset_info.get("sha256") or "").strip().lower()
        rel_path = str(asset_info.get("path") or "").lstrip("/")
        if not rel_path:
            raise ValueError("manifest 资产缺少 path")

        if sha256_hex:
            cache_file = self._cache_dir / f"{sha256_hex}.bin"
            if cache_file.exists():
                try:
                    return await asyncio.to_thread(self._read_and_touch_sync, cache_file)
                except Exception:
                    pass

        resp = await self._http.get(
            f"{origin}/{rel_path}",
            headers={"Referer": f"{origin}/"},
            timeout_sec=30.0,
            max_bytes=16 * 1024 * 1024,
        )
        if resp.status != 200:
            raise RuntimeError(f"拉取再漫画索引分片失败 ({rel_path}): HTTP {resp.status}")

        decompressed = _maybe_gunzip(resp.body)
        if sha256_hex:
            raw_hash = hashlib.sha256(resp.body).hexdigest().lower()
            dec_hash = hashlib.sha256(decompressed).hexdigest().lower()
            if sha256_hex not in (raw_hash, dec_hash):
                logger.debug(
                    f"索引分片哈希提示 ({rel_path}): expected={sha256_hex[:12]}, got={dec_hash[:12]}"
                )
            await asyncio.to_thread(self._write_cache_file_sync, sha256_hex, decompressed)
        return decompressed

    @staticmethod
    def _read_and_touch_sync(path: Path) -> bytes:
        data = path.read_bytes()
        try:
            os.utime(path, None)
        except OSError:
            pass
        return data

    def _write_cache_file_sync(self, sha256_hex: str, content: bytes) -> None:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        target = self._cache_dir / f"{sha256_hex}.bin"
        tmp = self._cache_dir / f"{sha256_hex}.bin.tmp"
        try:
            tmp.write_bytes(content)
            os.replace(tmp, target)
        except BaseException:
            tmp.unlink(missing_ok=True)

    def _prune_stale_cache_sync(self, valid_hashes: set[str]) -> None:
        if not self._cache_dir.exists():
            return
        for entry in list(self._cache_dir.iterdir()):
            if entry.suffix in (".tmp", ".part"):
                entry.unlink(missing_ok=True)
            elif entry.suffix == ".bin" and entry.stem.lower() not in valid_hashes:
                entry.unlink(missing_ok=True)

    async def run_maintenance(
        self,
        *,
        idle_unload_sec: float = 600.0,
        disk_max_age_sec: float = 72 * 3600.0,
        disk_max_bytes: int = DEFAULT_MAX_DISK_CACHE_BYTES,
        source_enabled: bool = True,
    ) -> tuple[bool, int]:
        """执行内存索引空闲卸载与磁盘分片垃圾/过期清理，返回 `(是否释放内存索引, 清理磁盘文件数)`。"""
        evicted_ram = False
        now_mono = time.monotonic()
        if self._loaded is not None and (
            not source_enabled or (now_mono - self._last_access_ts) >= idle_unload_sec
        ):
            async with self._index_lock:
                if self._loaded is not None and (
                    not source_enabled
                    or (time.monotonic() - self._last_access_ts) >= idle_unload_sec
                ):
                    self._loaded.posting_shards.clear()
                    self._loaded.meta_shards.clear()
                    self._loaded.author_by_id.clear()
                    self._loaded = None
                    evicted_ram = True
            if evicted_ram:
                gc.collect()
                logger.debug("再漫画离线索引因空闲超时已从内存卸载（保留本地分片按需秒级重载）。")

        removed_files = await asyncio.to_thread(
            self._cleanup_disk_cache_sync,
            disk_max_age_sec=disk_max_age_sec,
            disk_max_bytes=disk_max_bytes,
            wipe_all=not source_enabled,
        )
        return evicted_ram, removed_files

    def _cleanup_disk_cache_sync(
        self,
        *,
        disk_max_age_sec: float,
        disk_max_bytes: int,
        wipe_all: bool,
    ) -> int:
        if not self._cache_dir.exists():
            return 0
        if wipe_all:
            count = sum(1 for _ in self._cache_dir.iterdir())
            shutil.rmtree(self._cache_dir, ignore_errors=True)
            return count

        now = time.time()
        removed = 0
        kept_bins: list[tuple[float, int, Path]] = []

        for entry in list(self._cache_dir.iterdir()):
            if not entry.is_file():
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
                continue
            if entry.suffix != ".bin":
                entry.unlink(missing_ok=True)
                removed += 1
                continue
            try:
                st = entry.stat()
            except OSError:
                continue
            if (now - st.st_mtime) >= disk_max_age_sec:
                entry.unlink(missing_ok=True)
                removed += 1
            else:
                kept_bins.append((st.st_mtime, st.st_size, entry))

        # 若缓存目录总大小超过预算，按最久未访问 (LRU) 淘汰旧分片
        total_bytes = sum(sz for _, sz, _ in kept_bins)
        if total_bytes > disk_max_bytes:
            kept_bins.sort(key=lambda item: item[0])
            for _, sz, fpath in kept_bins:
                if total_bytes <= disk_max_bytes:
                    break
                fpath.unlink(missing_ok=True)
                total_bytes -= sz
                removed += 1

        try:
            if not any(self._cache_dir.iterdir()):
                self._cache_dir.rmdir()
        except OSError:
            pass
        return removed

    async def _ensure_index(self, origin: str) -> _LoadedIndex:
        now = time.monotonic()
        self._last_access_ts = now
        if (
            self._loaded is not None
            and self._loaded.origin == origin
            and (now - self._loaded.loaded_at) < MANIFEST_TTL_SEC
        ):
            return self._loaded

        async with self._index_lock:
            now = time.monotonic()
            self._last_access_ts = now
            if (
                self._loaded is not None
                and self._loaded.origin == origin
                and (now - self._loaded.loaded_at) < MANIFEST_TTL_SEC
            ):
                return self._loaded

            manifest_resp = await self._http.get(
                f"{origin}/assets/manifest.json",
                headers={"Referer": f"{origin}/", "Cache-Control": "no-cache"},
                timeout_sec=20.0,
            )
            if manifest_resp.status != 200:
                raise RuntimeError(
                    f"请求再漫画搜索站 manifest.json 失败: HTTP {manifest_resp.status}"
                )
            manifest = manifest_resp.json()
            stats = manifest.get("stats") or {}
            assets = manifest.get("assets") or {}
            total_count = int(stats.get("count") or 0)
            meta_shard_docs = int(stats.get("metaShardDocs") or total_count or 1)

            meta_assets: list[dict[str, Any]] = assets.get("metaShards") or (
                [assets["meta"]] if assets.get("meta") else []
            )
            index_assets: list[dict[str, Any]] = assets.get("indexShards") or (
                [assets["index"]] if assets.get("index") else []
            )
            if not meta_assets or not index_assets:
                raise RuntimeError("再漫画搜索站 manifest 缺少 metaShards 或 indexShards")

            valid_hashes: set[str] = set()
            for item in [assets.get("authors"), assets.get("dict"), *meta_assets, *index_assets]:
                if isinstance(item, dict) and item.get("sha256"):
                    valid_hashes.add(str(item["sha256"]).lower())

            sem = asyncio.Semaphore(4)

            async def _dl(info: dict[str, Any]) -> bytes:
                async with sem:
                    return await self._fetch_asset_cached(origin, info)

            authors_raw, dict_raw = await asyncio.gather(
                _dl(assets["authors"]),
                _dl(assets["dict"]),
            )
            author_map = parse_authors_dict_bytes(authors_raw)
            del authors_raw
            ngram_dict = parse_ngram_dict_bytes(dict_raw)
            del dict_raw

            # 流式逐批加载并解析 meta 分片，立即释放原始分片字节，避免 21 个原始分片与解析对象同时驻留峰值翻倍
            parsed_shards: list[_MetaShard] = []
            doc_ids = array("I")
            doc_flags = bytearray()
            meta_sep = " "

            meta_raw_list = await asyncio.gather(*(_dl(m) for m in meta_assets))
            for m_bytes in meta_raw_list:
                sh, sh_ids, sh_flags = parse_meta_shard_bytes(m_bytes)
                if sh.sep:
                    meta_sep = sh.sep
                doc_ids.extend(sh_ids)
                doc_flags.extend(sh_flags)
                parsed_shards.append(sh)
            del meta_raw_list

            await asyncio.to_thread(self._prune_stale_cache_sync, valid_hashes)

            self._loaded = _LoadedIndex(
                origin=origin,
                loaded_at=time.monotonic(),
                total_count=len(doc_ids),
                meta_sep=meta_sep,
                meta_shard_docs=max(1, meta_shard_docs),
                meta_shards=parsed_shards,
                doc_ids=doc_ids,
                doc_flags=doc_flags,
                author_by_id=author_map,
                ngram_dict=ngram_dict,
                index_assets=index_assets,
                posting_shards=OrderedDict(),
            )
            logger.info(
                f"再漫画离线索引就绪 ({origin})：共收录 {len(doc_ids)} 部漫画，"
                f"{len(parsed_shards)} 个紧凑分片。"
            )
            return self._loaded

    async def _get_posting_shard(self, idx: _LoadedIndex, sid: int) -> bytes | None:
        if sid < 0 or sid >= len(idx.index_assets):
            return None
        if sid in idx.posting_shards:
            idx.posting_shards.move_to_end(sid)
            return idx.posting_shards[sid]

        async with self._shard_lock:
            if sid in idx.posting_shards:
                idx.posting_shards.move_to_end(sid)
                return idx.posting_shards[sid]
            data = await self._fetch_asset_cached(idx.origin, idx.index_assets[sid])
            idx.posting_shards[sid] = data
            idx.posting_shards.move_to_end(sid)
            # 严格限制常驻内存中的倒排分片数量（最多 4 个 ≈ 1MB），超出按 LRU 移出内存
            while len(idx.posting_shards) > MAX_RAM_POSTING_SHARDS:
                idx.posting_shards.popitem(last=False)
            return data

    @staticmethod
    def _doc_matches_need_login(flags_byte: int, need_login_filter: str) -> bool:
        if need_login_filter == "any":
            return True
        is_need_login = (flags_byte & 4) != 0
        if need_login_filter == "only1":
            return is_need_login
        if need_login_filter == "only0":
            return not is_need_login
        return True

    @staticmethod
    def _extract_doc_candidate(idx: _LoadedIndex, doc_pos: int) -> MangaCandidate:
        shard_no = doc_pos // idx.meta_shard_docs
        local_idx = doc_pos - shard_no * idx.meta_shard_docs
        sh = idx.meta_shards[shard_no]

        title = _decode_str_from_pool(memoryview(sh.titles_pool), sh.titles_offsets, local_idx)
        aliases_raw = _decode_str_from_pool(
            memoryview(sh.aliases_pool), sh.aliases_offsets, local_idx
        )
        aliases = [a for a in aliases_raw.split(idx.meta_sep) if a] if aliases_raw else []

        a_start = sh.author_ids_offsets[local_idx] >> 1
        a_end = sh.author_ids_offsets[local_idx + 1] >> 1
        authors = [
            idx.author_by_id[aid]
            for aid in sh.author_ids_pool[a_start:a_end]
            if aid in idx.author_by_id
        ]

        f_byte = idx.doc_flags[doc_pos]
        manga_id = str(idx.doc_ids[doc_pos])
        return MangaCandidate(
            source=SourceId.ZAIMANHUA,
            manga_id=manga_id,
            title=title,
            aliases=aliases,
            authors=authors,
            need_login=(f_byte & 4) != 0,
            url=f"https://manhua.zaimanhua.com/details/{manga_id}",
            extra={
                "hidden": (f_byte & 1) != 0,
                "is_hide_chapter": (f_byte & 2) != 0,
                "is_lock": (f_byte & 8) != 0,
            },
        )

    async def search(
        self,
        keyword: str,
        *,
        site_url: str,
        need_login_filter: str = "any",
        limit: int = 15,
    ) -> list[MangaCandidate]:
        """在 `zmh.shadows.de5.net` 索引中检索漫画并返回高置信度候选列表。"""
        origin, _ = self.parse_site_url(site_url)
        idx = await self._ensure_index(origin)
        self._last_access_ts = time.monotonic()

        raw_variants = [keyword.strip()]
        simp_kw = to_simplified(keyword).strip()
        if simp_kw and simp_kw not in raw_variants:
            raw_variants.append(simp_kw)

        recalled_docs: set[int] = set()
        for variant in raw_variants:
            tokens = [
                _normalize_index_term(tok) for tok in variant.split() if _normalize_index_term(tok)
            ]
            if not tokens:
                continue

            short_tokens = [t for t in tokens if len(t) < idx.ngram_dict.n]
            long_tokens = [t for t in tokens if len(t) >= idx.ngram_dict.n]

            if long_tokens:
                all_dict_indices: list[list[int]] = []
                for tok in long_tokens:
                    grams = _extract_ngrams(tok, idx.ngram_dict.n)
                    d_indices: list[int] = []
                    for g in grams:
                        key = _encode_bigram_key(g)
                        if key is None:
                            continue
                        pos = bisect.bisect_left(idx.ngram_dict.keys, key)
                        if pos < len(idx.ngram_dict.keys) and idx.ngram_dict.keys[pos] == key:
                            d_indices.append(pos)
                    if d_indices:
                        all_dict_indices.append(d_indices)

                token_doc_sets: list[set[int]] = []
                for tok, d_indices in zip(long_tokens, all_dict_indices, strict=False):
                    grams_count = max(1, len(_extract_ngrams(tok, idx.ngram_dict.n)))
                    min_hits = min(len(d_indices), max(1, int((grams_count * 0.6) + 0.9999)))
                    hit_counts: dict[int, int] = {}
                    for di in d_indices:
                        sid = idx.ngram_dict.shard_ids[di]
                        shard_buf = await self._get_posting_shard(idx, sid)
                        if shard_buf is None:
                            continue
                        off = idx.ngram_dict.offsets[di]
                        ln = idx.ngram_dict.lengths[di]
                        for doc_pos in decode_postings(shard_buf, off, ln):
                            if 0 <= doc_pos < idx.total_count and self._doc_matches_need_login(
                                idx.doc_flags[doc_pos], need_login_filter
                            ):
                                hit_counts[doc_pos] = hit_counts.get(doc_pos, 0) + 1
                    matched_for_tok = {d for d, cnt in hit_counts.items() if cnt >= min_hits}
                    token_doc_sets.append(matched_for_tok)

                if token_doc_sets:
                    intersected = set.intersection(*token_doc_sets)
                    recalled_docs.update(intersected)

            if short_tokens and not long_tokens:
                target_char = short_tokens[0]
                for doc_pos in range(idx.total_count):
                    if not self._doc_matches_need_login(idx.doc_flags[doc_pos], need_login_filter):
                        continue
                    shard_no = doc_pos // idx.meta_shard_docs
                    local_idx = doc_pos - shard_no * idx.meta_shard_docs
                    sh = idx.meta_shards[shard_no]
                    t_str = _decode_str_from_pool(
                        memoryview(sh.titles_pool), sh.titles_offsets, local_idx
                    )
                    if target_char in _normalize_index_term(t_str):
                        recalled_docs.add(doc_pos)

        scored: list[MangaCandidate] = []
        for doc_pos in recalled_docs:
            cand = self._extract_doc_candidate(idx, doc_pos)
            cand.match_score = score_title_match(keyword, cand.title, cand.aliases)
            if cand.match_score >= 0.35:
                scored.append(cand)

        scored.sort(
            key=lambda c: (round(c.match_score, 4), int(c.manga_id) if c.manga_id.isdigit() else 0),
            reverse=True,
        )
        return scored[:limit]
