"""拷贝漫画 (CopyManga) 漫画源实现。

特性：
1. 内置多 API 域名容灾池与干净浏览器请求头（严禁发送旧版 App UA 触发防盗版 IP 限制）；
2. 支持简繁双词检索与高置信度匹配度打分；
3. 支持 REST v3 主通道 + Web 端 AES-128-CBC 解密双栈自动切换；
4. 严格依据 `/chapter2/` 接口的 `words` 映射表重排 `contents`，100% 还原真实漫画页序；
5. 支持拉取 `/api/v3/roasts` 单话评论/章末吐槽。
"""

from __future__ import annotations

import json
import re
from typing import Any

from astrbot.api import logger
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..auth_manager import format_cookie_header
from ..chapter_matcher import analyze_chapter_meta
from ..config import CopymangaConfig
from ..http_client import AsyncHttpClient
from ..models import (
    ChapterComment,
    ChapterInfo,
    MangaCandidate,
    MangaNotFoundError,
    PageSpec,
    SourceId,
    SourceThrottleError,
)
from ..text_utils import score_title_match, to_traditional
from .base import BaseMangaSource

COPYMANGA_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0"
)
FALLBACK_API_DOMAINS: tuple[str, ...] = (
    "api.mangacopy.com",
    "api.2026copy.com",
    "api.copy5000.com",
    "api.copy4000.com",
    "api.copy3000.com",
    "mapi.copy20.com",
    "api.copy2000.online",
)
FALLBACK_WEB_DOMAINS: tuple[str, ...] = (
    "www.mangacopy.com",
    "www.copy3000.com",
    "www.copy4000.com",
    "www.copy20.com",
)

_RESOLUTION_RE = re.compile(r"\.c\d+x\.(jpg|webp|png)", re.IGNORECASE)
_DNT_RE = re.compile(r'id=["\']dnt["\'][^>]*value=["\']([^"\']*)["\']', re.IGNORECASE)
_CCZ_VAR_RE = re.compile(r"var\s+\w+\s*=\s*['\"]([^'\"]{16})['\"]")
_CONTENT_KEY_RE = re.compile(r'contentKey\s*=\s*["\']([^"\']{32,})["\']')


def decrypt_copymanga_hex(encrypted_str: str, key_16: str) -> Any:
    """解密拷贝漫画 Web 端 AES-128-CBC (PKCS7) 十六进制载荷。"""
    raw = (encrypted_str or "").strip()
    if len(raw) <= 16 or len(key_16.encode("utf-8")) != 16:
        raise ValueError("拷贝漫画加密数据或密钥长度非法")
    iv = raw[:16].encode("utf-8")
    ciphertext = bytes.fromhex(raw[16:])
    cipher = Cipher(algorithms.AES(key_16.encode("utf-8")), modes.CBC(iv))
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    plain = unpadder.update(padded) + unpadder.finalize()
    return json.loads(plain.decode("utf-8"))


def reorder_copymanga_pages(
    contents: list[dict[str, Any]],
    words: list[int] | None,
    resolution: str = "1500",
) -> list[str]:
    """根据 `words` 真实页码数组重排 `contents`，确保拷贝漫画图片顺序绝对正确。"""
    if not contents:
        return []

    res = resolution if resolution in ("800", "1200", "1500") else "1500"

    def _upgrade_url(u: str) -> str:
        return _RESOLUTION_RE.sub(rf".c{res}x.\1", u)

    if isinstance(words, list) and len(words) == len(contents):
        indexed_pairs: list[tuple[int, int, str]] = []
        for orig_idx, (word_pos, item) in enumerate(zip(words, contents, strict=False)):
            url = str(item.get("url") or "").strip() if isinstance(item, dict) else ""
            if url:
                try:
                    sort_pos = int(word_pos)
                except (TypeError, ValueError):
                    sort_pos = orig_idx
                indexed_pairs.append((sort_pos, orig_idx, _upgrade_url(url)))
        indexed_pairs.sort(key=lambda t: (t[0], t[1]))
        return [t[2] for t in indexed_pairs]

    out: list[str] = []
    for item in contents:
        if isinstance(item, dict):
            url = str(item.get("url") or "").strip()
            if url:
                out.append(_upgrade_url(url))
    return out


