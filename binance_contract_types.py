"""Local cache helpers for Binance USD-M futures contract types."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from loguru import logger


DEFAULT_CONTRACT_TYPES_PATH = Path(
    os.getenv("BINANCE_CONTRACT_TYPES_PATH")
    or Path(__file__).resolve().with_name("binance_contract_types.json")
)

_cache_path: Optional[Path] = None
_cache_mtime_ns: Optional[int] = None
_cache: dict[str, str] = {}
_missing_pairs_logged: set[str] = set()


def normalize_usdt_pair(symbol: str) -> str:
    """Convert a base asset or USDT pair into the continuous-klines pair name."""
    normalized = str(symbol).upper().strip()
    return normalized if normalized.endswith("USDT") else f"{normalized}USDT"


def contract_types_path(path: str | Path | None = None) -> Path:
    return Path(path) if path is not None else DEFAULT_CONTRACT_TYPES_PATH


def load_contract_types(path: str | Path | None = None) -> dict[str, str]:
    """Load the cache only when its on-disk version changed."""
    global _cache_path, _cache_mtime_ns, _cache

    cache_file = contract_types_path(path)
    try:
        mtime_ns = cache_file.stat().st_mtime_ns
    except FileNotFoundError:
        if _cache_path == cache_file:
            _cache_mtime_ns = None
            _cache = {}
        return {}
    except OSError as exc:
        logger.warning("Unable to stat Binance contract type cache {}: {}", cache_file, exc)
        return {}

    if _cache_path == cache_file and _cache_mtime_ns == mtime_ns:
        return _cache

    try:
        with cache_file.open("r", encoding="utf-8") as file:
            payload = json.load(file)
        raw_types = payload.get("contract_types", {}) if isinstance(payload, dict) else {}
        if not isinstance(raw_types, dict):
            raise ValueError("contract_types must be an object")
        loaded = {
            str(pair).upper(): str(contract_type).upper()
            for pair, contract_type in raw_types.items()
            if pair and contract_type
        }
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.warning("Unable to load Binance contract type cache {}: {}", cache_file, exc)
        return {}

    _cache_path = cache_file
    _cache_mtime_ns = mtime_ns
    _cache = loaded
    return _cache


def resolve_contract_type(
    symbol: str,
    *,
    fallback: str = "PERPETUAL",
    path: str | Path | None = None,
) -> str:
    """Return the cached contract type for a continuous-klines USDT pair."""
    pair = normalize_usdt_pair(symbol)
    contract_type = load_contract_types(path).get(pair)
    if contract_type:
        return contract_type

    if pair not in _missing_pairs_logged:
        logger.warning(
            "No cached Binance contract type for {}; using fallback {}",
            pair,
            fallback,
        )
        _missing_pairs_logged.add(pair)
    return fallback.upper()


def build_contract_types(exchange_info: Mapping[str, object]) -> dict[str, str]:
    """Extract exact USDT symbols so quarterly contracts cannot replace a perp pair."""
    symbols = exchange_info.get("symbols", [])
    if not isinstance(symbols, list):
        raise ValueError("exchangeInfo.symbols must be an array")

    contract_types: dict[str, str] = {}
    for item in symbols:
        if not isinstance(item, dict) or str(item.get("quoteAsset", "")).upper() != "USDT":
            continue
        symbol = str(item.get("symbol", "")).upper()
        contract_type = str(item.get("contractType", "")).upper()
        if symbol and contract_type:
            contract_types[symbol] = contract_type
    if not contract_types:
        raise ValueError("exchangeInfo did not contain any USDT contract types")
    return contract_types


def write_contract_types(
    contract_types: Mapping[str, str], path: str | Path | None = None
) -> Path:
    """Atomically replace the cache so concurrent readers always see valid JSON."""
    cache_file = contract_types_path(path)
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "contract_types": dict(sorted(contract_types.items())),
    }
    temp_file = cache_file.with_name(f".{cache_file.name}.{os.getpid()}.tmp")
    try:
        with temp_file.open("w", encoding="utf-8") as file:
            json.dump(payload, file, ensure_ascii=True, sort_keys=True)
            file.write("\n")
        os.replace(temp_file, cache_file)
    finally:
        try:
            temp_file.unlink(missing_ok=True)
        except OSError:
            pass
    return cache_file
