"""Generate signal-driven KOL copy and publish to Binance Square and OKX Orbit."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Optional

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
OKX_PUBLISH_URL = "https://www.okx.com/priapi/v5/content/ugc/publish"
OKX_INSTRUMENTS_URL = "https://www.okx.com/api/v5/public/instruments"
BINANCE_FAPI_KLINES_URL = "https://www.binance.com/fapi/v1/klines"
VALID_PLATFORMS = frozenset({"binance", "okx"})
PlatformName = Literal["binance", "okx"]


@dataclass(frozen=True)
class AIConfig:
    base_url: str
    api_key: str
    model: str
    temperature: float = 0.9
    timeout_seconds: float = 45.0
    concise: bool = False
    max_chars: int = 1200


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
    tone: ToneConfig
    platform: PlatformName = "binance"
    square_api_key: str = ""
    okx_authorization: str = ""
    okx_devid: str = ""
    okx_group: str = "USDT"
    enabled: bool = True
    model: Optional[str] = None
    temperature: Optional[float] = None
    concise: Optional[bool] = None
    max_chars: Optional[int] = None
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
    daily_post_limit_pause_seconds: int = 86400
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


class SquareDailyPostLimitError(RuntimeError):
    """Binance Square OpenAPI has reached its daily post allowance."""

    code = "220009"


class OkxAuthorizationError(RuntimeError):
    """OKX Orbit authorization token is invalid or expired."""


_OKX_AUTH_MESSAGE_HINTS = (
    "unauthorized",
    "unauthoriz",
    "token expired",
    "jwt expired",
    "login expired",
    "not logged",
    "not login",
    "please login",
    "authorization",
    "invalid token",
    "token invalid",
    "credential",
    "请登录",
    "登录过期",
    "未登录",
    "token失效",
    "token无效",
    "授权",
    "过期",
)


def _okx_error_message(body: Mapping[str, Any], *, status_code: Optional[int] = None) -> str:
    message = str(body.get("msg") or body.get("message") or body.get("error") or "").strip()
    code = str(body.get("code", "")).strip()
    if message and code:
        return f"[{code}] {message}"
    if message:
        return message
    if code:
        return f"[{code}]"
    if status_code is not None:
        return f"HTTP {status_code}"
    return "unknown OKX error"


def _okx_response_is_auth_error(status_code: int, body: Mapping[str, Any]) -> bool:
    if status_code in {401, 403}:
        return True
    haystack = " ".join(
        str(body.get(key) or "")
        for key in ("code", "msg", "message", "error", "data")
    ).lower()
    return any(hint in haystack for hint in _OKX_AUTH_MESSAGE_HINTS)


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


def _okx_env_name(account_id: str, field: str) -> str:
    suffix = re.sub(r"[^A-Z0-9]", "_", account_id.upper())
    return f"OKX_{field.upper()}_{suffix}"


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
        raise RuntimeError("accounts must contain at least one publishing account")
    accounts: list[AccountConfig] = []
    seen_ids: set[str] = set()
    for index, row in enumerate(account_rows):
        account_id = str(row.get("id") or "").strip()
        if not account_id or not re.fullmatch(r"[A-Za-z0-9_-]+", account_id):
            raise RuntimeError(f"accounts[{index}].id must use letters, numbers, _ or -")
        if account_id in seen_ids:
            raise RuntimeError(f"Duplicate account id: {account_id}")
        seen_ids.add(account_id)
        platform = str(row.get("platform") or "binance").strip().lower()
        if platform not in VALID_PLATFORMS:
            raise RuntimeError(
                f"accounts[{index}].platform must be one of: {', '.join(sorted(VALID_PLATFORMS))}"
            )
        delay = row.get("posting_delay_seconds") or [0, 0]
        if not isinstance(delay, list) or len(delay) != 2:
            raise RuntimeError(f"accounts[{index}].posting_delay_seconds must be [min, max]")
        account_ai = row.get("ai") or {}
        square_key = _secret(row.get("square_api_key"), _account_env_name(account_id))
        okx_authorization = _secret(
            row.get("authorization") or row.get("okx_authorization"),
            _okx_env_name(account_id, "authorization"),
        )
        okx_devid = _secret(
            row.get("devid") or row.get("okx_devid"),
            _okx_env_name(account_id, "devid"),
        )
        accounts.append(
            AccountConfig(
                account_id=account_id,
                platform=platform,  # type: ignore[arg-type]
                square_api_key=square_key,
                okx_authorization=okx_authorization,
                okx_devid=okx_devid,
                okx_group=str(row.get("okx_group") or row.get("group") or "USDT").strip() or "USDT",
                tone=_tone(row.get("tone") or {}, default_tone),
                enabled=bool(row.get("enabled", True)),
                model=str(account_ai.get("model") or "").strip() or None,
                temperature=(
                    float(account_ai["temperature"])
                    if account_ai.get("temperature") is not None
                    else None
                ),
                concise=(
                    bool(account_ai["concise"])
                    if account_ai.get("concise") is not None
                    else None
                ),
                max_chars=(
                    int(account_ai["max_chars"])
                    if account_ai.get("max_chars") is not None
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
            concise=bool(ai_data.get("concise", False)),
            max_chars=int(ai_data.get("max_chars", 1200)),
        ),
        accounts=tuple(accounts),
        publisher=PublisherConfig(
            poll_interval_seconds=float(publisher_data.get("poll_interval_seconds", 10)),
            max_post_chars=int(publisher_data.get("max_post_chars", 1200)),
            retry_base_seconds=int(publisher_data.get("retry_base_seconds", 30)),
            max_attempts=int(publisher_data.get("max_attempts", 8)),
            max_signal_age_seconds=int(publisher_data.get("max_signal_age_seconds", 3600)),
            daily_post_limit_pause_seconds=int(
                publisher_data.get("daily_post_limit_pause_seconds", 86400)
            ),
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
    if not 50 <= config.ai.max_chars <= 2000:
        raise RuntimeError("ai.max_chars must be between 50 and 2000")
    enabled = [account for account in config.accounts if account.enabled]
    if not enabled:
        raise RuntimeError("At least one account must be enabled")
    for account in enabled:
        if require_square_key and not config.publisher.dry_run:
            if account.platform == "binance" and not account.square_api_key:
                raise RuntimeError(
                    f"Missing Square key for account {account.account_id}; set "
                    f"{_account_env_name(account.account_id)} or accounts[].square_api_key"
                )
            if account.platform == "okx" and (
                not account.okx_authorization or not account.okx_devid
            ):
                raise RuntimeError(
                    f"Missing OKX credentials for account {account.account_id}; set "
                    f"{_okx_env_name(account.account_id, 'authorization')} / "
                    f"{_okx_env_name(account.account_id, 'devid')} or "
                    f"accounts[].authorization / accounts[].devid"
                )
        if account.cooldown_seconds < 0:
            raise RuntimeError(f"Account {account.account_id} cooldown must be non-negative")
        if account.min_delay_seconds < 0 or account.max_delay_seconds < account.min_delay_seconds:
            raise RuntimeError(f"Account {account.account_id} has an invalid posting delay range")
        if account.max_chars is not None and not 50 <= account.max_chars <= 2000:
            raise RuntimeError(
                f"Account {account.account_id} ai.max_chars must be between 50 and 2000"
            )
    if not 100 <= config.publisher.max_post_chars <= 2000:
        raise RuntimeError("publisher.max_post_chars must be between 100 and 2000")
    if config.publisher.daily_post_limit_pause_seconds < 300:
        raise RuntimeError("publisher.daily_post_limit_pause_seconds must be at least 300")
    if config.publisher.feishu_enabled and not config.publisher.feishu_webhook.startswith(
        "https://open.feishu.cn/open-apis/bot/"
    ):
        raise RuntimeError("publisher.feishu_webhook must be a Feishu bot webhook URL")


def _coerce_assistant_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(
                    item.get("text")
                    or item.get("content")
                    or item.get("output_text")
                    or ""
                ).strip()
            else:
                text = ""
            if text:
                parts.append(text)
        return "\n".join(parts).strip()
    return ""


def _message_text_from_choice(choice: Mapping[str, Any]) -> str:
    message = choice.get("message")
    if isinstance(message, dict):
        text = _coerce_assistant_text(message.get("content"))
        if text:
            return text
    for key in ("text", "content", "output_text"):
        text = _coerce_assistant_text(choice.get(key))
        if text:
            return text
    return ""


def _chat_completion_roots(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    roots: list[Mapping[str, Any]] = [payload]
    seen: set[int] = {id(payload)}
    queue: list[Mapping[str, Any]] = [payload]
    wrapper_keys = ("data", "result", "response", "output")
    while queue:
        current = queue.pop(0)
        for key in wrapper_keys:
            nested = current.get(key)
            if not isinstance(nested, dict):
                continue
            nested_id = id(nested)
            if nested_id in seen:
                continue
            seen.add(nested_id)
            roots.append(nested)
            queue.append(nested)
    return tuple(roots)


def _raise_chat_completion_error(payload: Mapping[str, Any]) -> None:
    if payload.get("success") is False:
        message = (
            payload.get("msg")
            or payload.get("message")
            or payload.get("error")
            or "gateway returned success=false"
        )
        raise RuntimeError(f"AI gateway error: {message}")
    error = payload.get("error")
    if isinstance(error, dict):
        message = error.get("message") or error.get("msg") or error
        raise RuntimeError(f"AI API error: {message}")
    if isinstance(error, str) and error.strip():
        raise RuntimeError(f"AI API error: {error}")


def extract_assistant_text_from_chat_response(payload: Any) -> str:
    """Extract post text from OpenAI-compatible or gateway-wrapped chat responses."""

    if not isinstance(payload, dict):
        raise KeyError("payload")
    _raise_chat_completion_error(payload)

    for key in ("output_text", "text", "content"):
        text = _coerce_assistant_text(payload.get(key))
        if text:
            return text

    for root in _chat_completion_roots(payload):
        choices = root.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            text = _message_text_from_choice(choice)
            if text:
                return text

    raise KeyError("content")


def _normalize_chat_completion_response(payload: Any) -> Mapping[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("AI response was not a JSON object")
    return payload


def _extract_assistant_text(result: Mapping[str, Any]) -> str:
    return extract_assistant_text_from_chat_response(result)


def _chat_url(base_url: str) -> str:
    return base_url + "/chat/completions" if base_url.rstrip("/").endswith("/v1") else base_url + "/v1/chat/completions"


def build_messages(
    signal: Mapping[str, Any],
    tone: ToneConfig,
    max_chars: int,
    *,
    concise: bool = False,
    platform: PlatformName = "binance",
) -> list[dict[str, str]]:
    symbol = normalize_symbol(str(signal["symbol"]))
    details = json.loads(signal.get("details_json") or "{}")
    direction_text = {"LONG": "看多/做多", "SHORT": "看空/做空", "WATCH": "观察等待"}[
        str(signal["direction"])
    ]
    requirements = "\n".join(f"- {item}" for item in tone.requirements)
    concise_rule = (
        "- 使用简短回复：直接给结论，只保留 2-3 个最关键依据，不复述全部输入数据。"
        if concise
        else "- 在字数限制内给出必要的行情依据，避免机械罗列全部输入数据。"
    )
    if platform == "okx":
        platform_rules = f"""- 这是 OKX 星球（Orbit）帖子，不是币安广场；措辞、开头、论证顺序必须与币安广场版本明显不同。
