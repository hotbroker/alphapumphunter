import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kol_publisher import (
    AIConfig,
    AccountConfig,
    AppConfig,
    AIWriter,
    KOLPublisher,
    MarketContextClient,
    PublishResult,
    PublisherConfig,
    SignalStore,
    SquareClient,
    SquareDailyPostLimitError,
    ToneConfig,
    build_messages,
    clean_generated_text,
    load_config,
    parse_recent_15m_klines,
    validate_config,
)
from kol_signal import connect, emit_signal, normalize_symbol


class KOLSignalTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "signals.db"

    def tearDown(self):
        self.tempdir.cleanup()

    def emit(self, indicator="alpha_surge", symbol="ACE", fingerprint=None):
        return emit_signal(
            symbol=symbol,
            source="test",
            indicator=indicator,
            direction="LONG",
            summary="10分钟上涨且成交量放大",
            details={"change_pct": 12.3},
            fingerprint=fingerprint,
            db_path=self.db_path,
        )

    def account(self, account_id="one"):
        return AccountConfig(
            account_id=account_id,
            square_api_key="test",
            tone=ToneConfig(),
        )

    def sync(self, store, *accounts):
        store.sync_deliveries(tuple(accounts), 3600)

    def test_normalizes_usdt_pair_and_deduplicates_fingerprint(self):
        first = self.emit(symbol="aceusdt", fingerprint="same")
        second = self.emit(symbol="ACE", fingerprint="same")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        with connect(self.db_path) as connection:
            row = connection.execute("SELECT symbol FROM signals").fetchone()
        self.assertEqual(row["symbol"], "ACE")

    def test_cooldown_is_per_symbol_and_indicator(self):
        first_id = self.emit(indicator="alpha_surge")
        other_id = self.emit(indicator="top_pump_energy")
        store = SignalStore(self.db_path)
        self.sync(store, self.account())
        first = store.claim_next()
        self.assertEqual(first["signal_id"], first_id)
        store.mark_published(first["delivery_id"], "$ACE 看多", PublishResult("1", "url"), dry_run=False)

        other = store.claim_next()
        self.assertEqual(other["signal_id"], other_id)
        self.assertIsNone(store.cooldown_match(other, 3600))

        third_id = self.emit(indicator="alpha_surge", fingerprint="new")
        store.mark_published(other["delivery_id"], "$ACE 看多", PublishResult("2", "url2"), dry_run=False)
        self.sync(store, self.account())
        third = store.claim_next()
        self.assertEqual(third["signal_id"], third_id)
        self.assertEqual(store.cooldown_match(third, 3600)["id"], first["delivery_id"])

    def test_expired_cooldown_allows_same_indicator(self):
        first_id = self.emit()
        store = SignalStore(self.db_path)
        self.sync(store, self.account())
        first = store.claim_next()
        store.mark_published(first["delivery_id"], "$ACE 看多", PublishResult("1", "url"), dry_run=False)
        with connect(self.db_path) as connection:
            connection.execute(
                "UPDATE deliveries SET published_at=? WHERE id=?",
                (time.time() - 3601, first["delivery_id"]),
            )
        self.emit(fingerprint="later")
        self.sync(store, self.account())
        second = store.claim_next()
        self.assertIsNone(store.cooldown_match(second, 3600))

    def test_same_signal_is_delivered_once_to_each_account(self):
        signal_id = self.emit()
        store = SignalStore(self.db_path)
        self.sync(store, self.account("one"), self.account("two"))
        first = store.claim_next()
        store.mark_published(first["delivery_id"], "$ACE 看多 A", PublishResult("1", "a"), dry_run=False)
        second = store.claim_next()
        self.assertEqual({first["account_id"], second["account_id"]}, {"one", "two"})
        self.assertEqual(first["signal_id"], signal_id)
        self.assertEqual(second["signal_id"], signal_id)
        self.assertIsNone(store.cooldown_match(second, 3600))
        self.assertEqual(
            store.generated_by_other_accounts(signal_id, second["account_id"]),
            ("$ACE 看多 A",),
        )

    def test_daily_limit_block_suppresses_one_account_without_blocking_others(self):
        first_id = self.emit(symbol="ACE")
        first_account = self.account("one")
        second_account = self.account("two")
        store = SignalStore(self.db_path)
        self.sync(store, first_account, second_account)

        blocked_until, suppressed_count = store.block_account_for_daily_post_limit(
            "one", 86400, SquareDailyPostLimitError("Square API [220009]: daily limit")
        )
        self.assertGreater(blocked_until, time.time())
        self.assertEqual(suppressed_count, 1)
        self.assertEqual(store.blocked_account_ids(), {"one"})

        second_id = self.emit(symbol="BTC")
        self.sync(store, first_account, second_account)
        with connect(self.db_path) as connection:
            rows = connection.execute(
                "SELECT signal_id, account_id, status FROM deliveries ORDER BY signal_id, account_id"
            ).fetchall()
        self.assertEqual(
            [(row["signal_id"], row["account_id"], row["status"]) for row in rows],
            [
                (first_id, "one", "suppressed"),
                (first_id, "two", "pending"),
                (second_id, "two", "pending"),
            ],
        )

    def test_discard_pending_marks_backlog_suppressed_without_recreating_it(self):
        signal_id = self.emit()
        account = self.account()
        store = SignalStore(self.db_path)
        self.sync(store, account)
        claimed = store.claim_next()
        self.assertEqual(claimed["signal_id"], signal_id)

        self.assertEqual(store.discard_pending(), 1)
        self.sync(store, account)
        with connect(self.db_path) as connection:
            row = connection.execute("SELECT status FROM deliveries").fetchone()
        self.assertEqual(row["status"], "suppressed")


