"""三大漫画源协议解析、页序还原、单话评论提取与登录态自动刷新测试。"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from core.auth_manager import AuthManager
from core.config import CopymangaConfig, KomiicConfig, ZaimanhuaConfig
from core.http_client import HttpResponseData
from core.models import (
    ChapterKind,
    MangaCandidate,
    SourceId,
    SourceThrottleError,
)
from core.sources.copymanga import CopymangaSource, reorder_copymanga_pages
from core.sources.komiic import KomiicSource
from core.sources.zaimanhua import ZaimanhuaSource


def _make_jwt(exp_offset_sec: int) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').decode().rstrip("=")
    payload_json = json.dumps({"exp": int(time.time()) + exp_offset_sec}).encode("utf-8")
    payload = base64.urlsafe_b64encode(payload_json).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _encrypt_copymanga_hex(obj: Any, key_16: str, iv_16: str = "1234567890abcdef") -> str:
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    cipher = Cipher(algorithms.AES(key_16.encode("utf-8")), modes.CBC(iv_16.encode("utf-8")))
    enc = cipher.encryptor()
    ct = enc.update(padded) + enc.finalize()
    return iv_16 + ct.hex()


class _MockRouterHttp:
    def __init__(self) -> None:
        self.handlers: list[Any] = []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def get(self, url: str, **kwargs: Any) -> HttpResponseData:
        return await self._dispatch("GET", url, kwargs)

    async def post(self, url: str, **kwargs: Any) -> HttpResponseData:
        return await self._dispatch("POST", url, kwargs)

    async def _dispatch(self, method: str, url: str, kwargs: dict[str, Any]) -> HttpResponseData:
        self.calls.append((method, url, kwargs))
        for handler in self.handlers:
            res = handler(method, url, kwargs)
            if res is not None:
                return res
        return HttpResponseData(status=404, headers={}, cookies={}, body=b"{}", url=url)


def test_copymanga_words_reordering_and_resolution() -> None:
    # 模拟拷贝漫画打乱的 contents 与真实页序映射 words = [2, 0, 1]
    contents = [
        {"url": "https://img.example.com/p_third.jpg.c800x.jpg"},
        {"url": "https://img.example.com/p_first.jpg.c800x.jpg"},
        {"url": "https://img.example.com/p_second.jpg.c800x.jpg"},
    ]
    words = [2, 0, 1]
    ordered = reorder_copymanga_pages(contents, words, resolution="1500")
    assert ordered == [
        "https://img.example.com/p_first.jpg.c1500x.jpg",
        "https://img.example.com/p_second.jpg.c1500x.jpg",
        "https://img.example.com/p_third.jpg.c1500x.jpg",
    ]


@pytest.mark.asyncio
async def test_copymanga_web_aes_fallback_and_roasts() -> None:
    http = _MockRouterHttp()
    key_16 = "0p0zzpvv.nmn.oop"
    enc_chapters = _encrypt_copymanga_hex(
        {
            "groups": {
                "default": {
                    "name": "默認",
                    "chapters": [{"id": "uuid-ch1", "name": "第01話"}],
                }
            }
        },
        key_16,
    )
    enc_pages = _encrypt_copymanga_hex(
        [
            {"url": "https://cdn.example.com/01.jpg.c800x.jpg"},
            {"url": "https://cdn.example.com/02.jpg.c800x.jpg"},
        ],
        key_16,
    )

    def _route(method: str, url: str, kwargs: dict[str, Any]) -> HttpResponseData | None:
        if "/api/v3/comic2/" in url or "/api/v3/comic/dianjuren/chapter2/" in url:
            # 模拟 REST v3 触发 code=210 频控，迫使适配器自动切换到 Web AES-128-CBC 通道
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps({"code": 210, "message": "throttled"}).encode(),
                url=url,
            )
        if url == "https://www.mangacopy.com/comic/dianjuren":
            html = f'<span id="dnt" value="9"></span><script>var ccz = "{key_16}";</script>'
            return HttpResponseData(
                status=200, headers={}, cookies={"sessionid": "s1"}, body=html.encode(), url=url
            )
        if url == "https://www.mangacopy.com/comicdetail/dianjuren/chapters":
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps({"code": 200, "results": enc_chapters}).encode(),
                url=url,
            )
        if url == "https://www.mangacopy.com/comic/dianjuren/chapter/uuid-ch1":
            html = f'<div contentKey="{enc_pages}"></div><script>var cct = "{key_16}";</script>'
            return HttpResponseData(status=200, headers={}, cookies={}, body=html.encode(), url=url)
        if "/api/v3/roasts" in url:
            roasts = {
                "code": 200,
                "results": {
                    "list": [
                        {"user_name": "波奇塔", "comment": "第一话神作开局！"},
                        {"user_name": "电次", "comment": "好想吃涂果酱的面包"},
                    ]
                },
            }
            return HttpResponseData(
                status=200, headers={}, cookies={}, body=json.dumps(roasts).encode(), url=url
            )
        return None

    http.handlers.append(_route)
    cfg = CopymangaConfig(image_resolution="1500")
    source = CopymangaSource(lambda: cfg, http)  # type: ignore[arg-type]
    cand = MangaCandidate(source=SourceId.COPYMANGA, manga_id="dianjuren", title="电锯人")

    chapters = await source.fetch_chapters(cand)
    assert len(chapters) == 1 and chapters[0].chapter_id == "uuid-ch1"

    pages = await source.fetch_chapter_pages(cand, chapters[0])
    assert [p.url for p in pages] == [
        "https://cdn.example.com/01.jpg.c1500x.jpg",
        "https://cdn.example.com/02.jpg.c1500x.jpg",
    ]

    comments = await source.fetch_chapter_comments(cand, chapters[0], max_count=10)
    assert len(comments) == 2
    assert comments[0].author == "波奇塔"
    assert comments[0].content == "第一话神作开局！"


@pytest.mark.asyncio
async def test_copymanga_all_channels_throttled_raises() -> None:
    http = _MockRouterHttp()
    http.handlers.append(
        lambda m, u, k: HttpResponseData(
            status=210,
            headers={},
            cookies={},
            body=b'{"code":210,"message":"rate limit"}',
            url=u,
        )
    )
    source = CopymangaSource(lambda: CopymangaConfig(), http)  # type: ignore[arg-type]
    with pytest.raises(SourceThrottleError):
        await source.search_manga("电锯人")


@pytest.mark.asyncio
async def test_zaimanhua_auto_relogin_hidden_chapters_and_comments(tmp_path: Path) -> None:
    http = _MockRouterHttp()
    expired_jwt = _make_jwt(-600)
    fresh_jwt = _make_jwt(86400)
    login_calls = 0

    # 预写入一条已过期的缓存凭证，验证请求时自动重登并落盘新 token
    state_file = tmp_path / "auth_state.json"
    auth = AuthManager(state_file, http)  # type: ignore[arg-type]
    zmh_cfg = ZaimanhuaConfig(username="tester", password="secret_password", auto_sign_in=True)

    def _route(method: str, url: str, kwargs: dict[str, Any]) -> HttpResponseData | None:
        nonlocal login_calls
        if "account-api.zaimanhua.com/v1/login/passwd" in url:
            login_calls += 1
            return HttpResponseData(
                status=200,
                headers={},
                cookies={"zmh_sess": "ok"},
                body=json.dumps({"errno": 0, "data": {"user": {"token": fresh_jwt}}}).encode(),
                url=url,
            )
        if "task/sign_in" in url:
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=b'{"errno":0,"errmsg":"ok"}',
                url=url,
            )
        if "v4api.zaimanhua.com/app/v1/comic/detail/46598" in url:
            hdrs = kwargs.get("headers") or {}
            if hdrs.get("Authorization") != f"Bearer {fresh_jwt}":
                return HttpResponseData(
                    status=200, headers={}, cookies={}, body=b'{"errno":99}', url=url
                )
            # 模拟 isHideChapter=1，触发 PC comic2/comic/detail 补全隐藏章节
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps(
                    {
                        "errno": 0,
                        "data": {"data": {"id": 46598, "isHideChapter": 1, "canRead": True}},
                    }
                ).encode(),
                url=url,
            )
        if "manhua.zaimanhua.com/api/v1/comic2/comic/detail" in url:
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps(
                    {
                        "errno": 0,
                        "data": {
                            "comicInfo": {
                                "title": "电锯人",
                                "chapterList": [
                                    {
                                        "title": "连载",
                                        "data": [
                                            {
                                                "chapter_id": 9001,
                                                "chapter_title": "第01话",
                                                "updatetime": 1700000000,
                                            }
                                        ],
                                    }
                                ],
                            }
                        },
                    }
                ).encode(),
                url=url,
            )
        if "v4api.zaimanhua.com/app/v1/comic/chapter/46598/9001" in url:
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps(
                    {
                        "errno": 0,
                        "data": {
                            "data": {
                                "canRead": True,
                                "page_url_hd": [
                                    "https://images.zaimanhua.com/p1.jpg",
                                    "https://images.zaimanhua.com/p2.jpg",
                                ],
                            }
                        },
                    }
                ).encode(),
                url=url,
            )
        if "viewpoint/list" in url:
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps(
                    {
                        "errno": 0,
                        "data": {
                            "list": [
                                [9001, 0, 1, 66, "", 0, 1001, "好耶！"],
                                [9001, 0, 2, 23, "", 0, 1002, "生姜烧肉定食"],
                            ]
                        },
                    }
                ).encode(),
                url=url,
            )
        return None

    http.handlers.append(_route)
    _ = expired_jwt

    class _DummyShadow:
        pass

    source = ZaimanhuaSource(lambda: zmh_cfg, http, auth, _DummyShadow())  # type: ignore[arg-type]
    cand = MangaCandidate(
        source=SourceId.ZAIMANHUA, manga_id="46598", title="电锯人", need_login=True
    )

    chapters = await source.fetch_chapters(cand)
    assert login_calls == 1
    assert len(chapters) == 1 and chapters[0].chapter_id == "9001"
    # 验证持久化文件已保存新 token 与 cookie
    saved = json.loads(state_file.read_text(encoding="utf-8"))
    assert saved["zaimanhua"]["token"] == fresh_jwt

    pages = await source.fetch_chapter_pages(cand, chapters[0])
    assert [p.url for p in pages] == [
        "https://images.zaimanhua.com/p1.jpg",
        "https://images.zaimanhua.com/p2.jpg",
    ]
    comments = await source.fetch_chapter_comments(cand, chapters[0])
    assert [c.content for c in comments] == ["好耶！", "生姜烧肉定食"]
    assert comments[0].likes == 66


@pytest.mark.asyncio
async def test_komiic_refresh_and_graphql_flow(tmp_path: Path) -> None:
    http = _MockRouterHttp()
    soon_expire_jwt = _make_jwt(600)  # 仅剩 10 分钟 (< 3600s 阈值，应触发 /auth/refresh)
    refreshed_jwt = _make_jwt(86400)
    refresh_called = False

    km_cfg = KomiicConfig(username="user@example.com", password="pw")
    state_file = tmp_path / "auth_state.json"
    auth = AuthManager(state_file, http)  # type: ignore[arg-type]

    # 先通过初始登录落盘一条即将过期的 Cookie，随后验证自动调用 /auth/refresh
    login_mode = "soon"

    def _route(method: str, url: str, kwargs: dict[str, Any]) -> HttpResponseData | None:
        nonlocal refresh_called
        if url.endswith("/api/login"):
            tok = soon_expire_jwt if login_mode == "soon" else refreshed_jwt
            return HttpResponseData(
                status=200,
                headers={},
                cookies={"komiic-access-token": tok, "komiic-refresh": "r1"},
                body=b"{}",
                url=url,
            )
        if url.endswith("/auth/refresh"):
            refresh_called = True
            return HttpResponseData(
                status=200,
                headers={},
                cookies={"komiic-access-token": refreshed_jwt},
                body=b"{}",
                url=url,
            )
        if url.endswith("/api/query"):
            body = kwargs.get("json_body") or {}
            op = body.get("operationName")
            if op == "chapterByComicId":
                return HttpResponseData(
                    status=200,
                    headers={},
                    cookies={},
                    body=json.dumps(
                        {
                            "data": {
                                "chaptersByComicId": [
                                    {
                                        "id": "b1",
                                        "serial": "1",
                                        "type": "book",
                                        "size": 180,
                                        "dateUpdated": "2026-01-01T00:00:00Z",
                                    },
                                    {
                                        "id": "c1",
                                        "serial": "1",
                                        "type": "chapter",
                                        "size": 45,
                                        "dateUpdated": "2026-01-02T00:00:00Z",
                                    },
                                ]
                            }
                        }
                    ).encode(),
                    url=url,
                )
            if op == "imagesByChapterId":
                return HttpResponseData(
                    status=200,
                    headers={},
                    cookies={},
                    body=json.dumps(
                        {"data": {"imagesByChapterId": [{"kid": "kid-1"}, {"kid": "kid-2"}]}}
                    ).encode(),
                    url=url,
                )
        return None

    http.handlers.append(_route)
    # 首次调用触发 /api/login 拿到 soon_expire_jwt，第二次请求前因 <3600s 自动触发 /auth/refresh
    hdr1 = await auth.get_komiic_auth_headers(km_cfg)
    assert soon_expire_jwt in hdr1["Cookie"]

    source = KomiicSource(lambda: km_cfg, http, auth)  # type: ignore[arg-type]
    cand = MangaCandidate(source=SourceId.KOMIIC, manga_id="294", title="電鋸人")
    chapters = await source.fetch_chapters(cand)
    assert refresh_called is True
    assert chapters[0].kind == ChapterKind.VOLUME
    assert chapters[1].kind == ChapterKind.CHAPTER

    pages = await source.fetch_chapter_pages(cand, chapters[1])
    assert [p.url for p in pages] == [
        "https://komiic.com/api/image/kid-1",
        "https://komiic.com/api/image/kid-2",
    ]
    assert pages[0].headers["Referer"].endswith("/comic/294/chapter/c1/page/1")
    assert refreshed_jwt in pages[0].headers["Cookie"]


@pytest.mark.asyncio
async def test_zaimanhua_v4_disconnect_failover_to_pc_channel(tmp_path: Path) -> None:
    """验证当再漫画 v4api 跨境连接超时/重置时，自动无感故障切换至 PC 端 comic2 接口，不会误回退下一漫画源。"""
    import asyncio

    http = _MockRouterHttp()
    auth = AuthManager(tmp_path / "auth.json", http)  # type: ignore[arg-type]
    valid_jwt = _make_jwt(int(time.time()) + 86400)
    zmh_cfg = ZaimanhuaConfig(cookie=valid_jwt, prefer_cookie=True)

    def _route(method: str, url: str, kwargs: dict[str, Any]) -> HttpResponseData | None:
        if "v4api.zaimanhua.com" in url:
            raise asyncio.TimeoutError("v4api cross-border timeout")
        if "manhua.zaimanhua.com/api/v1/comic2/comic/detail" in url:
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps(
                    {
                        "errno": 0,
                        "data": {
                            "comicInfo": {
                                "title": "黑暗集会",
                                "chapterList": [
                                    {
                                        "title": "连载",
                                        "data": [
                                            {
                                                "chapter_id": 196804,
                                                "chapter_title": "85话",
                                                "updatetime": 1793848380,
                                            }
                                        ],
                                    }
                                ],
                            }
                        },
                    }
                ).encode(),
                url=url,
            )
        if "manhua.zaimanhua.com/api/v1/comic2/chapter/detail" in url:
            return HttpResponseData(
                status=200,
                headers={},
                cookies={},
                body=json.dumps(
                    {
                        "errno": 0,
                        "data": {
                            "chapterInfo": {
                                "page_url": [
                                    "https://images.zaimanhua.com/h%2F85%2F001.jpg",
                                    "https://images.zaimanhua.com/h%2F85%2F002.jpg",
                                ]
                            }
                        },
                    }
                ).encode(),
                url=url,
            )
        return None

    http.handlers.append(_route)
    source = ZaimanhuaSource(lambda: zmh_cfg, http, auth, object())  # type: ignore[arg-type]
    cand = MangaCandidate(source=SourceId.ZAIMANHUA, manga_id="47682", title="黑暗集会")
    chs = await source.fetch_chapters(cand)
    assert len(chs) == 1 and chs[0].title == "85话"
    specs = await source.fetch_chapter_pages(cand, chs[0])
    assert len(specs) == 2 and "%2F" in specs[0].url
