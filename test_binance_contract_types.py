import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from binance_contract_types import (
    build_contract_types,
    load_contract_types,
    resolve_contract_type,
    write_contract_types,
)
from update_binance_contract_types import EXCHANGE_INFO_URL, update_contract_types


class ContractTypeCacheTests(unittest.TestCase):
    def test_exchange_info_uses_exact_symbol_without_quarterly_override(self):
        contract_types = build_contract_types(
            {
                "symbols": [
                    {"symbol": "BTCUSDT", "quoteAsset": "USDT", "contractType": "PERPETUAL"},
                    {
                        "symbol": "BTCUSDT_260925",
                        "quoteAsset": "USDT",
                        "contractType": "CURRENT_QUARTER",
                    },
                    {
                        "symbol": "XAUUSDT",
                        "quoteAsset": "USDT",
                        "contractType": "TRADIFI_PERPETUAL",
                    },
                    {"symbol": "BTCBUSD", "quoteAsset": "BUSD", "contractType": "PERPETUAL"},
                ]
            }
        )

        self.assertEqual(contract_types["BTCUSDT"], "PERPETUAL")
        self.assertEqual(contract_types["BTCUSDT_260925"], "CURRENT_QUARTER")
        self.assertEqual(contract_types["XAUUSDT"], "TRADIFI_PERPETUAL")
        self.assertNotIn("BTCBUSD", contract_types)

    def test_cache_reload_observes_atomic_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "contract_types.json"
            write_contract_types({"XAUUSDT": "TRADIFI_PERPETUAL"}, cache_path)
            self.assertEqual(resolve_contract_type("xau", path=cache_path), "TRADIFI_PERPETUAL")

            write_contract_types({"XAUUSDT": "PERPETUAL"}, cache_path)
            self.assertEqual(resolve_contract_type("XAUUSDT", path=cache_path), "PERPETUAL")
            self.assertEqual(load_contract_types(cache_path), {"XAUUSDT": "PERPETUAL"})


class ContractTypeUpdaterTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_fetches_fapi_exchange_info_and_writes_cache(self):
        response = mock.Mock()
        response.json.return_value = {
            "symbols": [
                {
                    "symbol": "XAUUSDT",
                    "quoteAsset": "USDT",
                    "contractType": "TRADIFI_PERPETUAL",
                }
            ]
        }
        client = mock.AsyncMock()
        client.get.return_value = response
        client_context = mock.MagicMock()
        client_context.__aenter__ = mock.AsyncMock(return_value=client)
        client_context.__aexit__ = mock.AsyncMock(return_value=False)

        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "contract_types.json"
            with mock.patch(
                "update_binance_contract_types.httpx.AsyncClient", return_value=client_context
            ):
                self.assertEqual(await update_contract_types(cache_path), 1)

            client.get.assert_awaited_once_with(EXCHANGE_INFO_URL)
            with cache_path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
            self.assertEqual(payload["contract_types"], {"XAUUSDT": "TRADIFI_PERPETUAL"})


if __name__ == "__main__":
    unittest.main()
