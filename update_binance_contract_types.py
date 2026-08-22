#!/usr/bin/env python3
"""Refresh Binance USD-M futures contract types into a local JSON cache."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import httpx
from loguru import logger

from binance_contract_types import (
    DEFAULT_CONTRACT_TYPES_PATH,
    build_contract_types,
    write_contract_types,
)


EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
REQUEST_HEADERS = {"User-Agent": "alphapumphunter-contract-type-cache/1.0"}


async def update_contract_types(output_path: str | Path = DEFAULT_CONTRACT_TYPES_PATH) -> int:
    async with httpx.AsyncClient(timeout=20.0, headers=REQUEST_HEADERS) as client:
        response = await client.get(EXCHANGE_INFO_URL)
        response.raise_for_status()
        exchange_info = response.json()
    if not isinstance(exchange_info, dict):
        raise RuntimeError("Binance exchangeInfo response was not an object")
    contract_types = build_contract_types(exchange_info)
    cache_file = write_contract_types(contract_types, output_path)
    logger.info("Updated {} Binance USDT contract types in {}", len(contract_types), cache_file)
    return len(contract_types)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refresh Binance futures contract type cache")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_CONTRACT_TYPES_PATH),
        help="JSON cache path (default: %(default)s)",
    )
    parser.add_argument(
        "--interval-seconds",
        type=float,
        default=3600.0,
        help="Refresh interval when running continuously (default: %(default)s)",
    )
    parser.add_argument("--once", action="store_true", help="Refresh once and exit")
    return parser


async def async_main() -> None:
    args = build_parser().parse_args()
    if args.interval_seconds < 60:
        raise SystemExit("--interval-seconds must be at least 60")

    while True:
        try:
            await update_contract_types(args.output)
        except Exception:
            logger.exception("Unable to refresh Binance contract type cache")
            if args.once:
                raise
        if args.once:
            return
        await asyncio.sleep(args.interval_seconds)


if __name__ == "__main__":
    asyncio.run(async_main())