class KOLCopyTests(unittest.TestCase):
    def test_cleaner_requires_cashtag_and_direction_and_removes_url(self):
        text = clean_generated_text(
            "这波量能起来了 https://example.com 冲就完了",
            symbol="ACE",
            direction="LONG",
            max_chars=120,
        )
        self.assertIn("$ACE", text)
        self.assertIn("看多", text)
        self.assertNotIn("http", text)

    def test_prompt_contains_configured_tone_and_signal_facts(self):
        signal = {
            "symbol": "ACE",
            "source": "main.py",
            "indicator": "alpha_surge",
            "direction": "LONG",
            "summary": "10分钟涨幅12%",
            "price": 0.5,
            "details_json": json.dumps({"change_pct": 12}),
            "kline_context": [
                {
                    "open_time_utc": "2026-08-19T08:00+00:00",
                    "open": 0.48,
                    "high": 0.52,
                    "low": 0.47,
                    "close": 0.5,
                }
            ],
        }
        messages = build_messages(
            signal, ToneConfig(name="冷静分析师"), 300, concise=True
        )
        self.assertIn("冷静分析师", messages[0]["content"])
        self.assertIn("$ACE", messages[0]["content"])
        self.assertIn("使用简短回复", messages[0]["content"])
        self.assertIn("10分钟涨幅12%", messages[1]["content"])
        self.assertIn("近2小时15分钟K线", messages[1]["content"])
        self.assertIn("2026-08-19T08:00+00:00", messages[1]["content"])

    def test_parses_latest_eight_15m_klines_for_ai_context(self):
        rows = []
        for index in range(9):
            open_time = 1_700_000_000_000 + index * 900_000
            rows.append(
                [
                    open_time,
                    "1.0",
                    "1.2",
                    "0.9",
                    "1.1",
                    "100",
                    open_time + 899_999,
                    str(1000 + index),
                    50 + index,
                    "60",
                    "600",
                ]
            )
        result = parse_recent_15m_klines(rows, now_ms=rows[-1][6] - 1)
        self.assertEqual(len(result), 8)
        self.assertEqual(result[0]["quote_volume"], 1001.0)
        self.assertEqual(result[-1]["trade_count"], 58)
        self.assertFalse(result[-1]["closed"])

    def test_example_config_is_multi_account_and_uses_luna(self):
        with mock.patch.dict("os.environ", {"KOL_AI_API_KEY": "test-key"}):
            config = load_config("kol_config.example.json")
        validate_config(config, require_square_key=False)
        self.assertEqual(config.ai.model, "gpt-5.6-luna")
        self.assertTrue(config.ai.concise)
        self.assertEqual(config.ai.max_chars, 300)
        self.assertEqual(config.publisher.daily_post_limit_pause_seconds, 86400)
        self.assertEqual(len(config.accounts), 2)
        self.assertEqual(config.accounts[0].account_id, "baolao_01")
        self.assertTrue(config.accounts[0].enabled)
        self.assertFalse(config.accounts[1].enabled)
        self.assertNotEqual(config.accounts[0].tone.name, config.accounts[1].tone.name)
        self.assertEqual(config.accounts[0].max_chars, 260)
        self.assertFalse(config.accounts[1].concise)

    def test_ai_writer_uses_ai_character_limit_below_platform_limit(self):
        writer = AIWriter(
            AIConfig(
                "https://example.com/v1",
                "key",
                "gpt-5.6-luna",
                concise=True,
                max_chars=180,
            ),
            ToneConfig(),
            1200,
        )
        self.assertEqual(writer.max_chars, 180)


class KOLMarketContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetches_eight_klines_once_per_signal(self):
        row = [
            1_700_000_000_000,
            "1.0",
            "1.2",
            "0.9",
            "1.1",
            "100",
            1_700_000_899_999,
            "1000",
            50,
            "60",
            "600",
        ]
        response = mock.Mock()
        response.json.return_value = [row] * 8
        client = mock.AsyncMock()
        client.get.return_value = response
        client_context = mock.MagicMock()
        client_context.__aenter__ = mock.AsyncMock(return_value=client)
        client_context.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch("kol_publisher.httpx.AsyncClient", return_value=client_context):
            market = MarketContextClient()
            first = await market.get(7, "ace")
            second = await market.get(7, "ACE")
        self.assertEqual(first, second)
        self.assertEqual(len(first), 8)
        client.get.assert_awaited_once()
        _, kwargs = client.get.await_args
        self.assertEqual(kwargs["params"]["symbol"], "ACEUSDT")
        self.assertEqual(kwargs["params"]["interval"], "15m")
        self.assertEqual(kwargs["params"]["limit"], 8)


class SquareClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_daily_limit_response_has_a_dedicated_error_type(self):
        response = mock.Mock()
        response.json.return_value = {
            "code": "220009",
            "message": "Daily post limit exceeded for OpenAPI",
        }
        client = mock.AsyncMock()
        client.post.return_value = response
        client_context = mock.MagicMock()
        client_context.__aenter__ = mock.AsyncMock(return_value=client)
        client_context.__aexit__ = mock.AsyncMock(return_value=False)
        with mock.patch("kol_publisher.httpx.AsyncClient", return_value=client_context):
            with self.assertRaisesRegex(SquareDailyPostLimitError, "220009"):
                await SquareClient("square-key").publish("$ACE 看多")


class KOLPublishFailureIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_square_daily_limit_pauses_account_and_discards_its_backlog(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "signals.db")
            for symbol in ("ACE", "BTC"):
                emit_signal(
                    symbol=symbol,
                    source="test",
                    indicator="alpha_surge",
                    direction="LONG",
                    summary="测试",
                    db_path=db_path,
                )
            account = AccountConfig(
                account_id="one",
                square_api_key="square-key",
                tone=ToneConfig(),
            )
            config = AppConfig(
                ai=AIConfig("https://example.com/v1", "ai-key", "gpt-5.6-luna"),
                accounts=(account,),
                publisher=PublisherConfig(
                    database_path=db_path,
                    feishu_enabled=True,
                    feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test",
                ),
            )
            publisher = KOLPublisher(config)
            with (
                mock.patch.object(AIWriter, "generate", mock.AsyncMock(return_value="$ACE 看多")),
                mock.patch.object(
                    publisher.market_context,
                    "get",
                    mock.AsyncMock(return_value=()),
                ),
                mock.patch.object(
                    SquareClient,
                    "publish",
                    mock.AsyncMock(
                        side_effect=SquareDailyPostLimitError(
                            "Square API [220009]: Daily post limit exceeded for OpenAPI"
                        )
                    ),
                ) as square_publish,
                mock.patch.object(
                    publisher, "notify_daily_post_limit", mock.AsyncMock()
                ) as notify_limit,
            ):
                self.assertTrue(await publisher.process_one())

            with connect(db_path) as connection:
                delivery_statuses = connection.execute(
                    "SELECT status FROM deliveries ORDER BY id"
                ).fetchall()
                account_block = connection.execute(
                    "SELECT account_id, blocked_until FROM account_publish_blocks"
                ).fetchone()
            self.assertEqual([row["status"] for row in delivery_statuses], ["suppressed", "suppressed"])
            self.assertEqual(account_block["account_id"], "one")
            self.assertGreater(account_block["blocked_until"], time.time())
            square_publish.assert_awaited_once()
            notify_limit.assert_awaited_once()

            emit_signal(
                symbol="ETH",
                source="test",
                indicator="alpha_surge",
                direction="LONG",
                summary="暂停期间新信号",
                db_path=db_path,
            )
            publisher.store.sync_deliveries(config.accounts, config.publisher.max_signal_age_seconds)
            with connect(db_path) as connection:
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM deliveries WHERE status='pending'"
                ).fetchone()
            self.assertEqual(pending["count"], 0)

    async def test_feishu_failure_does_not_retry_successful_square_post(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "signals.db")
            emit_signal(
                symbol="ACE",
                source="test",
                indicator="alpha_surge",
                direction="LONG",
                summary="测试",
                db_path=db_path,
            )
            account = AccountConfig(
                account_id="one",
                square_api_key="square-key",
                tone=ToneConfig(),
            )
            config = AppConfig(
                ai=AIConfig("https://example.com/v1", "ai-key", "gpt-5.6-luna"),
                accounts=(account,),
                publisher=PublisherConfig(
                    database_path=db_path,
                    feishu_enabled=True,
                    feishu_webhook="https://open.feishu.cn/open-apis/bot/v2/hook/test",
                ),
            )
            publisher = KOLPublisher(config)
            with (
                mock.patch.object(AIWriter, "generate", mock.AsyncMock(return_value="$ACE 看多")),
                mock.patch.object(
                    publisher.market_context,
                    "get",
                    mock.AsyncMock(return_value=()),
                ),
                mock.patch.object(
                    SquareClient,
                    "publish",
                    mock.AsyncMock(return_value=PublishResult("1", "https://post/1")),
                ) as square_publish,
                mock.patch.object(
                    publisher,
                    "notify_feishu",
                    mock.AsyncMock(side_effect=RuntimeError("Feishu unavailable")),
                ),
            ):
                self.assertTrue(await publisher.process_one())
            with connect(db_path) as connection:
                row = connection.execute("SELECT status, attempts FROM deliveries").fetchone()
            self.assertEqual(row["status"], "published")
            self.assertEqual(row["attempts"], 0)
            square_publish.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
