"""Generate signal-driven KOL copy and publish it to Binance Square."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

import httpx
from loguru import logger

from kol_signal import DEFAULT_DB_PATH, connect, emit_signal, normalize_symbol


USER_CONFIG_PATH = Path.home() / ".config" / "alphapumphunter" / "kol_config.json"
DEFAULT_CONFIG_PATH = os.getenv("KOL_CONFIG_PATH") or (
    str(USER_CONFIG_PATH) if USER_CONFIG_PATH.exists() else "kol_config.json"
)
SQUARE_POST_URL = (
    "https://www.binance.com/bapi/composite/v1/public/pgc/openApi/content/add"
)


@dataclass(frozen=True)
class AIConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.9
    timeout_seconds: float = 45.0


@dataclass(frozen=True)
class ToneConfig:
    name: str = "百度贴吧暴躁老哥"
    persona: str = "像混迹币圈贴吧多年的暴躁老哥，短句、有火气、观点直接，但分析必须讲事实。"
    requirements: tuple[str, ...] = (
        "开头直接点名币种和方向",
        "把指标依据翻译成人话，不堆砌无关数据",
        "不承诺稳赚，不编造目标价、胜率或消息面",
        "结尾用一句符合人设的风险提醒",
    )
    extra_prompt: str = ""


@dataclass(frozen=True)
class AccountConfig:
    account_id: str
    square_api_key: str
    tone: ToneConfig
    enabled: bool = True
    model: Optional[str] = None
    temperature: Optional[float] = None
    cooldown_seconds: int = 3600
    min_delay_seconds: float = 0.0
    max_delay_seconds: float = 0.0


@dataclass(frozen=True)
class PublisherConfig:
    poll_interval_seconds: float = 10.0
    max_post_chars: int = 1200
    retry_base_seconds: int = 30
    max_attempts: int = 8
    max_signal_age_seconds: int = 3600
    database_path: str = DEFAULT_DB_PATH
    dry_run: bool = False
    feishu_enabled: bool = False
    feishu_webhook: str = ""


@dataclass(frozen=True)
class AppConfig:
    ai: AIConfig
    accounts: tuple[AccountConfig, ...]
    publisher: PublisherConfig


@dataclass(frozen=True)
class PublishResult:
    post_id: Optional[str]
    post_url: Optional[str]
    raw: Mapping[str, Any] = field(default_factory=dict)


def _secret(value: Any, env_name: str) -> str:
    return os.getenv(env_name, "").strip() or str(value or "").strip()


def _tone(data: Mapping[str, Any], fallback: Optional[ToneConfig] = None) -> ToneConfig:
    default = fallback or ToneConfig()
    requirements = data.get("requirements") or default.requirements
    return ToneConfig(
        name=str(data.get("name") or default.name),
        persona=str(data.get("persona") or default.persona),
        requirements=tuple(str(item) for item in requirements),
        extra_prompt=str(data.get("extra_prompt") or default.extra_prompt),
    )


def _account_env_name(account_id: str) -> str:
    suffix = re.sub(r"[^A-Z0-9]", "_", account_id.upper())
    return f"BINANCE_SQUARE_OPENAPI_KEY_{suffix}"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = Path(path)
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(
            f"Missing {config_path}. Copy kol_config.example.json to {config_path} and fill credentials."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {config_path}: {exc}") from exc

    ai_data = data.get("ai") or {}
    publisher_data = data.get("publisher") or {}
    api_key = _secret(ai_data.get("api_key"), "KOL_AI_API_KEY")
    if not api_key:
        raise RuntimeError("Missing AI key: set KOL_AI_API_KEY or ai.api_key")
    default_tone = _tone(data.get("default_tone") or data.get("tone") or {})
    account_rows = data.get("accounts") or []
    if not isinstance(account_rows, list) or not account_rows:
        raise RuntimeError("accounts must contain at least one Binance Square account")
    accounts: list[AccountConfig] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(account_rows):
        account_id = str(row.get("id") or "").strip()
        if not account_id or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            raise RuntimeError(f"accounts[{index}].id must use letters, numbers, _ or -")
        if account_id in seen_ids:
            raise RuntimeError(f"Duplicate account id: {account_id}")
        seen_ids.add(account_id)
        delay = row.get("posting_delay_seconds") or [0, 0]
        if not isinstance(delay, list) or len(delay) != 2:
            raise RuntimeError(f"accounts[{index}].posting_delay_seconds must be [min, max]")
        account_ai = row.get("ai") or {}
        square_key = _secret(row.get("square_api_key"), _account_env_name(account_id))
        accounts.append(
            AccountConfig(
                account_id=account_id,
                square_api_key=square_key,
                tone=_tone(row.get("tone") or {}, default_tone),
                enabled=bool(row.get("enabled", True)),
                model=str(account_ai.get("model") or "").strip() or None,
                temperature=(
                    float(account_ai["temperature"])
                    if account_ai.get("temperature") is not None
                    else None
                ),
                cooldown_seconds=int(row.get("cooldown_seconds", 3600)),
                min_delay_seconds=float(delay[0]),
                max_delay_seconds=float(delay[1]),
            )
        )
    return AppConfig(
        ai=AIConfig(
            base_url=str(ai_data.get("base_url") or "").rstrip("/"),
            api_key=api_key,
            model=str(ai_data.get("model") or "").strip(),
            temperature=float(ai_data.get("temperature", 0.9)),
            timeout_seconds=float(ai_data.get("timeout_seconds", 45)),
        ),
        accounts=tuple(accounts),
        publisher=PublisherConfig(
            poll_interval_seconds=float(publisher_data.get("poll_interval_seconds", 10)),
            max_post_chars=int(publisher_data.get("max_post_chars", 1200)),
            retry_base_seconds=int(publisher_data.get("retry_base_seconds", 30)),
            max_attempts=int(publisher_data.get("max_attempts", 8)),
            max_signal_age_seconds=int(publisher_data.get("max_signal_age_seconds", 3600)),
            database_path=str(publisher_data.get("database_path") or DEFAULT_DB_PATH),
            dry_run=bool(publisher_data.get("dry_run", False)),
            feishu_enabled=bool(publisher_data.get("feishu_enabled", False)),
            feishu_webhook=_secret(
                publisher_data.get("feishu_webhook"), "KOL_FEISHU_WEBHOOK"
            ),
        ),
    )


def validate_config(config: AppConfig, *, require_square_key: bool = True) -> None:
    if not config.ai.base_url.startswith(("https://", "http://")):
        raise RuntimeError("ai.base_url must be an HTTP(S) URL")
    if not config.ai.model:
        raise RuntimeError("ai.model must not be empty")
    enabled = [account for account in config.accounts if account.enabled]
    if not enabled:
        raise RuntimeError("At least one account must be enabled")
    for account in enabled:
        if require_square_key and not config.publisher.dry_run and not account.square_api_key:
            raise RuntimeError(
                f"Missing Square key for account {account.account_id}; set "
                f"{_account_env_name(account.account_id)} or accounts[].square_api_key"
            )
        if account.cooldown_seconds < 0:
            raise RuntimeError(f"Account {account.account_id} cooldown must be non-negative")
        if account.min_delay_seconds < 0 or account.max_delay_seconds < account.min_delay_seconds:
            raise RuntimeError(f"Account {account.account_id} has an invalid posting delay range")
    if not 100 <= config.publisher.max_post_chars <= 2000:
        raise RuntimeError("publisher.max_post_chars must be between 100 and 2000")
    if config.publisher.feishu_enabled and not config.publisher.feishu_webhook.startswith(
        "https://open.feishu.cn/open-apis/bot/"
    ):
        raise RuntimeError("publisher.feishu_webhook must be a Feishu bot webhook URL")


def _chat_url(base_url: str) -> str:
    return base_url + "/chat/completions" if base_url.rstrip("/").endswith("/v1") else base_url + "/v1/chat/completions"


def build_messages(signal: Mapping[str, Any], tone: ToneConfig, max_chars: int) -> list[dict[str, str]]:
    symbol = normalize_symbol(str(signal["symbol"]))
    details = json.loads(signal.get("details_json") or "{}")
    direction_text = {"LONG": "看多/做多", "SHORT": "看空/做空", "WATCH": "观察等待"}[
        str(signal["direction"])
    ]
    requirements = "\n".join(f"- {item}" for item in tone.requirements)
    system = f"""你是币安广场的交易信号 KOL，当前口吻配置为“{tone.name}”。
