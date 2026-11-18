"""插件配置数据类解析与安全脱敏工具。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .models import SourceId

DEFAULT_ZMH_SEARCH_URL = "https://zmh.shadows.de5.net/?needLogin=only1"
DEFAULT_COPYMANGA_API_DOMAIN = "api.mangacopy.com"
DEFAULT_COPYMANGA_WEB_DOMAIN = "www.mangacopy.com"
DEFAULT_KOMIIC_BASE_URL = "https://komiic.com"
DEFAULT_SOURCE_PRIORITY: tuple[SourceId, ...] = (
    SourceId.ZAIMANHUA,
    SourceId.COPYMANGA,
    SourceId.KOMIIC,
)


def _as_dict(val: Any) -> dict[str, Any]:
    return val if isinstance(val, dict) else {}


def _clamp_int(val: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(val)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


@dataclass(slots=True)
class ZaimanhuaConfig:
    enabled: bool = True
    search_site_url: str = DEFAULT_ZMH_SEARCH_URL
    need_login_filter: str = "any"
    username: str = ""
    password: str = ""
    cookie: str = ""
    prefer_cookie: bool = False
    auto_sign_in: bool = False


@dataclass(slots=True)
class CopymangaConfig:
    enabled: bool = True
    api_domain: str = DEFAULT_COPYMANGA_API_DOMAIN
    web_domain: str = DEFAULT_COPYMANGA_WEB_DOMAIN
    image_resolution: str = "1500"
    token: str = ""


@dataclass(slots=True)
class KomiicConfig:
    enabled: bool = True
    base_url: str = DEFAULT_KOMIIC_BASE_URL
    username: str = ""
    password: str = ""
    cookie: str = ""
    prefer_cookie: bool = False


@dataclass(slots=True)
class CommentsConfig:
    enable_comments: bool = True
    comments_per_page: int = 20
    max_comments: int = 60
    t2i_endpoint: str = ""
    theme: str = "paper"
    fallback_to_text: bool = True


@dataclass(slots=True)
class DeliveryConfig:
    include_header_node: bool = True
    max_pages_per_forward: int = 60
    max_mb_per_forward: int = 28
    send_wait_tip: bool = True
    recall_wait_tip: bool = True


@dataclass(slots=True)
class NetworkConfig:
    proxy: str = ""
    request_timeout_sec: int = 20
    download_concurrency: int = 6
    max_concurrent_tasks: int = 2


@dataclass(slots=True)
class PluginConfig:
    """强类型插件全局配置快照。"""

    source_priority: list[SourceId] = field(default_factory=lambda: list(DEFAULT_SOURCE_PRIORITY))
    zaimanhua: ZaimanhuaConfig = field(default_factory=ZaimanhuaConfig)
    copymanga: CopymangaConfig = field(default_factory=CopymangaConfig)
    komiic: KomiicConfig = field(default_factory=KomiicConfig)
    comments: CommentsConfig = field(default_factory=CommentsConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)

    def enabled_sources_in_order(self, forced_source: SourceId | None = None) -> list[SourceId]:
        """按配置的先后优先级返回当前已启用的漫画源列表。"""
        if forced_source is not None:
            return [forced_source]
        enabled_map = {
            SourceId.ZAIMANHUA: self.zaimanhua.enabled,
            SourceId.COPYMANGA: self.copymanga.enabled,
            SourceId.KOMIIC: self.komiic.enabled,
        }
        return [src for src in self.source_priority if enabled_map.get(src, False)]

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> PluginConfig:
        cfg = raw if isinstance(raw, dict) else {}

        # 解析漫画源优先级顺序（支持 list、逗号分隔字符串、中英文别名）
        raw_prio = cfg.get("source_priority")
        parsed_prio: list[SourceId] = []
        if isinstance(raw_prio, list):
            tokens = [str(item) for item in raw_prio]
        elif isinstance(raw_prio, str) and raw_prio.strip():
            tokens = [part.strip() for part in raw_prio.replace("，", ",").split(",")]
        else:
            tokens = [s.value for s in DEFAULT_SOURCE_PRIORITY]

        for tok in tokens:
            sid = SourceId.from_alias(tok)
            if sid is not None and sid not in parsed_prio:
                parsed_prio.append(sid)

        # 若用户在多选列表漏选了某个已启用源，按默认顺序补在末尾，避免启用开关失效；
        # 若用户确实想禁用某源，通过各源自身的 enabled 开关控制。
        for default_sid in DEFAULT_SOURCE_PRIORITY:
            if default_sid not in parsed_prio:
                parsed_prio.append(default_sid)

        zmh_raw = _as_dict(cfg.get("zaimanhua"))
        need_login_val = str(zmh_raw.get("need_login_filter") or "any").strip()
        if need_login_val not in ("any", "only1", "only0"):
            need_login_val = "any"
        zmh_cfg = ZaimanhuaConfig(
            enabled=bool(zmh_raw.get("enabled", True)),
            search_site_url=(
                str(zmh_raw.get("search_site_url") or DEFAULT_ZMH_SEARCH_URL).strip()
                or DEFAULT_ZMH_SEARCH_URL
            ),
            need_login_filter=need_login_val,
            username=str(zmh_raw.get("username") or "").strip(),
            password=str(zmh_raw.get("password") or ""),
            cookie=str(zmh_raw.get("cookie") or "").strip(),
            prefer_cookie=bool(zmh_raw.get("prefer_cookie", False)),
            auto_sign_in=bool(zmh_raw.get("auto_sign_in", False)),
        )

        cm_raw = _as_dict(cfg.get("copymanga"))
        res_val = str(cm_raw.get("image_resolution") or "1500").strip()
        if res_val not in ("800", "1200", "1500"):
            res_val = "1500"
        cm_cfg = CopymangaConfig(
            enabled=bool(cm_raw.get("enabled", True)),
            api_domain=(
                str(cm_raw.get("api_domain") or DEFAULT_COPYMANGA_API_DOMAIN)
                .strip()
                .removeprefix("https://")
                .removeprefix("http://")
                .strip("/")
                or DEFAULT_COPYMANGA_API_DOMAIN
            ),
            web_domain=(
                str(cm_raw.get("web_domain") or DEFAULT_COPYMANGA_WEB_DOMAIN)
                .strip()
                .removeprefix("https://")
                .removeprefix("http://")
                .strip("/")
                or DEFAULT_COPYMANGA_WEB_DOMAIN
            ),
            image_resolution=res_val,
            token=str(cm_raw.get("token") or "").strip(),
        )

        km_raw = _as_dict(cfg.get("komiic"))
        km_base = str(km_raw.get("base_url") or DEFAULT_KOMIIC_BASE_URL).strip().rstrip("/")
        if km_base and not km_base.startswith(("http://", "https://")):
            km_base = f"https://{km_base}"
        km_cfg = KomiicConfig(
            enabled=bool(km_raw.get("enabled", True)),
            base_url=km_base or DEFAULT_KOMIIC_BASE_URL,
            username=str(km_raw.get("username") or "").strip(),
            password=str(km_raw.get("password") or ""),
            cookie=str(km_raw.get("cookie") or "").strip(),
            prefer_cookie=bool(km_raw.get("prefer_cookie", False)),
        )

        cmt_raw = _as_dict(cfg.get("comments"))
        theme_val = str(cmt_raw.get("theme") or "paper").strip().lower()
        if theme_val not in ("paper", "light", "dark"):
            theme_val = "paper"
        cmt_cfg = CommentsConfig(
            enable_comments=bool(cmt_raw.get("enable_comments", True)),
            comments_per_page=_clamp_int(cmt_raw.get("comments_per_page"), 20, 5, 50),
            max_comments=_clamp_int(cmt_raw.get("max_comments"), 60, 5, 200),
            t2i_endpoint=str(cmt_raw.get("t2i_endpoint") or "").strip(),
            theme=theme_val,
            fallback_to_text=bool(cmt_raw.get("fallback_to_text", True)),
        )

        dlv_raw = _as_dict(cfg.get("delivery"))
        dlv_cfg = DeliveryConfig(
            include_header_node=bool(dlv_raw.get("include_header_node", True)),
            max_pages_per_forward=_clamp_int(dlv_raw.get("max_pages_per_forward"), 60, 10, 150),
            max_mb_per_forward=_clamp_int(dlv_raw.get("max_mb_per_forward"), 28, 5, 80),
            send_wait_tip=bool(dlv_raw.get("send_wait_tip", True)),
            recall_wait_tip=bool(dlv_raw.get("recall_wait_tip", True)),
        )

        net_raw = _as_dict(cfg.get("network"))
        net_cfg = NetworkConfig(
            proxy=str(net_raw.get("proxy") or "").strip(),
            request_timeout_sec=_clamp_int(net_raw.get("request_timeout_sec"), 20, 5, 120),
            download_concurrency=_clamp_int(net_raw.get("download_concurrency"), 6, 1, 16),
            max_concurrent_tasks=_clamp_int(net_raw.get("max_concurrent_tasks"), 2, 1, 10),
        )

        return cls(
            source_priority=parsed_prio,
            zaimanhua=zmh_cfg,
            copymanga=cm_cfg,
            komiic=km_cfg,
            comments=cmt_cfg,
            delivery=dlv_cfg,
            network=net_cfg,
        )


def redact_secrets(text: str, secrets: list[str]) -> str:
    """将异常或日志文本中可能出现的密码/Token/Cookie 脱敏为 `***`。"""
    if not text:
        return ""
    masked = text
    for sec in secrets:
        clean = (sec or "").strip()
        if len(clean) >= 4 and clean in masked:
            masked = masked.replace(clean, "***")
    return masked
