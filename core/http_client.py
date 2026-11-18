"""异步 HTTP 客户端封装（基于 aiohttp，支持流式落盘防内存溢出、超时、代理与压缩容错）。"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import ssl
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import aiohttp

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)


@dataclass(slots=True)
class HttpResponseData:
    """轻量级异步响应快照（用于小型 JSON/HTML 接口，脱离连接生命周期）。"""

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


@dataclass(slots=True)
class HttpStreamResult:
    """64KB 分块流式直写磁盘结果（不在内存中缓存完整图片字节，杜绝大图并发 OOM）。"""

    status: int
    headers: dict[str, str]
    size_bytes: int
    head_bytes: bytes
    error_preview: str = ""


def _extract_set_cookies(resp: aiohttp.ClientResponse) -> dict[str, str]:
    extracted: dict[str, str] = {}
    for k, morsel in resp.cookies.items():
        extracted[k] = morsel.value
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
        fallback_proxy: str = "",
        verify_ssl: bool = True,
    ) -> None:
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._proxy = (proxy or "").strip() or None
        self._fallback_proxy = (fallback_proxy or "").strip() or None
        self._verify_ssl = verify_ssl
        self._session: aiohttp.ClientSession | None = None
        self._lock = asyncio.Lock()

    def update_settings(
        self,
        *,
        timeout_sec: float,
        proxy: str,
        fallback_proxy: str = "",
    ) -> None:
        self._timeout_sec = max(5.0, float(timeout_sec))
        self._proxy = (proxy or "").strip() or None
        self._fallback_proxy = (fallback_proxy or "").strip() or None

    def _proxy_candidates_for_url(self, url: str) -> list[str | None]:
        """智能分流：国内再漫画域名优先直连（失败回退代理），海外/Cloudflare 域名优先走代理（失败回退直连）。"""
        lower_url = url.lower()
        eff_proxy = self._proxy or self._fallback_proxy
        is_domestic_zmh = ".zaimanhua.com" in lower_url or "://zaimanhua.com" in lower_url
        if is_domestic_zmh:
            return [None, eff_proxy] if eff_proxy else [None]
        if eff_proxy:
            return [eff_proxy, None]
        return [None]

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
        max_bytes: int = 16 * 1024 * 1024,
        allow_ssl_Disable_retry: bool = False,
    ) -> HttpResponseData:
        session = await self._get_session()
        eff_timeout = aiohttp.ClientTimeout(total=timeout_sec or self._timeout_sec)
        merged_headers = {"User-Agent": DEFAULT_USER_AGENT}
        if headers:
            merged_headers.update(headers)

        async def _do_call(proxy_val: str | None, ssl_param: bool | None) -> HttpResponseData:
            kwargs: dict[str, Any] = {
                "headers": merged_headers,
                "params": params,
                "json": json_body,
                "data": form_data,
                "timeout": eff_timeout,
                "proxy": proxy_val,
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

        proxy_candidates = self._proxy_candidates_for_url(url)
        last_exc: Exception | None = None
        for p_idx, p_val in enumerate(proxy_candidates):
            try:
                return await _do_call(p_val, None)
            except aiohttp.ClientConnectorCertificateError as exc:
                last_exc = exc
                if allow_ssl_Disable_retry:
                    try:
                        return await _do_call(p_val, False)
                    except Exception as inner_exc:
                        last_exc = inner_exc
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if p_idx + 1 < len(proxy_candidates):
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def stream_to_file(
        self,
        method: str,
        url: str,
        dest_path: Path,
        *,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        timeout_sec: float | None = None,
        max_bytes: int = 18 * 1024 * 1024,
    ) -> HttpStreamResult:
        """按 64KB 分块流式下载并直接写入磁盘，仅保留前 32 字节用于格式嗅探，内存零驻留大图。"""
        session = await self._get_session()
        eff_timeout = aiohttp.ClientTimeout(total=timeout_sec or self._timeout_sec)
        merged_headers = {"User-Agent": DEFAULT_USER_AGENT}
        if headers:
            merged_headers.update(headers)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = dest_path.with_suffix(dest_path.suffix + ".part")

        async def _stream_once(proxy_val: str | None) -> HttpStreamResult:
            async with session.request(
                method.upper(),
                url,
                headers=merged_headers,
                json=json_body,
                timeout=eff_timeout,
                proxy=proxy_val,
            ) as resp:
                resp_headers = {k.lower(): v for k, v in resp.headers.items()}
                if resp.status != 200:
                    err_buf = bytearray()
                    async for chunk in resp.content.iter_chunked(4096):
                        err_buf.extend(chunk)
                        if len(err_buf) >= 512:
                            break
                    preview = bytes(err_buf[:256]).decode("utf-8", errors="replace")
                    return HttpStreamResult(
                        status=resp.status,
                        headers=resp_headers,
                        size_bytes=0,
                        head_bytes=b"",
                        error_preview=preview,
                    )

                cl_str = resp_headers.get("content-length") or ""
                if cl_str.isdigit() and int(cl_str) > max_bytes:
                    raise ValueError(f"远端声明文件体积超限 ({cl_str} > {max_bytes} bytes)")

                head_buf = bytearray()
                total_written = 0
                fd = await asyncio.to_thread(
                    os.open,
                    str(part_path),
                    os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
                    0o644,
                )
                try:
                    async for chunk in resp.content.iter_chunked(65536):
                        if not chunk:
                            continue
                        if len(head_buf) < 32:
                            need = 32 - len(head_buf)
                            head_buf.extend(chunk[:need])
                        total_written += len(chunk)
                        if total_written > max_bytes:
                            raise ValueError(
                                f"流式下载体积超出安全上限 ({total_written} > {max_bytes} bytes)"
                            )
                        await asyncio.to_thread(os.write, fd, chunk)
                except BaseException:
                    await asyncio.to_thread(os.close, fd)
                    part_path.unlink(missing_ok=True)
                    raise
                else:
                    await asyncio.to_thread(os.close, fd)

                await asyncio.to_thread(os.replace, part_path, dest_path)
                return HttpStreamResult(
                    status=200,
                    headers=resp_headers,
                    size_bytes=total_written,
                    head_bytes=bytes(head_buf),
                )

        proxy_candidates = self._proxy_candidates_for_url(url)
        last_exc: Exception | None = None
        for p_idx, p_val in enumerate(proxy_candidates):
            try:
                return await _stream_once(p_val)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                part_path.unlink(missing_ok=True)
                last_exc = exc
                if p_idx + 1 < len(proxy_candidates):
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    async def get(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        params: dict[str, Any] | None = None,
        timeout_sec: float | None = None,
        max_bytes: int = 16 * 1024 * 1024,
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
