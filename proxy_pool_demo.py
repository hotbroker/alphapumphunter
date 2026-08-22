#!/usr/bin/env python3
"""Build a local Mihomo round-robin proxy-pool config from a VMess subscription."""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


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
    check_interval = int(data.get("health_check_interval_seconds", 300))
    if check_interval < 30:
        raise RuntimeError("health_check_interval_seconds must be at least 30")
    return PoolConfig(
        subscription_url_file=Path(url_file).expanduser(),
        output_path=Path(output_path).expanduser(),
        exclude_name_patterns=tuple(patterns),
        listen_port=listen_port,
        health_check_url=str(data.get("health_check_url", "https://www.gstatic.com/generate_204")),
        health_check_interval_seconds=check_interval,
    )


async def fetch_subscription(url: str) -> str:
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        response = await client.get(url)
        response.raise_for_status()
    return response.text


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
            logger.warning("Skipping malformed VMess node: {}", exc)
    if not nodes:
        raise RuntimeError("Subscription did not contain valid VMess nodes")
    return nodes


def filter_nodes(nodes: list[VmessNode], patterns: tuple[str, ...]) -> tuple[list[VmessNode], int]:
    excluded = 0
    included: list[VmessNode] = []
    compiled_patterns = tuple(re.compile(pattern) for pattern in patterns)
    for node in nodes:
        if any(pattern.search(node.name) for pattern in compiled_patterns):
            excluded += 1
        else:
            included.append(node)
    if not included:
        raise RuntimeError("All subscription nodes were excluded by exclude_name_patterns")
    return included, excluded


def _node_name(node: VmessNode, index: int, used_names: set[str]) -> str:
    name = node.name
    if name not in used_names:
        used_names.add(name)
        return name
    unique_name = f"{name} #{index + 1}"
    while unique_name in used_names:
        index += 1
        unique_name = f"{name} #{index + 1}"
    used_names.add(unique_name)
    return unique_name


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
    tls = str(payload.get("tls") or "").lower() == "tls"
    if tls:
        proxy["tls"] = True
        if payload.get("sni"):
            proxy["servername"] = str(payload["sni"])
        if payload.get("fp"):
            proxy["client-fingerprint"] = str(payload["fp"])
        if payload.get("alpn"):
            proxy["alpn"] = [item for item in str(payload["alpn"]).split(",") if item]

    network = str(payload.get("net") or "tcp").lower()
    if network == "ws":
        proxy["network"] = "ws"
        headers: dict[str, str] = {}
        if payload.get("host"):
            headers["Host"] = str(payload["host"])
        proxy["ws-opts"] = {"path": str(payload.get("path") or "/"), "headers": headers}
    elif network == "grpc":
        proxy["network"] = "grpc"
        proxy["grpc-opts"] = {"grpc-service-name": str(payload.get("path") or "")}
    elif network not in ("", "tcp"):
        proxy["network"] = network
    return proxy


def build_mihomo_config(nodes: list[VmessNode], config: PoolConfig) -> dict[str, Any]:
    shuffled_nodes = list(nodes)
    random.SystemRandom().shuffle(shuffled_nodes)
    used_names: set[str] = set()
    proxies = [
        to_mihomo_proxy(node, _node_name(node, index, used_names))
        for index, node in enumerate(shuffled_nodes)
    ]
    names = [str(proxy["name"]) for proxy in proxies]
    return {
        "mixed-port": config.listen_port,
        "allow-lan": False,
        "bind-address": "127.0.0.1",
        "mode": "rule",
        "log-level": "info",
        "ipv6": False,
        "proxies": proxies,
        "proxy-groups": [
            {
                "name": "SUBSCRIPTION-POOL",
                "type": "load-balance",
                "strategy": "round-robin",
                "url": config.health_check_url,
                "interval": config.health_check_interval_seconds,
                "proxies": names,
            }
        ],
        "rules": ["MATCH,SUBSCRIPTION-POOL"],
    }


def write_private_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary_path, path)
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


async def refresh(config: PoolConfig) -> tuple[int, int]:
    subscription = await fetch_subscription(_read_subscription_url(config.subscription_url_file))
    nodes = parse_vmess_subscription(subscription)
    allowed_nodes, excluded_count = filter_nodes(nodes, config.exclude_name_patterns)
    write_private_json(config.output_path, build_mihomo_config(allowed_nodes, config))
    logger.info(
        "Wrote {} non-excluded VMess nodes to {}; excluded {} node(s)",
        len(allowed_nodes),
        config.output_path,
        excluded_count,
    )
    return len(allowed_nodes), excluded_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate a filtered Mihomo VMess proxy-pool config")
    parser.add_argument("--config", required=True, help="Path to the private demo JSON config")
    return parser


async def async_main() -> None:
    args = build_parser().parse_args()
    await refresh(load_config(args.config))


if __name__ == "__main__":
    asyncio.run(async_main())
