"""Shared runtime configuration for isolated KOL signal-source copies."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
from pathlib import Path
from typing import Any, Mapping, TypeVar
from urllib.parse import urlsplit, urlunsplit


logger = logging.getLogger(__name__)
T = TypeVar("T")
USER_CONFIG_PATH = Path.home() / ".config" / "alphapumphunter" / "kol_sources.json"
DEFAULT_CONFIG_PATH = os.getenv("KOL_SOURCES_CONFIG") or (
    str(USER_CONFIG_PATH) if USER_CONFIG_PATH.exists() else "kol_sources.json"
)
VALID_PROXY_SCHEMES = {"http", "https", "socks5", "socks5h"}


def load_sources_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config_path = Path(path)
    try:
        value = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Missing KOL source config {config_path}; start from kol_sources.example.json"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid KOL source config {config_path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("KOL source config root must be an object")
    return value


def source_config(source_name: str, config: Mapping[str, Any]) -> dict[str, Any]:
    defaults = config.get("defaults") or {}
    sources = config.get("sources") or {}
    source = sources.get(source_name) or {}
    if not isinstance(defaults, dict) or not isinstance(source, dict):
        raise RuntimeError(f"Invalid settings for source {source_name}")
    merged = dict(defaults)
    merged.update(source)
    merged["name"] = source_name
    return merged


def setting(source: Mapping[str, Any], key: str, default: T) -> T:
    value = source.get(key, default)
    if isinstance(default, bool):
        return bool(value)  # type: ignore[return-value]
    if isinstance(default, int) and not isinstance(default, bool):
        return int(value)  # type: ignore[return-value]
    if isinstance(default, float):
        return float(value)  # type: ignore[return-value]
    if isinstance(default, str):
        return str(value)  # type: ignore[return-value]
    return value


def _proxy_definition(source: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    proxy_ref = source.get("proxy", "default")
    if proxy_ref in (None, False, "direct"):
        return {"enabled": False}
    if isinstance(proxy_ref, dict):
        return dict(proxy_ref)
    proxies = config.get("proxies") or {}
    proxy = proxies.get(str(proxy_ref)) or {}
    if not isinstance(proxy, dict):
        raise RuntimeError(f"Proxy {proxy_ref!r} must be an object")
    return dict(proxy)


def _select_proxy_url(source_name: str, proxy: Mapping[str, Any]) -> str:
    urls = proxy.get("urls") or ([] if not proxy.get("url") else [proxy.get("url")])
    urls = [str(url).strip() for url in urls if str(url).strip()]
    if not urls:
        return ""
    override = os.getenv("KOL_PROXY_INDEX")
    if override is not None:
        index = int(override) % len(urls)
    elif str(proxy.get("strategy", "source_hash")) == "random":
        index = random.randrange(len(urls))
    else:
        digest = hashlib.sha256(source_name.encode("utf-8")).digest()
        index = int.from_bytes(digest[:4], "big") % len(urls)
    url = urls[index]
    scheme = urlsplit(url).scheme.lower()
    if scheme not in VALID_PROXY_SCHEMES:
        raise RuntimeError(
            f"Unsupported proxy scheme {scheme!r}; use http, https, socks5, or socks5h"
        )
    return url


def mask_proxy_url(url: str) -> str:
    parts = urlsplit(url)
    hostname = parts.hostname or ""
    port = f":{parts.port}" if parts.port else ""
    username = f"{parts.username}:***@" if parts.username else ""
    return urlunsplit((parts.scheme, f"{username}{hostname}{port}", "", "", ""))


def configure_source(source_name: str, path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    config = load_sources_config(path)
    source = source_config(source_name, config)
    if not bool(source.get("enabled", True)):
        raise SystemExit(f"KOL source {source_name} is disabled in {path}")
    proxy = _proxy_definition(source, config)
    if bool(proxy.get("enabled", False)):
        url = _select_proxy_url(source_name, proxy)
        if not url:
            raise RuntimeError(f"Proxy is enabled for {source_name}, but no proxy URL is configured")
        os.environ["HTTP_PROXY"] = url
        os.environ["HTTPS_PROXY"] = url
        os.environ["ALL_PROXY"] = url
        bypass = proxy.get("no_proxy") or []
        if bypass:
            os.environ["NO_PROXY"] = ",".join(str(item) for item in bypass)
        logger.info("KOL source %s using proxy %s", source_name, mask_proxy_url(url))
        source["selected_proxy"] = mask_proxy_url(url)
    else:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            os.environ.pop(name, None)
        source["selected_proxy"] = "direct"
    return source


def assert_safe_source_config(config: Mapping[str, Any]) -> None:
    sources = config.get("sources") or {}
    required = {
        "alpha_surge",
        "top_pump_energy",
        "pullback_consolidation",
        "instant_opportunity",
        "instant_drop_trigger",
    }
    missing = required.difference(sources)
    if missing:
        raise RuntimeError(f"Missing KOL source settings: {', '.join(sorted(missing))}")
    for name, source in sources.items():
        if not re.fullmatch(r"[a-z0-9_]+", str(name)) or not isinstance(source, dict):
            raise RuntimeError(f"Invalid source entry: {name!r}")
