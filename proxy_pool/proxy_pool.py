#!/usr/bin/env python3
"""Generate a filtered Mihomo round-robin pool from a Base64 VMess subscription."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_EXCLUDE_NAME_PATTERNS = [
    r"(?i)(?:^|[^A-Z])(?:US|USA)(?:$|[^A-Z])",
    r"(?i)united[ _-]*states|america",
    r"美国|🇺🇸",
]


@dataclass(frozen=True)
class PoolConfig:
    subscription_url_file: Path
    output_path: Path
    exclude_name_patterns: tuple[str, ...]
    listen_port: int
    health_check_url: str
    health_check_interval_seconds: int


@dataclass(frozen=True)
class VmessNode:
    name: str
    payload: dict[str, Any]


def _decode_base64(value: str) -> bytes:
    compact = "".join(value.split())
    return base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4))


def _read_subscription_url(path: Path) -> str:
    try:
        url = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError as exc:
        raise RuntimeError(f"Subscription URL file does not exist: {path}") from exc
    if not url.startswith(("https://", "http://")):
        raise RuntimeError("Subscription URL must start with https:// or http://")
    return url


def load_config(path: str | Path) -> PoolConfig:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"Missing proxy-pool config: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Proxy-pool config root must be an object")

    url_file = str(data.get("subscription_url_file", "")).strip()
    output_path = str(data.get("output_path", "")).strip()
    if not url_file or not output_path:
        raise RuntimeError("subscription_url_file and output_path are required")
    patterns = data.get("exclude_name_patterns", DEFAULT_EXCLUDE_NAME_PATTERNS)
    if not isinstance(patterns, list) or not all(isinstance(item, str) for item in patterns):
        raise RuntimeError("exclude_name_patterns must be a list of regular expressions")
    for pattern in patterns:
        re.compile(pattern)

    listen_port = int(data.get("listen_port", 7890))
    if not 1 <= listen_port <= 65535:
        raise RuntimeError("listen_port must be between 1 and 65535")
    interval = int(data.get("health_check_interval_seconds", 300))
    if interval < 30:
        raise RuntimeError("health_check_interval_seconds must be at least 30")
    return PoolConfig(
        subscription_url_file=Path(url_file).expanduser(),
        output_path=Path(output_path).expanduser(),
        exclude_name_patterns=tuple(patterns),
        listen_port=listen_port,
        health_check_url=str(data.get("health_check_url", "https://www.gstatic.com/generate_204")),
        health_check_interval_seconds=interval,
    )


def _fetch_subscription_sync(url: str) -> str:
    request = Request(url, headers={"User-Agent": "mihomo-proxy-pool/1.0"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read().decode("utf-8")
    except (HTTPError, URLError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"Unable to download subscription: {exc}") from exc


async def fetch_subscription(url: str) -> str:
    return await asyncio.to_thread(_fetch_subscription_sync, url)


def parse_vmess_subscription(subscription: str) -> list[VmessNode]:
    try:
        decoded = _decode_base64(subscription).decode("utf-8")
    except (UnicodeDecodeError, ValueError) as exc:
        raise RuntimeError("Subscription is not a valid Base64 VMess list") from exc

    nodes: list[VmessNode] = []
    for line in decoded.splitlines():
        if not line.startswith("vmess://"):
            continue
        try:
            payload = json.loads(_decode_base64(line.removeprefix("vmess://")).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("VMess payload is not an object")
            name = str(payload.get("ps", "")).strip()
            server = str(payload.get("add", "")).strip()
            uuid = str(payload.get("id", "")).strip()
            port = int(payload.get("port", 0))
            if not name or not server or not uuid or not 1 <= port <= 65535:
                raise ValueError("VMess node is missing name, server, UUID, or port")
            nodes.append(VmessNode(name=name, payload=payload))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            print(f"Skipping malformed VMess node: {exc}", file=sys.stderr)
    if not nodes:
        raise RuntimeError("Subscription did not contain valid VMess nodes")
    return nodes


def filter_nodes(nodes: list[VmessNode], patterns: tuple[str, ...]) -> tuple[list[VmessNode], int]:
    compiled = tuple(re.compile(pattern) for pattern in patterns)
    included = [node for node in nodes if not any(pattern.search(node.name) for pattern in compiled)]
    if not included:
        raise RuntimeError("All subscription nodes were excluded by exclude_name_patterns")
    return included, len(nodes) - len(included)


def _unique_name(node: VmessNode, index: int, used: set[str]) -> str:
    name = node.name
    if name not in used:
        used.add(name)
        return name
    candidate = f"{name} #{index + 1}"
    while candidate in used:
        index += 1
        candidate = f"{name} #{index + 1}"
    used.add(candidate)
    return candidate


def to_mihomo_proxy(node: VmessNode, name: str) -> dict[str, Any]:
    payload = node.payload
    proxy: dict[str, Any] = {
        "name": name,
        "type": "vmess",
        "server": str(payload["add"]),
        "port": int(payload["port"]),
        "uuid": str(payload["id"]),
        "alterId": int(payload.get("aid") or 0),
        "cipher": str(payload.get("scy") or "auto"),
        "udp": True,
    }
    if str(payload.get("tls") or "").lower() == "tls":
        proxy["tls"] = True
        if payload.get("sni"):
            proxy["servername"] = str(payload["sni"])
        if payload.get("fp"):
            proxy["client-fingerprint"] = str(payload["fp"])
        if payload.get("alpn"):
            proxy["alpn"] = [part for part in str(payload["alpn"]).split(",") if part]

    network = str(payload.get("net") or "tcp").lower()
    if network == "ws":
        headers = {"Host": str(payload["host"])} if payload.get("host") else {}
        proxy.update({"network": "ws", "ws-opts": {"path": str(payload.get("path") or "/"), "headers": headers}})
    elif network == "grpc":
        proxy.update({"network": "grpc", "grpc-opts": {"grpc-service-name": str(payload.get("path") or "")}})
    elif network not in ("", "tcp"):
        proxy["network"] = network
    return proxy


def build_mihomo_config(nodes: list[VmessNode], config: PoolConfig) -> dict[str, Any]:
    ordered = list(nodes)
    random.SystemRandom().shuffle(ordered)
    used_names: set[str] = set()
    proxies = [to_mihomo_proxy(node, _unique_name(node, index, used_names)) for index, node in enumerate(ordered)]
    return {
        "mixed-port": config.listen_port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "proxies": proxies,
        "proxy-groups": [{
            "name": "SUBSCRIPTION-POOL",
            "type": "load-balance",
            "strategy": "round-robin",
            "url": config.health_check_url,
            "interval": config.health_check_interval_seconds,
            "proxies": [proxy["name"] for proxy in proxies],
        }],
        "rules": ["MATCH,SUBSCRIPTION-POOL"],
    }


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


async def refresh(config: PoolConfig) -> tuple[int, int]:
    subscription = await fetch_subscription(_read_subscription_url(config.subscription_url_file))
    nodes = parse_vmess_subscription(subscription)
    allowed, excluded = filter_nodes(nodes, config.exclude_name_patterns)
    write_private_json(config.output_path, build_mihomo_config(allowed, config))
    print(f"Wrote {len(allowed)} non-excluded VMess nodes; excluded {excluded} node(s).")
    return len(allowed), excluded


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Generate a filtered Mihomo VMess proxy-pool config")
    parser.add_argument("--config", required=True, help="Path to the private JSON config")
    args = parser.parse_args()
    await refresh(load_config(args.config))


if __name__ == "__main__":
    asyncio.run(async_main())