- 自然写出币种名 {symbol}，不要写 $ 前缀 cashtag，不要写任何 URL。
- 若提供了其他平台已发版本，必须彻底改写，禁止复用相同句式或段落结构。"""
    else:
        platform_rules = f"""- 必须原样包含可点击 cashtag ${symbol}，不得写任何 URL。"""
    system = f"""你是{"OKX 星球" if platform == "okx" else "币安广场"}的交易信号 KOL，当前口吻配置为“{tone.name}”。
人设：{tone.persona}

硬性规则：
- 只输出最终帖子正文，不要标题标签、解释、Markdown 代码块。
- 全文使用简体中文，控制在 {max_chars} 个字符以内。
{concise_rule}
{platform_rules}
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
            "近2小时15分钟K线": signal.get("kline_context") or [],
            "K线说明": "按时间从旧到新排列；最新一根可能尚未收盘。",
        },
        ensure_ascii=False,
        indent=2,
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def clean_generated_text(
    text: str,
    *,
    symbol: str,
    direction: str,
    max_chars: int,
    platform: PlatformName = "binance",
) -> str:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:text|markdown)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = re.sub(r"https?://\S+", "", cleaned).strip()
    normalized = normalize_symbol(symbol)
    direction_terms = ("做多", "看多") if direction == "LONG" else (("做空", "看空") if direction == "SHORT" else ("观察", "等待"))
    if platform == "binance":
        cashtag = f"${normalized}"
        cashtag_pattern = rf"\${re.escape(normalized)}(?![A-Z0-9])"
        if not re.search(cashtag_pattern, cleaned, flags=re.IGNORECASE):
            cleaned = f"{cashtag} {cleaned}"
        if not any(term in cleaned for term in direction_terms):
            label = {"LONG": "看多", "SHORT": "看空", "WATCH": "观察"}[direction]
            cleaned = f"{cashtag} {label}。{cleaned.replace(cashtag, '', 1).lstrip()}"
    else:
        if normalized not in cleaned.upper().replace("$", ""):
            label = {"LONG": "看多", "SHORT": "看空", "WATCH": "观察"}[direction]
            cleaned = f"{normalized} {label}。{cleaned}"
        elif not any(term in cleaned for term in direction_terms):
            label = {"LONG": "看多", "SHORT": "看空", "WATCH": "观察"}[direction]
            cleaned = f"{label}。{cleaned}"
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip("，,。；; ") + "。"
    if not cleaned:
        raise ValueError("AI returned empty post text")
    return cleaned


