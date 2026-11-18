"""再漫画与 Komiic 账号登录、Cookie/Token 持久化保存及失效自动刷新模块。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from astrbot.api import logger

from .config import KomiicConfig, ZaimanhuaConfig
from .http_client import AsyncHttpClient
from .models import SourceAuthError

ZMH_ACCOUNT_LOGIN_URL = "https://account-api.zaimanhua.com/v1/login/passwd"
ZMH_PASSPORT_LOGIN_URL = "https://i.zaimanhua.com/lpi/v1/login/passwd"
ZMH_SIGN_IN_URL = "https://i.zaimanhua.com/lpi/v1/task/sign_in"

_CST_TZ = timezone(timedelta(hours=8))


def parse_cookie_string(raw: str) -> dict[str, str]:
    """将浏览器 Cookie 字符串解析为键值对字典。"""
    s = (raw or "").strip()
    if not s:
        return {}
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    result: dict[str, str] = {}
    sc = SimpleCookie()
    try:
        sc.load(s)
        for k, morsel in sc.items():
            result[k] = morsel.value
    except Exception:
        pass
    # 补充手工分割以兼容含特殊字符的 JWT 值
    for segment in s.split(";"):
        seg = segment.strip()
        if not seg or "=" not in seg:
            continue
        k, v = seg.split("=", 1)
        k = k.strip()
        v = v.strip()
        if k and k.lower() not in ("path", "domain", "expires", "max-age", "samesite"):
            result.setdefault(k, v)
    return result


def format_cookie_header(cookies: dict[str, str]) -> str:
    """将 Cookie 字典格式化为请求头字符串。"""
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if k and v)


def decode_jwt_exp(token: str) -> int | None:
    """无第三方依赖解析 JWT 载荷中的 `exp` 过期秒级时间戳。"""
    tok = (token or "").strip()
    if tok.lower().startswith("bearer "):
        tok = tok[7:].strip()
    parts = tok.split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    padding_len = (-len(payload_b64)) % 4
    if padding_len:
        payload_b64 += "=" * padding_len
    try:
        decoded = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        obj = json.loads(decoded.decode("utf-8"))
        exp = obj.get("exp")
        return int(exp) if exp is not None else None
    except Exception:
        return None


def is_jwt_expired(token: str, leeway_sec: int = 300) -> bool:
    """检查 JWT 是否已过期（含提前刷新缓冲窗口 `leeway_sec`）。非 JWT 格式视作不过期交由服务端判定。"""
    exp = decode_jwt_exp(token)
    if exp is None:
        return False
    return (time.time() + leeway_sec) >= exp


def _credential_fingerprint(username: str, password: str) -> str:
    raw = f"{username.strip()}\n{password}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _read_json_sync(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic_sync(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    try:
        tmp_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


class AuthManager:
    """统一管理再漫画与 Komiic 的登录态获取、磁盘持久化、过期续期与失效重登。"""

    def __init__(self, state_file: Path, http: AsyncHttpClient) -> None:
        self._state_file = state_file
        self._http = http
        self._zmh_lock = asyncio.Lock()
        self._komiic_lock = asyncio.Lock()
        self._file_lock = asyncio.Lock()
        self._memory_state: dict[str, Any] | None = None

    async def _load_state(self) -> dict[str, Any]:
        if self._memory_state is not None:
            return self._memory_state
        async with self._file_lock:
            if self._memory_state is None:
                self._memory_state = await asyncio.to_thread(_read_json_sync, self._state_file)
            return self._memory_state

    async def _save_section(self, section: str, payload: dict[str, Any]) -> None:
        async with self._file_lock:
            if self._memory_state is None:
                self._memory_state = await asyncio.to_thread(_read_json_sync, self._state_file)
            self._memory_state[section] = payload
            snapshot = dict(self._memory_state)
            await asyncio.to_thread(_write_json_atomic_sync, self._state_file, snapshot)

    # ------------------------------------------------------------------
    # 再漫画 (Zaimanhua) 认证管理
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_manual_zmh_cookie(raw_cookie: str) -> tuple[str, dict[str, str]]:
        """从用户直接填写的再漫画 Cookie 或 Token 中提取 JWT Token 与 Cookie 字典。"""
        text = (raw_cookie or "").strip()
        if not text:
            return "", {}
        if text.lower().startswith("bearer "):
            jwt = text[7:].strip()
            return jwt, {"token": jwt}
        if "=" not in text and ";" not in text:
            return text, {"token": text}

        cookies = parse_cookie_string(text)
        jwt = (
            cookies.get("token")
            or cookies.get("my")
            or cookies.get("Authorization")
            or cookies.get("authorization")
            or ""
        )
        if jwt.lower().startswith("bearer "):
            jwt = jwt[7:].strip()
        return jwt, cookies

    @staticmethod
    def _build_zmh_headers(token: str, cookies: dict[str, str]) -> dict[str, str]:
        headers: dict[str, str] = {}
        merged_cookies = dict(cookies)
        if token:
            headers["Authorization"] = f"Bearer {token}"
            merged_cookies.setdefault("token", token)
        cookie_str = format_cookie_header(merged_cookies)
        if cookie_str:
            headers["Cookie"] = cookie_str
        return headers

    async def get_zaimanhua_auth_headers(
        self,
        cfg: ZaimanhuaConfig,
        *,
        force_refresh: bool = False,
    ) -> dict[str, str]:
        """获取再漫画请求鉴权头（含 `Authorization` 与 `Cookie`）。

        支持：
        1. 直接使用配置的手动 Cookie / Token；
        2. 账号密码自动登录并持久化 Cookie/Token，过期或 `force_refresh=True` 时自动重登。
        """
        has_account = bool(cfg.username.strip() and cfg.password)
        has_manual_cookie = bool(cfg.cookie.strip())

        # 情况 A：未强制刷新时，若用户仅配置手动 Cookie，或开启了 prefer_cookie
        if has_manual_cookie and (not has_account or (cfg.prefer_cookie and not force_refresh)):
            manual_token, manual_cookies = self._parse_manual_zmh_cookie(cfg.cookie)
            if manual_token and is_jwt_expired(manual_token, leeway_sec=60) and has_account:
                logger.info("手动配置的再漫画 Cookie/Token 已过期，自动使用账号密码登录兜底。")
            else:
                return self._build_zmh_headers(manual_token, manual_cookies)

        if not has_account:
            if has_manual_cookie:
                manual_token, manual_cookies = self._parse_manual_zmh_cookie(cfg.cookie)
                return self._build_zmh_headers(manual_token, manual_cookies)
            return {}

        fp = _credential_fingerprint(cfg.username, cfg.password)
        state = await self._load_state()
        cached = state.get("zaimanhua") if isinstance(state.get("zaimanhua"), dict) else {}

        if not force_refresh and cached.get("cred_fp") == fp:
            cached_token = str(cached.get("token") or "")
            cached_cookies = (
                cached.get("cookies") if isinstance(cached.get("cookies"), dict) else {}
            )
            if cached_token and not is_jwt_expired(cached_token, leeway_sec=300):
                if cfg.auto_sign_in:
                    await self._maybe_zmh_daily_sign_in(cached_token, cached)
                return self._build_zmh_headers(cached_token, cached_cookies)

        async with self._zmh_lock:
            # 双重检查锁：若并发协程刚刚完成了重登，直接复用新令牌
            state = await self._load_state()
            cached = state.get("zaimanhua") if isinstance(state.get("zaimanhua"), dict) else {}
            if (
                not force_refresh
                and cached.get("cred_fp") == fp
                and cached.get("token")
                and not is_jwt_expired(str(cached["token"]), leeway_sec=300)
            ):
                return self._build_zmh_headers(
                    str(cached["token"]),
                    cached.get("cookies") if isinstance(cached.get("cookies"), dict) else {},
                )

            if force_refresh and cached.get("cred_fp") == fp:
                updated_at = float(cached.get("updated_at") or 0.0)
                if (time.time() - updated_at) < 5.0 and cached.get("token"):
                    # 5 秒内刚刚刷新过，避免并发重复打登录接口
                    return self._build_zmh_headers(
                        str(cached["token"]),
                        cached.get("cookies") if isinstance(cached.get("cookies"), dict) else {},
                    )

            token, cookies = await self._login_zaimanhua(cfg.username, cfg.password)
            new_record: dict[str, Any] = {
                "cred_fp": fp,
                "token": token,
                "cookies": cookies,
                "updated_at": time.time(),
                "last_sign_date": str(cached.get("last_sign_date") or ""),
            }
            await self._save_section("zaimanhua", new_record)
            logger.info("再漫画账号登录成功，已更新并持久化本地 Cookie/Token。")

            if cfg.auto_sign_in:
                await self._maybe_zmh_daily_sign_in(token, new_record)

            return self._build_zmh_headers(token, cookies)

    async def _login_zaimanhua(self, username: str, password: str) -> tuple[str, dict[str, str]]:
        """执行再漫画双通道账号密码登录（兼容误粘首尾引号兜底）。"""
        pwd_candidates = [password]
        stripped_pwd = password.strip()
        if (
            len(stripped_pwd) >= 3
            and stripped_pwd[0] in ('"', "'", "“")
            and stripped_pwd[-1] in ('"', "'", "”")
        ):
            unquoted = stripped_pwd[1:-1]
            if unquoted and unquoted not in pwd_candidates:
                pwd_candidates.append(unquoted)

        errors: list[str] = []
        for pwd_try in pwd_candidates:
            passwd_md5 = hashlib.md5(pwd_try.encode("utf-8")).hexdigest().lower()

            # 通道 1：Mihon 再漫画同款 account-api.zaimanhua.com/v1/login/passwd
            try:
                resp = await self._http.post(
                    ZMH_ACCOUNT_LOGIN_URL,
                    form_data={"username": username.strip(), "passwd": passwd_md5},
                    headers={"X-Requested-With": "XMLHttpRequest"},
                )
                if resp.status == 200:
                    payload = resp.json()
                    errno = payload.get("errno", 0)
                    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
                    user_obj = (
                        data.get("user")
                        or data.get("userInfo")
                        or (data if "token" in data else {})
                    )
                    token = (
                        str(user_obj.get("token") or "").strip()
                        if isinstance(user_obj, dict)
                        else ""
                    )
                    if errno == 0 and token:
                        cookies = dict(resp.cookies)
                        cookies["token"] = token
                        return token, cookies
                    errmsg = str(payload.get("errmsg") or f"errno={errno}")
                    errors.append(f"account-api: {errmsg}")
                else:
                    errors.append(f"account-api HTTP {resp.status}")
            except Exception as exc:
                errors.append(f"account-api 异常: {exc}")

            # 通道 2：再漫画签到脚本同款 i.zaimanhua.com/lpi/v1/login/passwd
            try:
                query = urlencode({"username": username.strip(), "passwd": passwd_md5})
                resp2 = await self._http.post(f"{ZMH_PASSPORT_LOGIN_URL}?{query}")
                if resp2.status == 200:
                    payload2 = resp2.json()
                    errno2 = payload2.get("errno", -1)
                    data2 = payload2.get("data") if isinstance(payload2.get("data"), dict) else {}
                    user2 = data2.get("user") or data2.get("userInfo") or data2
                    token2 = (
                        str(user2.get("token") or payload2.get("token") or "").strip()
                        if isinstance(user2, dict)
                        else ""
                    )
                    if errno2 == 0 and token2:
                        cookies2 = dict(resp2.cookies)
                        cookies2["token"] = token2
                        return token2, cookies2
                    errmsg2 = str(payload2.get("errmsg") or f"errno={errno2}")
                    errors.append(f"lpi: {errmsg2}")
                else:
                    errors.append(f"lpi HTTP {resp2.status}")
            except Exception as exc:
                errors.append(f"lpi 异常: {exc}")

        raise SourceAuthError(f"再漫画账号自动登录失败（{'；'.join(errors)}）")

    async def _maybe_zmh_daily_sign_in(self, token: str, record: dict[str, Any]) -> None:
        """每日最多一次自动执行再漫画签到（参考 qd-today 再漫画签到模板，失败静默不阻塞业务）。"""
        today_str = datetime.now(_CST_TZ).strftime("%Y-%m-%d")
        if record.get("last_sign_date") == today_str:
            return
        record["last_sign_date"] = today_str
        await self._save_section("zaimanhua", record)
        try:
            resp = await self._http.post(
                ZMH_SIGN_IN_URL,
                headers={"Authorization": f"Bearer {token}"},
                timeout_sec=8.0,
            )
            if resp.status == 200:
                data = resp.json()
                errno = data.get("errno")
                errmsg = data.get("errmsg") or "成功"
                if errno in (0, 1):
                    logger.info(f"再漫画每日自动签到完成：{errmsg}")
        except Exception as exc:
            logger.debug(f"再漫画自动签到略过：{exc}")

    # ------------------------------------------------------------------
    # Komiic 认证管理
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_manual_komiic_cookie(raw_cookie: str) -> dict[str, str]:
        text = (raw_cookie or "").strip()
        if not text:
            return {}
        if text.lower().startswith("bearer "):
            text = text[7:].strip()
        if "=" not in text and ";" not in text:
            return {"komiic-access-token": text}
        return parse_cookie_string(text)

    async def get_komiic_auth_headers(
        self,
        cfg: KomiicConfig,
        *,
        force_refresh: bool = False,
    ) -> dict[str, str]:
        """获取 Komiic 请求的 `Cookie` 鉴权头，支持 `/auth/refresh` 自动续期与 `/api/login` 自动重登。"""
        has_account = bool(cfg.username.strip() and cfg.password)
        has_manual_cookie = bool(cfg.cookie.strip())
        base_url = cfg.base_url.rstrip("/")

        if has_manual_cookie and (not has_account or (cfg.prefer_cookie and not force_refresh)):
            manual_cookies = self._parse_manual_komiic_cookie(cfg.cookie)
            access_tok = manual_cookies.get("komiic-access-token", "")
            if access_tok and is_jwt_expired(access_tok, leeway_sec=60) and has_account:
                logger.info("手动配置的 Komiic Cookie 已过期，自动使用账号密码登录兜底。")
            else:
                cookie_hdr = format_cookie_header(manual_cookies)
                return {"Cookie": cookie_hdr} if cookie_hdr else {}

        if not has_account:
            if has_manual_cookie:
                cookie_hdr = format_cookie_header(self._parse_manual_komiic_cookie(cfg.cookie))
                return {"Cookie": cookie_hdr} if cookie_hdr else {}
            return {}

        fp = _credential_fingerprint(cfg.username, cfg.password)
        state = await self._load_state()
        cached = state.get("komiic") if isinstance(state.get("komiic"), dict) else {}

        if not force_refresh and cached.get("cred_fp") == fp:
            cached_cookies = (
                dict(cached.get("cookies")) if isinstance(cached.get("cookies"), dict) else {}
            )
            access_tok = cached_cookies.get("komiic-access-token", "")
            if access_tok and not is_jwt_expired(access_tok, leeway_sec=3600):
                return {"Cookie": format_cookie_header(cached_cookies)}

        async with self._komiic_lock:
            state = await self._load_state()
            cached = state.get("komiic") if isinstance(state.get("komiic"), dict) else {}
            cached_cookies = (
                dict(cached.get("cookies"))
                if (cached.get("cred_fp") == fp and isinstance(cached.get("cookies"), dict))
                else {}
            )
            access_tok = cached_cookies.get("komiic-access-token", "")

            if not force_refresh and access_tok and not is_jwt_expired(access_tok, leeway_sec=3600):
                return {"Cookie": format_cookie_header(cached_cookies)}

            if force_refresh and cached.get("cred_fp") == fp:
                updated_at = float(cached.get("updated_at") or 0.0)
                if (time.time() - updated_at) < 5.0 and cached_cookies:
                    return {"Cookie": format_cookie_header(cached_cookies)}

            # 1. 若已有历史 Cookie（如 refresh cookie），优先尝试轻量级 /auth/refresh 续期
            if cached_cookies:
                refreshed = await self._try_refresh_komiic(base_url, cached_cookies)
                if refreshed is not None:
                    await self._save_section(
                        "komiic",
                        {
                            "cred_fp": fp,
                            "cookies": refreshed,
                            "updated_at": time.time(),
                        },
                    )
                    logger.info("Komiic Token 自动续期 (/auth/refresh) 成功。")
                    return {"Cookie": format_cookie_header(refreshed)}

            # 2. 调用 /api/login 执行完整邮箱密码登录
            fresh_cookies = await self._login_komiic(base_url, cfg.username, cfg.password)
            await self._save_section(
                "komiic",
                {
                    "cred_fp": fp,
                    "cookies": fresh_cookies,
                    "updated_at": time.time(),
                },
            )
            logger.info("Komiic 账号登录成功，已更新并持久化本地 Cookie。")
            return {"Cookie": format_cookie_header(fresh_cookies)}

    async def _try_refresh_komiic(
        self, base_url: str, existing_cookies: dict[str, str]
    ) -> dict[str, str] | None:
        try:
            resp = await self._http.post(
                f"{base_url}/auth/refresh",
                headers={
                    "Content-Type": "application/json",
                    "Origin": base_url,
                    "Referer": f"{base_url}/",
                    "Cookie": format_cookie_header(existing_cookies),
                },
            )
            if 200 <= resp.status < 300 and resp.cookies:
                merged = dict(existing_cookies)
                merged.update(resp.cookies)
                if merged.get("komiic-access-token"):
                    return merged
        except Exception as exc:
            logger.debug(f"Komiic /auth/refresh 续期未成功，将回退重新登录：{exc}")
        return None

    async def _login_komiic(self, base_url: str, email: str, password: str) -> dict[str, str]:
        resp = await self._http.post(
            f"{base_url}/api/login",
            headers={
                "Content-Type": "application/json",
                "Origin": base_url,
                "Referer": f"{base_url}/login",
            },
            json_body={"email": email.strip(), "password": password},
        )
        if 200 <= resp.status < 300:
            cookies = dict(resp.cookies)
            # 兼容若响应体同时返回 token 字段
            if not cookies.get("komiic-access-token"):
                try:
                    body_json = resp.json()
                    tok = str(body_json.get("token") or "").strip()
                    if tok:
                        cookies["komiic-access-token"] = tok
                except Exception:
                    pass
            if cookies:
                return cookies
        err_detail = f"HTTP {resp.status}"
        try:
            err_json = resp.json()
            if isinstance(err_json, dict) and err_json.get("error"):
                err_detail = str(err_json["error"])
        except Exception:
            pass
        raise SourceAuthError(f"Komiic 账号登录失败：{err_detail}")
