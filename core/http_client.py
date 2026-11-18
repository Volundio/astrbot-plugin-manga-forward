"""异步 HTTP 客户端封装（基于 aiohttp，支持超时、代理、Cookie 提取与压缩容错）。"""

from __future__ import annotations

import asyncio
import gzip
import json
import ssl
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Any

import aiohttp

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class HttpResponseData:
    """轻量级异步响应快照（脱离连接生命周期，避免连接泄漏）。"""

    status: int
    headers: dict[str, str]
    cookies: dict[str, str]
    body: bytes
    url: str

    def text(self, encoding: str = "utf-8") -> str:
        raw = self.body
        if len(raw) >= 3 and raw[0] == 0x1F and raw[1] == 0x8B and raw[2] == 0x08:
            try:
                raw = gzip.decompress(raw)
            except Exception:
                pass
        return raw.decode(encoding, errors="replace")

    def json(self) -> Any:
        txt = self.text("utf-8").lstrip("\ufeff").strip()
        return json.loads(txt)


def _extract_set_cookies(resp: aiohttp.ClientResponse) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for k, morsel in resp.cookies.items():
        extracted[k] = morsel.value
    # 补充解析多条原始 Set-Cookie 头，防止特殊字符 Cookie 被标准库丢弃
    raw_headers = resp.headers.getall("Set-Cookie", [])
    for raw_sc in raw_headers:
        sc = SimpleCookie()
        try:
            sc.load(raw_sc)
            for k, morsel in sc.items():
                extracted[k] = morsel.value
        except Exception:
            first_pair = raw_sc.split(";", 1)[0].strip()
            if "=" in first_pair:
                ck, cv = first_pair.split("=", 1)
                extracted[ck.strip()] = cv.strip()
    return extracted


class AsyncHttpClient:
    """插件级共享异步 HTTP 会话管理器。"""

    def __init__(
        self,
        *,
        timeout_sec: float = 20.0,
        proxy: str = "",
        verify_ssl: bool = True,
    ) -> None:
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._proxy = (proxy or "").strip() or None
        self._verify_ssl = verify_ssl
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    def update_settings(self, *, timeout_sec: float, proxy: str) -> None:
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._proxy = (proxy or "").strip() or None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is not None and not self._session.closed:
            return self._session
        async with self._lock:
            if self._session is not None and not self._session.closed:
                return self._session
            ssl_ctx: ssl.SSLContext | bool = True
            if not self._verify_ssl:
                ssl_ctx = False
            connector = aiohttp.TCPConnector(
                limit=32,
                limit_per_host=12,
                ttl_dns_cache=300,
                ssl=ssl_ctx,
            )
            # 禁用 aiohttp 内部 CookieJar 自动串味，鉴权 Cookie 全部由 AuthManager 精确按源注入
            self._session = aiohttp.ClientSession(
                connector=connector,
                cookie_jar=aiohttp.DummyCookieJar(),
                headers={"User-Agent": DEFAULT_USER_AGENT},
            )
            return self._session

    async def close(self) -> None:
        async with self._lock:
            if self._session is not None and not self._session.closed:
                await self._session.close()
            self._session = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        form_data: dict[str, str] | None = None,
        timeout_sec: float | None = None,
        max_bytes: int = 32 * 1024 * 1024,
        allow_ssl_Disable_retry: bool = False,
    ) -> HttpResponseData:
        session = await self._get_session()
        eff_timeout = aiohttp.ClientTimeout(total=timeout_sec or self._timeout_sec)
        merged_headers = {"User-Agent": DEFAULT_USER_AGENT}
        if headers:
            merged_headers.update(headers)

        async def _do_call(ssl_param: bool | None) -> HttpResponseData:
            kwargs: dict[str, Any] = {
                "headers": merged_headers,
                "params": params,
                "json": json_body,
                "data": form_data,
                "timeout": eff_timeout,
                "proxy": self._proxy,
            }
            if ssl_param is not None:
                kwargs["ssl"] = ssl_param

            async with session.request(method.upper(), url, **kwargs) as resp:
                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.content.iter_chunked(65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError(f"响应体积超出上限 ({total} > {max_bytes} bytes): {url}")
                    chunks.append(chunk)
                body = b"".join(chunks)
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                cookies = _extract_set_cookies(resp)
                return HttpResponseData(
                    status=resp.status,
                    headers=resp_headers,
                    cookies=cookies,
                    body=body,
                    url=str(resp.url),
                )

        try:
            return await _do_call(None)
        except aiohttp.ClientConnectorCertificateError:
            if allow_ssl_Disable_retry:
                return await _do_call(False)
            raise

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
        max_bytes: int = 32 * 1024 * 1024,
    ) -> HttpResponseData:
        return await self.request(
            "GET",
            url,
            headers=headers,
            params=params,
            timeout_sec=timeout_sec,
            max_bytes=max_bytes,
        )

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        form_data: dict[str, str] | None = None,
        timeout_sec: float | None = None,
    ) -> HttpResponseData:
        return await self.request(
            "POST",
            url,
            headers=headers,
            params=params,
            json_body=json_body,
            form_data=form_data,
            timeout_sec=timeout_sec,
        )