def parse_recent_15m_klines(
    rows: Any, *, now_ms: Optional[int] = None
) -> tuple[dict[str, Any], ...]:
    if not isinstance(rows, list) or not rows:
        raise ValueError("Binance returned no 15m kline data")
    current_ms = int(time.time() * 1000) if now_ms is None else now_ms
    result: list[dict[str, Any]] = []
    for row in rows[-8:]:
        if not isinstance(row, list) or len(row) < 11:
            raise ValueError("Binance returned malformed 15m kline data")
        open_time_ms = int(row[0])
        result.append(
            {
                "open_time_utc": datetime.fromtimestamp(
                    open_time_ms / 1000, timezone.utc
                ).isoformat(timespec="minutes"),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "quote_volume": float(row[7]),
                "trade_count": int(row[8]),
                "taker_buy_quote_volume": float(row[10]),
                "closed": int(row[6]) < current_ms,
            }
        )
    return tuple(result)


class MarketContextClient:
    def __init__(self, timeout_seconds: float = 15.0, cache_size: int = 1000):
        self.timeout_seconds = timeout_seconds
        self.cache_size = cache_size
        self._cache: dict[int, tuple[dict[str, Any], ...]] = {}

    async def get(
        self, signal_id: int, symbol: str
    ) -> tuple[dict[str, Any], ...]:
        cached = self._cache.get(signal_id)
        if cached is not None:
            return cached
        params = {
            "symbol": f"{normalize_symbol(symbol)}USDT",
            "interval": "15m",
            "limit": 8,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(BINANCE_FAPI_KLINES_URL, params=params)
            response.raise_for_status()
            context = parse_recent_15m_klines(response.json())
        if len(self._cache) >= self.cache_size:
            self._cache.pop(next(iter(self._cache)))
        self._cache[signal_id] = context
        return context


class OkxSwapCatalog:
    """Cache live OKX USDT-margined perpetual contracts for pre-publish filtering."""

    def __init__(
        self,
        timeout_seconds: float = 15.0,
        refresh_seconds: float = 3600.0,
    ):
        self.timeout_seconds = timeout_seconds
        self.refresh_seconds = refresh_seconds
        self._live_usdt_swaps: set[str] = set()
        self._loaded_at = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def inst_id(symbol: str) -> str:
        return f"{normalize_symbol(symbol)}-USDT-SWAP"

    async def refresh(self) -> None:
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.get(
                OKX_INSTRUMENTS_URL, params={"instType": "SWAP"}
            )
            response.raise_for_status()
            payload = response.json()
        if str(payload.get("code")) != "0":
            message = payload.get("msg") or payload.get("message") or payload
            raise RuntimeError(f"OKX instruments API [{payload.get('code')}]: {message}")
        live = {
            str(item["instId"])
            for item in (payload.get("data") or [])
            if isinstance(item, dict)
            and str(item.get("instId", "")).endswith("-USDT-SWAP")
            and str(item.get("state", "")).lower() == "live"
        }
        self._live_usdt_swaps = live
        self._loaded_at = time.time()
        logger.info("Loaded {} live OKX USDT-SWAP instruments", len(live))

    async def has_usdt_swap(self, symbol: str) -> bool:
        async with self._lock:
            if not self._live_usdt_swaps or (
                time.time() - self._loaded_at >= self.refresh_seconds
            ):
                await self.refresh()
            return self.inst_id(symbol) in self._live_usdt_swaps


class AIWriter:
    def __init__(
        self,
        config: AIConfig,
        tone: ToneConfig,
        max_chars: int,
        *,
        platform: PlatformName = "binance",
    ):
        self.config = config
        self.tone = tone
        self.max_chars = min(max_chars, config.max_chars)
        self.platform = platform

    async def generate(self, signal: Mapping[str, Any], avoid_texts: tuple[str, ...] = ()) -> str:
        messages = build_messages(
            signal,
            self.tone,
            self.max_chars,
            concise=self.config.concise,
            platform=self.platform,
        )
        if avoid_texts:
            excerpts = "\n---\n".join(text[:600] for text in avoid_texts[-5:])
            label = (
                "其他平台或其他账号已发版本"
                if self.platform == "okx"
                else "同一信号的其他账号文案"
            )
            messages[0]["content"] += (
                f"\n- 这是{label}。必须换开头、句式和论述顺序，禁止照抄：\n"
                + excerpts
            )
        request_payload = {
            "model": self.config.model,
            "messages": messages,
            "temperature": self.config.temperature,
        }
        headers = {"Authorization": f"Bearer {self.config.api_key}"}
        last_error: Optional[Exception] = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
                    response = await client.post(
                        _chat_url(self.config.base_url),
                        headers=headers,
                        json=request_payload,
                    )
                    response.raise_for_status()
                    response_body = response.json()
                if not isinstance(response_body, dict):
                    raise KeyError("payload")
                _raise_chat_completion_error(response_body)
                text = extract_assistant_text_from_chat_response(response_body)
            except RuntimeError as exc:
                if not str(exc).startswith("AI "):
                    raise
                last_error = exc
                logger.warning(
                    "AI gateway returned error on attempt {} model={}: {}",
                    attempt + 1,
                    self.config.model,
                    exc,
                )
                continue
            except KeyError as exc:
                last_error = RuntimeError(
                    "AI response did not contain usable assistant text"
                )
                last_error.__cause__ = exc
                logger.warning(
                    "AI returned malformed payload on attempt {} model={}: {}",
                    attempt + 1,
                    self.config.model,
                    response_body if "response_body" in locals() else None,
                )
                continue
            return clean_generated_text(
                text,
                symbol=str(signal["symbol"]),
                direction=str(signal["direction"]),
                max_chars=self.max_chars,
                platform=self.platform,
            )
        raise last_error or RuntimeError("AI returned empty post text")


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
        if str(payload.get("code")) == SquareDailyPostLimitError.code:
            raise SquareDailyPostLimitError(
                f"Square API [{payload.get('code')}]: {payload.get('message')}"
            )
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


class OkxClient:
    def __init__(
        self,
        authorization: str,
        devid: str,
        *,
        group: str = "USDT",
        timeout_seconds: float = 30.0,
    ):
        self.authorization = authorization.strip()
        self.devid = devid.strip()
        self.group = group.strip() or "USDT"
        self.timeout_seconds = timeout_seconds

    def _headers(self) -> dict[str, str]:
        return {
            "accept": "application/json",
            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
            "app-type": "web",
            "authorization": self.authorization,
            "content-type": "application/json",
            "devid": self.devid,
            "origin": "https://www.okx.com",
            "platform": "web",
            "referer": "https://www.okx.com/zh-hans/orbit",
            "user-agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
            ),
            "x-cdn": "https://www.okx.com",
            "x-utc": "8",
            "x-zkdex-env": "0",
        }

    async def publish(self, text: str) -> PublishResult:
        publish_id = str(uuid.uuid4())
        params = {"t": str(int(time.time() * 1000))}
        payload = {
            "content": text,
            "group": self.group,
            "publishId": publish_id,
        }
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                OKX_PUBLISH_URL,
                params=params,
                headers=self._headers(),
                json=payload,
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        if _okx_response_is_auth_error(response.status_code, body):
            raise OkxAuthorizationError(
                "OKX authorization invalid: "
                + _okx_error_message(body, status_code=response.status_code)
            )
        response.raise_for_status()
        code = str(body.get("code", ""))
        if code not in {"0", "00000"}:
            message = _okx_error_message(body)
            if _okx_response_is_auth_error(response.status_code, body):
                raise OkxAuthorizationError(f"OKX authorization invalid: {message}")
            raise RuntimeError(f"OKX Orbit API {message}")
        data = body.get("data") or {}
        post_id = (
            str(data["id"])
            if data.get("id") is not None
            else str(data.get("contentId") or publish_id)
        )
        return PublishResult(
            post_id,
            data.get("shareLink") or data.get("url"),
            {"publishId": publish_id, **body},
        )

    def validate_credentials(self) -> None:
        if not self.authorization or not self.devid:
            raise RuntimeError("OKX authorization and devid must not be empty")


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS account_publish_blocks (
                    account_id TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    blocked_at REAL NOT NULL,
                    blocked_until REAL NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_account_publish_blocks_until "
                "ON account_publish_blocks(blocked_until)"
            )

    @staticmethod
    def _blocked_account_ids(connection: sqlite3.Connection, current: float) -> set[str]:
        connection.execute(
            "DELETE FROM account_publish_blocks WHERE blocked_until <= ?", (current,)
        )
        rows = connection.execute(
            "SELECT account_id FROM account_publish_blocks WHERE blocked_until > ?",
            (current,),
        ).fetchall()
        return {str(row["account_id"]) for row in rows}

    def blocked_account_ids(self, now: Optional[float] = None) -> set[str]:
        current = time.time() if now is None else now
        with connect(self.path) as connection:
            return self._blocked_account_ids(connection, current)

    def block_account_for_daily_post_limit(
        self, account_id: str, pause_seconds: int, error: Exception
    ) -> tuple[float, int]:
        now = time.time()
        blocked_until = now + pause_seconds
        reason = str(error)[:1000]
        with connect(self.path) as connection:
            connection.execute(
                """
                INSERT INTO account_publish_blocks (
                    account_id, reason, blocked_at, blocked_until
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    reason=excluded.reason,
                    blocked_at=excluded.blocked_at,
                    blocked_until=excluded.blocked_until
                """,
                (account_id, reason, now, blocked_until),
            )
            cursor = connection.execute(
                "UPDATE deliveries SET status='suppressed', error=?, claimed_at=NULL "
                "WHERE account_id=? AND status IN ('pending','processing')",
                (f"daily post limit: {reason}", account_id),
            )
        return blocked_until, cursor.rowcount

    def discard_pending(self, account_id: Optional[str] = None) -> int:
        reason = "manual backlog discard"
        with connect(self.path) as connection:
            if account_id:
                cursor = connection.execute(
                    "UPDATE deliveries SET status='suppressed', error=?, claimed_at=NULL "
                    "WHERE account_id=? AND status IN ('pending','processing')",
                    (reason, account_id),
                )
            else:
                cursor = connection.execute(
                    "UPDATE deliveries SET status='suppressed', error=?, claimed_at=NULL "
                    "WHERE status IN ('pending','processing')",
                    (reason,),
                )
        return cursor.rowcount

    def sync_deliveries(
        self, accounts: tuple[AccountConfig, ...], max_signal_age_seconds: int
    ) -> None:
        now = time.time()
        with connect(self.path) as connection:
            signal_rows = connection.execute(
                "SELECT id, created_at FROM signals WHERE created_at >= ?",
                (now - max_signal_age_seconds,),
            ).fetchall()
            configured_accounts = tuple(account for account in accounts if account.enabled)
            configured_ids = tuple(account.account_id for account in configured_accounts)
            if configured_ids:
                placeholders = ",".join("?" for _ in configured_ids)
                connection.execute(
                    f"UPDATE deliveries SET status='cancelled', claimed_at=NULL "
                    f"WHERE status IN ('pending','processing') AND account_id NOT IN ({placeholders})",
                    configured_ids,
                )
            blocked_ids = self._blocked_account_ids(connection, now)
            enabled_accounts = tuple(
                account for account in configured_accounts if account.account_id not in blocked_ids
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
        self.suppress_delivery(
            delivery_id,
            f"cooldown: same account, symbol, and indicator as delivery {prior_id}",
        )

    def suppress_delivery(self, delivery_id: int, reason: str) -> None:
        with connect(self.path) as connection:
            connection.execute(
                "UPDATE deliveries SET status='suppressed', error=?, claimed_at=NULL WHERE id=?",
                (reason[:1000], delivery_id),
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
        self.market_context = MarketContextClient()
        self.okx_swap_catalog = OkxSwapCatalog()

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
        platform_label = "OKX 星球" if account.platform == "okx" else "币安广场"
        lines = [
            f"{platform_label} KOL 已发布",
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

    async def notify_daily_post_limit(
        self, account: AccountConfig, error: SquareDailyPostLimitError, blocked_until: float
    ) -> None:
        publisher = self.config.publisher
        if not publisher.feishu_enabled or not publisher.feishu_webhook:
            return
        resume_at = datetime.fromtimestamp(blocked_until, timezone.utc).isoformat(
            timespec="minutes"
        )
        payload = {
            "msg_type": "text",
            "content": {
                "text": "\n".join(
                    (
                        "币安广场 KOL 发布已暂停",
                        f"账号：{account.account_id}",
                        "原因：Binance Square OpenAPI 达到每日发帖限制（220009）",
                        f"恢复时间（UTC）：{resume_at}",
                        f"接口信息：{error}",
                        "该账号现有待发布任务已抑制，不会继续累积或重试。",
                    )
                )
            },
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(publisher.feishu_webhook, json=payload)
            response.raise_for_status()

    async def notify_okx_authorization_error(
        self,
        account: AccountConfig,
        error: OkxAuthorizationError,
        blocked_until: float,
    ) -> None:
        publisher = self.config.publisher
        if not publisher.feishu_enabled or not publisher.feishu_webhook:
            return
        resume_at = datetime.fromtimestamp(blocked_until, timezone.utc).isoformat(
            timespec="minutes"
        )
        payload = {
            "msg_type": "text",
            "content": {
                "text": "\n".join(
                    (
                        "OKX 星球 KOL 发布已暂停",
                        f"账号：{account.account_id}",
                        "原因：authorization 无效或已过期",
                        f"接口信息：{error}",
                        f"本地保护恢复时间（UTC）：{resume_at}",
                        "请在 kol_config.json 更新 authorization / devid 后保存配置，",
                        "或手动重启 alphapumphunter-kol-publisher.service。",
                        "该账号现有待发布任务已抑制，不会继续重试。",
                    )
                )
            },
        }
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
        if account.platform == "okx":
            if not await self.okx_swap_catalog.has_usdt_swap(str(delivery["symbol"])):
                inst = OkxSwapCatalog.inst_id(str(delivery["symbol"]))
                self.store.suppress_delivery(
                    int(delivery["delivery_id"]),
                    f"okx: no live USDT-SWAP contract ({inst})",
                )
                logger.info(
                    "Suppressed OKX delivery {} for account {}: {} has no live OKX USDT-SWAP",
                    delivery["delivery_id"],
                    account.account_id,
                    delivery["symbol"],
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
                concise=(
                    account.concise
                    if account.concise is not None
                    else self.config.ai.concise
                ),
                max_chars=(
                    account.max_chars
                    if account.max_chars is not None
                    else self.config.ai.max_chars
                ),
            )
            writer = AIWriter(
                ai_config,
                account.tone,
                self.config.publisher.max_post_chars,
                platform=account.platform,
            )
            other_texts = self.store.generated_by_other_accounts(
                int(delivery["signal_id"]), account.account_id
            )
            enriched_delivery = dict(delivery)
            enriched_delivery["kline_context"] = await self.market_context.get(
                int(delivery["signal_id"]), str(delivery["symbol"])
            )
            text = await writer.generate(enriched_delivery, other_texts)
            if text in other_texts:
                raise RuntimeError("AI generated an exact duplicate of another account's post")
            if self.config.publisher.dry_run:
                result = PublishResult(None, None, {"dry_run": True})
                logger.info(
                    "DRY RUN delivery {} account {} ({}):\n{}",
                    delivery["delivery_id"],
                    account.account_id,
                    account.platform,
                    text,
                )
            elif account.platform == "okx":
                result = await OkxClient(
                    account.okx_authorization,
                    account.okx_devid,
                    group=account.okx_group,
                ).publish(text)
                logger.info(
                    "Published OKX delivery {} for account {} publishId={}: {}",
                    delivery["delivery_id"],
                    account.account_id,
                    (result.raw or {}).get("publishId"),
                    result.post_url or result.post_id or "post id unavailable",
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
        except OkxAuthorizationError as exc:
            blocked_until, suppressed_count = self.store.block_account_for_daily_post_limit(
                account.account_id,
                self.config.publisher.daily_post_limit_pause_seconds,
                exc,
            )
            logger.warning(
                "OKX authorization invalid for account {}; paused until {} and suppressed {} delivery(s)",
                account.account_id,
                datetime.fromtimestamp(blocked_until, timezone.utc).isoformat(timespec="minutes"),
                suppressed_count,
            )
            try:
                await self.notify_okx_authorization_error(account, exc, blocked_until)
            except Exception:
                logger.exception(
                    "Unable to send Feishu OKX auth alert for account {}", account.account_id
                )
        except SquareDailyPostLimitError as exc:
            if account.platform != "binance":
                self.store.mark_failed(delivery, exc, self.config.publisher)
                logger.exception(
                    "Unexpected Square daily-limit error for non-Binance account {}",
                    account.account_id,
                )
                return True
            blocked_until, suppressed_count = self.store.block_account_for_daily_post_limit(
                account.account_id,
                self.config.publisher.daily_post_limit_pause_seconds,
                exc,
            )
            logger.warning(
                "Square daily post limit reached for account {}; paused until {} and suppressed {} delivery(s)",
                account.account_id,
                datetime.fromtimestamp(blocked_until, timezone.utc).isoformat(timespec="minutes"),
                suppressed_count,
            )
            try:
                await self.notify_daily_post_limit(account, exc, blocked_until)
            except Exception:
                logger.exception(
                    "Unable to send Feishu daily-limit alert for account {}", account.account_id
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
    parser = argparse.ArgumentParser(
        description="AI KOL publisher for Binance Square and OKX Orbit"
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="Local JSON config path")
    parser.add_argument("--dry-run", action="store_true", help="Generate copy without calling Square")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="Continuously consume monitor signals")
    run.add_argument("--once", action="store_true", help="Process at most one pending signal")
    test = sub.add_parser("test-signal", help="Queue a synthetic signal for end-to-end validation")
    test.add_argument("symbol")
    test.add_argument("--direction", choices=["LONG", "SHORT", "WATCH"], default="LONG")
    discard = sub.add_parser(
        "discard-pending", help="Discard queued deliveries without allowing them to be recreated"
    )
    discard.add_argument("--account", help="Only discard one account's queue")
    sub.add_parser(
        "validate-accounts",
        help="Validate all enabled Binance Square keys and OKX credentials without posting",
    )
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
            if account.platform == "okx":
                OkxClient(
                    account.okx_authorization,
                    account.okx_devid,
                    group=account.okx_group,
                ).validate_credentials()
                logger.info("OKX credentials are configured for account {}", account.account_id)
            else:
                await SquareClient(account.square_api_key).validate_key()
                logger.info("Square key is valid for account {}", account.account_id)
        return
    if args.command == "discard-pending":
        discarded = SignalStore(config.publisher.database_path).discard_pending(args.account)
        scope = f"account {args.account}" if args.account else "all accounts"
        logger.info("Discarded {} queued delivery(s) for {}", discarded, scope)
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