人设：{tone.persona}

硬性规则：
- 只输出最终帖子正文，不要标题标签、解释、Markdown 代码块。
- 全文使用简体中文，控制在 {max_chars} 个字符以内。
- 必须原样包含可点击 cashtag ${symbol}，不得写任何 URL。
- 明确表达“{direction_text}”，不得把 LONG/SHORT 方向写反。
- 只能使用输入信号里的事实；不得捏造价格、涨幅、指标、新闻或内幕。
- 不得声称稳赚、保本，也不得煽动用户梭哈或借贷交易。
{requirements}
{tone.extra_prompt}""".strip()
    user = json.dumps(
        {
            "币种": symbol,
            "方向": direction_text,
            "指标来源": signal["source"],
            "指标类型": signal["indicator"],
            "信号摘要": signal["summary"],
            "触发价格": signal.get("price"),
            "详细数据": details,
        },
        ensure_ascii=False,
        indent=2,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def clean_generated_text(text: str, *, symbol: str, direction: str, max_chars: int) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:text|markdown)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned).strip()
    cashtag = f"${normalize_symbol(symbol)}"
    cashtag_pattern = rf"\${re.escape(normalize_symbol(symbol))}(?![A-Z0-9])"
    if not re.search(cashtag_pattern, cleaned, flags=re.IGNORECASE):
        cleaned = f"{cashtag} {cleaned}"
    direction_terms = ("做多", "看多") if direction == "LONG" else (("做空", "看空") if direction == "SHORT" else ("观察", "等待"))
    if not any(term in cleaned for term in direction_terms):
        label = {"LONG": "看多", "SHORT": "看空", "WATCH": "观察"}[direction]
        cleaned = f"{cashtag} {label}。{cleaned.replace(cashtag, '', 1).lstrip()}"
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip("，,。；; ") + "。"
    if not cleaned:
        raise ValueError("AI returned empty post text")
    return cleaned


class AIWriter:
    def __init__(self, config: AIConfig, tone: ToneConfig, max_chars: int):
        self.config = config
        self.tone = tone
        self.max_chars = max_chars

    async def generate(self, signal: Mapping[str, Any], avoid_texts: tuple[str, ...] = ()) -> str:
        messages = build_messages(signal, self.tone, self.max_chars)
        if avoid_texts:
            excerpts = "\n---\n".join(text[:600] for text in avoid_texts[-5:])
            messages[0]["content"] += (
                "\n- 这是同一信号的其他账号文案。必须换开头、句式和论述顺序，禁止照抄：\n"
                + excerpts
            )
        payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            response = await client.post(_chat_url(self.config.base_url), headers=headers, json=payload)
            response.raise_for_status()
            result = response.json()
        try:
            text = result["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("AI response did not contain choices[0].message.content") from exc
        return clean_generated_text(
            str(text),
            symbol=str(signal["symbol"]),
            direction=str(signal["direction"]),
            max_chars=self.max_chars,
        )


class SquareClient:
    def __init__(self, api_key: str, timeout_seconds: float = 30.0):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    async def publish(self, text: str) -> PublishResult:
        headers = {
            "X-Square-OpenAPI-Key": self.api_key,
            "Content-Type": "application/json",
            "clienttype": "alphapumphunter",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                SQUARE_POST_URL,
                headers=headers,
                json={"contentType": 1, "bodyTextOnly": text},
            )
        if response.status_code == 504:
            return PublishResult(None, None, {"publishStatus": "success_without_post_id"})
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "000000":
            raise RuntimeError(f"Square API [{payload.get('code')}]: {payload.get('message')}")
        data = payload.get("data") or {}
        return PublishResult(
            str(data["id"]) if data.get("id") is not None else None,
            data.get("shareLink")
            or (
                f"https://www.binance.com/square/post/{data['id']}"
                if data.get("id") is not None
                else None
            ),
            data,
        )

    async def validate_key(self) -> None:
        """Validate authentication by submitting an intentionally rejected empty body."""
        headers = {
            "X-Square-OpenAPI-Key": self.api_key,
            "Content-Type": "application/json",
            "clienttype": "alphapumphunter",
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                SQUARE_POST_URL,
                headers=headers,
                json={"contentType": 1, "bodyTextOnly": ""},
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Square key validation returned HTTP {response.status_code} with non-JSON body"
            ) from exc
        code = str(payload.get("code", ""))
        if code in {"20020", "220011"}:
            return
        raise RuntimeError(f"Square key validation failed [{code}]: {payload.get('message')}")


class SignalStore:
    def __init__(self, path: str | Path):
        self.path = path
        with connect(self.path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL REFERENCES signals(id),
                    account_id TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt_at REAL NOT NULL DEFAULT 0,
                    claimed_at REAL,
                    generated_text TEXT,
                    published_at REAL,
                    post_id TEXT,
                    post_url TEXT,
                    error TEXT,
                    UNIQUE(signal_id, account_id)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deliveries_pending "
                "ON deliveries(status, next_attempt_at, created_at)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_deliveries_account_published "
                "ON deliveries(account_id, published_at)"
            )

    def sync_deliveries(
        self, accounts: tuple[AccountConfig, ...], max_signal_age_seconds: int
    ) -> None:
        now = time.time()
        with connect(self.path) as connection:
            signal_rows = connection.execute(
                "SELECT id, created_at FROM signals WHERE created_at >= ?",
                (now - max_signal_age_seconds,),
            ).fetchall()
            enabled_accounts = tuple(account for account in accounts if account.enabled)
            enabled_ids = tuple(account.account_id for account in enabled_accounts)
            if enabled_ids:
                placeholders = ",".join("?" for _ in enabled_ids)
                connection.execute(
                    f"UPDATE deliveries SET status='cancelled', claimed_at=NULL "
                    f"WHERE status IN ('pending','processing') AND account_id NOT IN ({placeholders})",
                    enabled_ids,
                )
            for signal in signal_rows:
                for account in enabled_accounts:
                    delay = random.uniform(account.min_delay_seconds, account.max_delay_seconds)
                    connection.execute(
                        "INSERT INTO deliveries "
                        "(signal_id, account_id, created_at, next_attempt_at) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(signal_id, account_id) DO UPDATE SET "
                        "status='pending', next_attempt_at=excluded.next_attempt_at "
                        "WHERE deliveries.status='cancelled'",
                        (
                            signal["id"],
                            account.account_id,
                            now,
                            max(now, float(signal["created_at"]) + delay),
                        ),
                    )

    def claim_next(self, stale_seconds: int = 300) -> Optional[dict[str, Any]]:
        now = time.time()
        with connect(self.path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE deliveries SET status='pending', claimed_at=NULL "
                "WHERE status='processing' AND claimed_at < ?",
                (now - stale_seconds,),
            )
            row = connection.execute(
                "SELECT d.id AS delivery_id, d.signal_id, d.account_id, d.attempts, "
                "s.created_at AS signal_created_at, s.symbol, s.source, s.indicator, "
                "s.direction, s.price, s.summary, s.details_json, s.fingerprint "
                "FROM deliveries d JOIN signals s ON s.id=d.signal_id "
                "WHERE d.status='pending' AND d.next_attempt_at <= ? "
                "ORDER BY d.next_attempt_at, d.id LIMIT 1",
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "UPDATE deliveries SET status='processing', claimed_at=? WHERE id=?",
                (now, row["delivery_id"]),
            )
            connection.commit()
            return dict(row)

    def cooldown_match(self, delivery: Mapping[str, Any], cooldown_seconds: int) -> Optional[sqlite3.Row]:
        with connect(self.path) as connection:
            return connection.execute(
                "SELECT d.id, d.published_at FROM deliveries d "
                "JOIN signals s ON s.id=d.signal_id "
                "WHERE d.id != ? AND d.account_id=? AND s.symbol=? AND s.indicator=? "
                "AND d.status='published' AND d.published_at >= ? "
                "ORDER BY d.published_at DESC LIMIT 1",
                (
                    delivery["delivery_id"],
                    delivery["account_id"],
                    delivery["symbol"],
                    delivery["indicator"],
                    time.time() - cooldown_seconds,
                ),
            ).fetchone()

    def generated_by_other_accounts(self, signal_id: int, account_id: str) -> tuple[str, ...]:
        with connect(self.path) as connection:
            rows = connection.execute(
                "SELECT generated_text FROM deliveries "
                "WHERE signal_id=? AND account_id != ? AND generated_text IS NOT NULL",
                (signal_id, account_id),
            ).fetchall()
        return tuple(str(row["generated_text"]) for row in rows)

    def mark_suppressed(self, delivery_id: int, prior_id: int) -> None:
        with connect(self.path) as connection:
            connection.execute(
                "UPDATE deliveries SET status='suppressed', error=?, claimed_at=NULL WHERE id=?",
                (f"cooldown: same account, symbol, and indicator as delivery {prior_id}", delivery_id),
            )

    def mark_published(self, delivery_id: int, text: str, result: PublishResult, *, dry_run: bool) -> None:
        with connect(self.path) as connection:
            connection.execute(
                "UPDATE deliveries SET status=?, generated_text=?, published_at=?, post_id=?, "
                "post_url=?, error=NULL, claimed_at=NULL WHERE id=?",
                (
                    "dry_run" if dry_run else "published",
                    text,
                    time.time(),
                    result.post_id,
                    result.post_url,
                    delivery_id,
                ),
            )

    def mark_failed(self, delivery: Mapping[str, Any], error: Exception, config: PublisherConfig) -> None:
        attempts = int(delivery["attempts"]) + 1
        terminal = attempts >= config.max_attempts
        delay = min(config.retry_base_seconds * (2 ** max(0, attempts - 1)), 3600)
        with connect(self.path) as connection:
            connection.execute(
                "UPDATE deliveries SET status=?, attempts=?, next_attempt_at=?, error=?, claimed_at=NULL WHERE id=?",
                (
                    "failed" if terminal else "pending",
                    attempts,
                    time.time() + delay,
                    str(error)[:1000],
                    delivery["delivery_id"],
                ),
            )


class KOLPublisher:
    def __init__(self, config: AppConfig):
        self.config = config
        self.store = SignalStore(config.publisher.database_path)
        self.accounts = {account.account_id: account for account in config.accounts if account.enabled}

    async def notify_feishu(
        self,
        account: AccountConfig,
        delivery: Mapping[str, Any],
        text: str,
        result: PublishResult,
    ) -> None:
        publisher = self.config.publisher
        if not publisher.feishu_enabled or not publisher.feishu_webhook:
            return
        lines = [
            "币安广场 KOL 已发布",
            f"账号：{account.account_id}",
            f"币种：${delivery['symbol']}",
            f"方向：{delivery['direction']}",
            f"指标：{delivery['indicator']}",
            f"帖子：{result.post_url or '发布成功，但接口未返回链接'}",
            "",
            text,
        ]
        payload = {"msg_type": "text", "content": {"text": "\n".join(lines)}}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(publisher.feishu_webhook, json=payload)
            response.raise_for_status()

    async def process_one(self) -> bool:
        self.store.sync_deliveries(
            self.config.accounts, self.config.publisher.max_signal_age_seconds
        )
        delivery = self.store.claim_next()
        if delivery is None:
            return False
        account = self.accounts.get(str(delivery["account_id"]))
        if account is None:
            self.store.mark_failed(delivery, RuntimeError("account is disabled or missing"), self.config.publisher)
            return True
        prior = self.store.cooldown_match(delivery, account.cooldown_seconds)
        if prior is not None:
            self.store.mark_suppressed(int(delivery["delivery_id"]), int(prior["id"]))
            logger.info(
                "Suppressed delivery {} for account {}: {} remains in cooldown for {}",
                delivery["delivery_id"], account.account_id, delivery["symbol"], delivery["indicator"],
            )
            return True
        try:
            ai_config = AIConfig(
                base_url=self.config.ai.base_url,
                api_key=self.config.ai.api_key,
                model=account.model or self.config.ai.model,
                temperature=(
                    account.temperature
                    if account.temperature is not None
                    else self.config.ai.temperature
                ),
                timeout_seconds=self.config.ai.timeout_seconds,
            )
            writer = AIWriter(ai_config, account.tone, self.config.publisher.max_post_chars)
            other_texts = self.store.generated_by_other_accounts(
                int(delivery["signal_id"]), account.account_id
            )
            text = await writer.generate(delivery, other_texts)
            if text in other_texts:
                raise RuntimeError("AI generated an exact duplicate of another account's post")
            if self.config.publisher.dry_run:
                result = PublishResult(None, None, {"dry_run": True})
                logger.info(
                    "DRY RUN delivery {} account {}:\n{}",
                    delivery["delivery_id"], account.account_id, text,
                )
            else:
                result = await SquareClient(account.square_api_key).publish(text)
                logger.info(
                    "Published delivery {} for account {}: {}",
                    delivery["delivery_id"], account.account_id, result.post_url or "post id unavailable",
                )
            self.store.mark_published(
                int(delivery["delivery_id"]), text, result, dry_run=self.config.publisher.dry_run
            )
            if not self.config.publisher.dry_run:
                try:
                    await self.notify_feishu(account, delivery, text, result)
                    logger.info("Feishu notified for delivery {}", delivery["delivery_id"])
                except Exception:
                    logger.exception(
                        "Square post succeeded, but Feishu notification failed for delivery {}",
                        delivery["delivery_id"],
                    )
        except Exception as exc:
            self.store.mark_failed(delivery, exc, self.config.publisher)
            logger.exception(
                "Unable to process delivery {} for account {}",
                delivery["delivery_id"], account.account_id,
            )
        return True

    async def run(self, *, once: bool = False) -> None:
        while True:
            processed = await self.process_one()
            if once:
                return
            if not processed:
                await asyncio.sleep(self.config.publisher.poll_interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI KOL publisher for Binance Square")
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Local JSON config path")
    parser.add_argument("--dry-run", action="store_true", help="Generate copy without calling Square")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Continuously consume monitor signals")
    run.add_argument("--once", action="store_true", help="Process at most one pending signal")
    test = sub.add_parser("test-signal", help="Queue a synthetic signal for end-to-end validation")
    test.add_argument("symbol")
    test.add_argument("--direction", choices=["LONG", "SHORT", "WATCH"], default="LONG")
    sub.add_parser("validate-accounts", help="Validate all enabled Square keys without posting")
    return parser


async def async_main() -> None:
    args = build_parser().parse_args()
    config = load_config(args.config)
    if args.dry_run:
        config = AppConfig(
            ai=config.ai,
            accounts=config.accounts,
            publisher=PublisherConfig(**{**config.publisher.__dict__, "dry_run": True}),
        )
    validate_config(config)
    if args.command == "validate-accounts":
        for account in config.accounts:
            if not account.enabled:
                continue
            await SquareClient(account.square_api_key).validate_key()
            logger.info("Square key is valid for account {}", account.account_id)
        return
    if args.command == "test-signal":
        signal_id = emit_signal(
            symbol=args.symbol,
            source="manual_test",
            indicator=f"manual_{args.direction.lower()}",
            direction=args.direction,
            summary="人工端到端测试信号，不代表真实行情机会",
            details={"test": True},
            fingerprint=f"manual:{time.time_ns()}",
            db_path=config.publisher.database_path,
        )
        logger.info("Queued test signal {}", signal_id)
        return
    await KOLPublisher(config).run(once=args.once)


if __name__ == "__main__":
    logger.add("logkol_publisher.py.log", rotation="1 MB", retention="7 days", level="INFO")
    asyncio.run(async_main())