class CopymangaSource(BaseMangaSource):
    """拷贝漫画数据源适配器。"""

    source_id = SourceId.COPYMANGA

    def __init__(self, cfg_getter: Any, http: AsyncHttpClient) -> None:
        self._get_cfg = cfg_getter
        self._http = http

    @property
    def cfg(self) -> CopymangaConfig:
        return self._get_cfg()

    def _api_domains(self) -> list[str]:
        seen: list[str] = []
        for dom in (self.cfg.api_domain, *FALLBACK_API_DOMAINS):
            d = (dom or "").strip()
            if d and d not in seen:
                seen.append(d)
        return seen

    def _web_domains(self) -> list[str]:
        seen: list[str] = []
        for dom in (self.cfg.web_domain, *FALLBACK_WEB_DOMAINS):
            d = (dom or "").strip()
            if d and d not in seen:
                seen.append(d)
        return seen

    def _api_headers(self, *, search_mode: bool = False) -> dict[str, str]:
        headers: dict[str, str] = {
            "User-Agent": COPYMANGA_BROWSER_UA,
            "Accept": "application/json",
            "Accept-Language": "zh-CN,zh;q=0.9,zh-TW;q=0.8,en;q=0.7",
            "webp": "1",
            "region": "1",
            "platform": "2" if search_mode else "1",
            "version": "2.3.0" if search_mode else "2026.08.21",
        }
        tok = self.cfg.token.strip()
        if tok:
            headers["Authorization"] = tok if tok.lower().startswith("token ") else f"Token {tok}"
        return headers

    async def _api_get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        search_mode: bool = False,
    ) -> tuple[dict[str, Any], str]:
        """轮询可用 API 域名请求 JSON，返回 `(payload, used_domain)`。"""
        throttled_msg = ""
        last_err = ""
        headers = self._api_headers(search_mode=search_mode)

        for dom in self._api_domains():
            url = f"https://{dom}{path}"
            try:
                resp = await self._http.get(
                    url,
                    params=params,
                    headers=headers,
                    timeout_sec=12.0,
                )
            except Exception as exc:
                last_err = str(exc)
                continue

            if resp.status == 210:
                try:
                    j = resp.json()
                    throttled_msg = str(j.get("message") or "源站触发频率或客户端限制 (210)")
                except Exception:
                    throttled_msg = "源站触发频率限制 (HTTP 210)"
                continue

            if resp.status != 200:
                last_err = f"HTTP {resp.status}"
                continue

            try:
                payload = resp.json()
            except Exception:
                last_err = "响应非合法 JSON"
                continue

            code = payload.get("code")
            if code == 210:
                throttled_msg = str(payload.get("message") or "源站触发频控限制 (code=210)")
                continue
            if code == 200 and isinstance(payload.get("results"), dict):
                return payload, dom
            last_err = str(payload.get("message") or f"code={code}")

        if throttled_msg:
            raise SourceThrottleError(f"拷贝漫画 API 限流：{throttled_msg}")
        raise RuntimeError(f"拷贝漫画 API 请求失败 ({path})：{last_err or '无可用节点'}")

    async def search_manga(self, keyword: str) -> list[MangaCandidate]:
        queries = [keyword.strip()]
        trad_kw = to_traditional(keyword).strip()
        if trad_kw and trad_kw not in queries:
            queries.append(trad_kw)

        merged_by_id: dict[str, MangaCandidate] = {}
        for q in queries:
            payload, _ = await self._api_get_json(
                "/api/v3/search/comic",
                params={"limit": 20, "offset": 0, "q": q, "platform": 2},
                search_mode=True,
            )
            items = (payload.get("results") or {}).get("list") or []
            if not isinstance(items, list):
                continue

            for item in items:
                if not isinstance(item, dict):
                    continue
                pw = str(item.get("path_word") or "").strip()
                name = str(item.get("name") or "").strip()
                if not pw or not name:
                    continue
                alias_str = str(item.get("alias") or "")
                aliases = [a.strip() for a in alias_str.split(",") if a.strip()]
                raw_authors = item.get("author") or []
                authors = [
                    str(a.get("name") or "").strip()
                    for a in raw_authors
                    if isinstance(a, dict) and a.get("name")
                ]
                score = score_title_match(keyword, name, aliases)
                if score < 0.35:
                    continue
                cand = MangaCandidate(
                    source=SourceId.COPYMANGA,
                    manga_id=pw,
                    title=name,
                    aliases=aliases,
                    authors=authors,
                    cover_url=str(item.get("cover") or ""),
                    match_score=score,
                    url=f"https://{self.cfg.web_domain}/comic/{pw}",
                )
                prev = merged_by_id.get(pw)
                if prev is None or cand.match_score > prev.match_score:
                    merged_by_id[pw] = cand

            # 若原词已命中高置信度词条，无需再发第二次繁体查询
            if any(c.match_score >= 0.90 for c in merged_by_id.values()):
                break

        results = sorted(merged_by_id.values(), key=lambda c: c.match_score, reverse=True)
        if not results:
            raise MangaNotFoundError(f"未检索到与「{keyword}」匹配的漫画")
        return results[:12]

    async def get_manga_by_id(self, manga_id: str) -> MangaCandidate:
        return MangaCandidate(
            source=SourceId.COPYMANGA,
            manga_id=manga_id,
            title=manga_id,
            match_score=1.0,
            url=f"https://{self.cfg.web_domain}/comic/{manga_id}",
        )

    async def fetch_chapters(self, manga: MangaCandidate) -> list[ChapterInfo]:
        pw = manga.manga_id
        throttle_err: SourceThrottleError | None = None

        # 通道 1：REST v3 章节分页拉取
        try:
            groups: list[tuple[str, str]] = [("default", "默认")]
            try:
                detail_payload, _ = await self._api_get_json(f"/api/v3/comic2/{pw}")
                res_obj = detail_payload.get("results") or {}
                comic_obj = res_obj.get("comic") or {}
                if comic_obj.get("name") and manga.title == pw:
                    manga.title = str(comic_obj["name"])
                raw_groups = res_obj.get("groups")
                if isinstance(raw_groups, dict) and raw_groups:
                    groups = [
                        (
                            str(g_val.get("path_word") or g_key),
                            str(g_val.get("name") or g_key),
                        )
                        for g_key, g_val in raw_groups.items()
                        if isinstance(g_val, dict)
                    ]
            except SourceThrottleError:
                raise
            except Exception as exc:
                logger.debug(f"拷贝漫画 comic2 分组元数据回退 default ({pw}): {exc}")

            chapters: list[ChapterInfo] = []
            seq = 0
            for group_pw, group_title in groups:
                offset = 0
                limit = 100
                while True:
                    ch_payload, _ = await self._api_get_json(
                        f"/api/v3/comic/{pw}/group/{group_pw}/chapters",
                        params={"limit": limit, "offset": offset, "_update": "true"},
                    )
                    res_ch = ch_payload.get("results") or {}
                    ch_list = res_ch.get("list") or []
                    if not isinstance(ch_list, list) or not ch_list:
                        break
                    for item in ch_list:
                        if not isinstance(item, dict):
                            continue
                        uuid = str(item.get("uuid") or "").strip()
                        cname = str(item.get("name") or "").strip()
                        if not uuid or not cname:
                            continue
                        meta = analyze_chapter_meta(cname, group_title)
                        chapters.append(
                            ChapterInfo(
                                chapter_id=uuid,
                                title=cname,
                                number=meta.number,
                                part_number=meta.part_number,
                                sub_order=meta.sub_order,
                                is_trial=meta.is_trial,
                                kind=meta.kind,
                                group_name=group_title,
                                order_index=int(item.get("ordered") or item.get("index") or seq),
                                page_count=int(item.get("size") or 0),
                                url=f"https://{self.cfg.web_domain}/comic/{pw}/chapter/{uuid}",
                            )
                        )
                        seq += 1
                    total = int(res_ch.get("total") or 0)
                    offset += limit
                    if offset >= total or offset >= 3000:
                        break
            if chapters:
                return chapters
        except SourceThrottleError as exc:
            throttle_err = exc
            logger.info(f"拷贝漫画 REST API 频控，尝试切换 Web AES 解密通道 ({pw})…")
        except Exception as exc:
            logger.debug(f"拷贝漫画 REST 章节拉取未果，尝试 Web 解密通道 ({pw}): {exc}")

        # 通道 2：Web 端 `/comicdetail/{pw}/chapters` AES-128-CBC 解密兜底
        web_chapters = await self._fetch_chapters_via_web(manga)
        if web_chapters:
            return web_chapters

        if throttle_err is not None:
            raise throttle_err
        raise SourceThrottleError(f"拷贝漫画《{manga.title}》章节列表为空或源站受限")

    async def _fetch_chapters_via_web(self, manga: MangaCandidate) -> list[ChapterInfo]:
        pw = manga.manga_id
        for web_dom in self._web_domains():
            try:
                page_url = f"https://{web_dom}/comic/{pw}"
                html_resp = await self._http.get(
                    page_url,
                    headers={"User-Agent": COPYMANGA_BROWSER_UA},
                    timeout_sec=12.0,
                )
                if html_resp.status != 200:
                    continue
                html = html_resp.text()
                m_dnt = _DNT_RE.search(html)
                m_ccz = _CCZ_VAR_RE.search(html)
                if not m_dnt or not m_ccz:
                    continue

                cookies = dict(html_resp.cookies)
                cookies.setdefault("webp", "1")
                detail_resp = await self._http.get(
                    f"https://{web_dom}/comicdetail/{pw}/chapters",
                    headers={
                        "User-Agent": COPYMANGA_BROWSER_UA,
                        "Referer": page_url,
                        "dnts": m_dnt.group(1),
                        "X-Requested-With": "XMLHttpRequest",
                        "Cookie": format_cookie_header(cookies),
                    },
                    timeout_sec=12.0,
                )
                if detail_resp.status != 200:
                    continue
                detail_json = detail_resp.json()
                enc_results = str(detail_json.get("results") or "")
                if len(enc_results) <= 16:
                    continue
                decrypted = decrypt_copymanga_hex(enc_results, m_ccz.group(1))
                raw_groups = decrypted.get("groups") or {}
                chapters: list[ChapterInfo] = []
                seq = 0
                for g_key, g_val in raw_groups.items():
                    if not isinstance(g_val, dict):
                        continue
                    g_name = str(g_val.get("name") or g_key)
                    c_list = g_val.get("chapters")
                    if not isinstance(c_list, list):
                        continue
                    for item in c_list:
                        if not isinstance(item, dict):
                            continue
                        cid = str(item.get("id") or item.get("uuid") or "").strip()
                        cname = str(item.get("name") or "").strip()
                        if not cid or not cname:
                            continue
                        meta = analyze_chapter_meta(cname, g_name)
                        chapters.append(
                            ChapterInfo(
                                chapter_id=cid,
                                title=cname,
                                number=meta.number,
                                part_number=meta.part_number,
                                sub_order=meta.sub_order,
                                is_trial=meta.is_trial,
                                kind=meta.kind,
                                group_name=g_name,
                                order_index=seq,
                                url=f"https://{web_dom}/comic/{pw}/chapter/{cid}",
                            )
                        )
                        seq += 1
                if chapters:
                    return chapters
            except Exception as exc:
                logger.debug(f"拷贝漫画 Web 章节解密未成功 ({web_dom}/{pw}): {exc}")
        return []

    async def fetch_chapter_pages(
        self, manga: MangaCandidate, chapter: ChapterInfo
    ) -> list[PageSpec]:
        pw = manga.manga_id
        cid = chapter.chapter_id
        res = self.cfg.image_resolution
        throttle_err: SourceThrottleError | None = None

        # 通道 1：REST v3 `/chapter2/{uuid}`（含 `words` 页序重排）
        for endpoint in (
            f"/api/v3/comic/{pw}/chapter2/{cid}",
            f"/api/v3/comic/{pw}/chapter/{cid}",
        ):
            try:
                payload, _ = await self._api_get_json(endpoint)
                ch_obj = (payload.get("results") or {}).get("chapter") or {}
                contents = ch_obj.get("contents") or []
                words = ch_obj.get("words")
                ordered_urls = reorder_copymanga_pages(contents, words, resolution=res)
                if ordered_urls:
                    img_headers = {
                        "User-Agent": COPYMANGA_BROWSER_UA,
                        "Referer": f"https://{self.cfg.web_domain}/",
                    }
                    return [
                        PageSpec(index=i, url=u, headers=dict(img_headers))
                        for i, u in enumerate(ordered_urls)
                    ]
            except SourceThrottleError as exc:
                throttle_err = exc
                break
            except Exception as exc:
                logger.debug(f"拷贝漫画接口 {endpoint} 未返回图片: {exc}")

        # 通道 2：Web 端阅读页 `contentKey` AES-128-CBC 解密兜底
        for web_dom in self._web_domains():
            try:
                ch_url = f"https://{web_dom}/comic/{pw}/chapter/{cid}"
                resp = await self._http.get(
                    ch_url,
                    headers={
                        "User-Agent": COPYMANGA_BROWSER_UA,
                        "Referer": f"https://{web_dom}/comic/{pw}",
                    },
                    timeout_sec=12.0,
                )
                if resp.status != 200:
                    continue
                html = resp.text()
                m_ck = _CONTENT_KEY_RE.search(html)
                m_key = _CCZ_VAR_RE.search(html)
                if not m_ck or not m_key:
                    continue
                decrypted_list = decrypt_copymanga_hex(m_ck.group(1), m_key.group(1))
                if isinstance(decrypted_list, list) and decrypted_list:
                    ordered_urls = reorder_copymanga_pages(decrypted_list, None, resolution=res)
                    if ordered_urls:
                        img_headers = {
                            "User-Agent": COPYMANGA_BROWSER_UA,
                            "Referer": f"https://{web_dom}/",
                        }
                        return [
                            PageSpec(index=i, url=u, headers=dict(img_headers))
                            for i, u in enumerate(ordered_urls)
                        ]
            except Exception as exc:
                logger.debug(f"拷贝漫画 Web 正文图片解密未成功 ({web_dom}): {exc}")

        if throttle_err is not None:
            raise throttle_err
        raise SourceThrottleError(f"拷贝漫画未能获取《{manga.title}》{chapter.title} 的图片")

    async def fetch_chapter_comments(
        self,
        manga: MangaCandidate,
        chapter: ChapterInfo,
        max_count: int = 20,
    ) -> list[ChapterComment]:
        """拉取拷贝漫画单话吐槽 (`/api/v3/roasts`)。"""
        limit = max(30, min(100, max_count * 2))
        payload, _ = await self._api_get_json(
            "/api/v3/roasts",
            params={"limit": limit, "offset": 0, "chapter_id": chapter.chapter_id},
            search_mode=True,
        )
        raw_list = (payload.get("results") or {}).get("list") or []
        if not isinstance(raw_list, list):
            return []

        comments: list[ChapterComment] = []
        seen: set[str] = set()
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            text = str(item.get("comment") or "").strip()
            if not text:
                continue
            user_name = str(item.get("user_name") or "").strip()
            if not user_name and isinstance(item.get("user"), dict):
                user_name = str(
                    item["user"].get("nickname") or item["user"].get("username") or ""
                ).strip()
            dedup_key = f"{user_name}:{text}"
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            comments.append(ChapterComment(content=text, author=user_name))
            if len(comments) >= max_count:
                break
        return comments
