"""Live smoke test for KOL AI generation using kol_config.json."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from kol_publisher import AIConfig, AIWriter, PublisherConfig, _tone, normalize_symbol

CONFIG_PATH = Path("kol_config.json")

SAMPLE_SIGNAL = {
    "symbol": "ACE",
    "source": "kol_main.py",
    "indicator": "alpha_surge",
    "direction": "LONG",
    "summary": "Alpha 币 8 分钟上涨 7.2%，并通过成交量过滤",
    "price": 0.4412,
    "details_json": json.dumps(
        {
            "change_pct": 7.2,
            "window_minutes": 8,
            "energy_level": 2,
            "energy_note": "能量不错",
            "futures_quote_volume_24h": 85000000,
        },
        ensure_ascii=False,
    ),
    "kline_context": [
        {
            "open_time_utc": "2026-08-28T02:00+00:00",
            "open": 0.42,
            "high": 0.445,
            "low": 0.418,
            "close": 0.441,
            "quote_volume": 4200000.0,
            "trade_count": 1280,
            "taker_buy_quote_volume": 2300000.0,
            "closed": True,
        }
    ],
}


def load_test_config():
    data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    ai_data = data.get("ai") or {}
    default_tone = _tone(data.get("default_tone") or {})
    publisher = PublisherConfig(**(data.get("publisher") or {}))
    accounts = []
    for row in data.get("accounts") or []:
        if not row.get("enabled", True):
            continue
        account_ai = row.get("ai") or {}
        accounts.append(
            {
                "account_id": str(row.get("id") or "unknown"),
                "platform": str(row.get("platform") or "binance").lower(),
                "tone": _tone(row.get("tone") or {}, default_tone),
                "model": str(account_ai.get("model") or "").strip() or None,
                "temperature": (
                    float(account_ai["temperature"])
                    if account_ai.get("temperature") is not None
                    else None
                ),
                "concise": (
                    bool(account_ai["concise"])
                    if account_ai.get("concise") is not None
                    else None
                ),
                "max_chars": (
                    int(account_ai["max_chars"])
                    if account_ai.get("max_chars") is not None
                    else None
                ),
            }
        )
    ai = AIConfig(
        base_url=str(ai_data.get("base_url") or "").rstrip("/"),
        api_key=str(ai_data.get("api_key") or "").strip(),
        model=str(ai_data.get("model") or "").strip(),
        temperature=float(ai_data.get("temperature", 0.9)),
        timeout_seconds=float(ai_data.get("timeout_seconds", 45)),
        concise=bool(ai_data.get("concise", False)),
        max_chars=int(ai_data.get("max_chars", 1200)),
    )
    return ai, publisher, accounts


def check_text(text: str, platform: str, direction: str = "LONG") -> list[str]:
    symbol = normalize_symbol(str(SAMPLE_SIGNAL["symbol"]))
    issues: list[str] = []
    if not text.strip():
        issues.append("empty text")
    if len(text) > 1200:
        issues.append(f"too long: {len(text)} chars")
    if "http" in text.lower():
        issues.append("contains URL")
    if direction == "LONG" and not any(term in text for term in ("做多", "看多")):
        issues.append("missing LONG direction wording")
    if platform == "binance":
        if f"${symbol}" not in text:
            issues.append("missing cashtag")
    elif f"${symbol}" in text:
        issues.append("okx should not use cashtag")
    return issues


async def run_one(ai_base, publisher, account) -> dict:
    ai = AIConfig(
        base_url=ai_base.base_url,
        api_key=ai_base.api_key,
        model=account["model"] or ai_base.model,
        temperature=(
            account["temperature"]
            if account["temperature"] is not None
            else ai_base.temperature
        ),
        timeout_seconds=ai_base.timeout_seconds,
        concise=(
            account["concise"] if account["concise"] is not None else ai_base.concise
        ),
        max_chars=(
            account["max_chars"] if account["max_chars"] is not None else ai_base.max_chars
        ),
    )
    writer = AIWriter(
        ai,
        account["tone"],
        publisher.max_post_chars,
        platform=account["platform"],  # type: ignore[arg-type]
    )
    text = await writer.generate(SAMPLE_SIGNAL)
    return {
        "account": account["account_id"],
        "platform": account["platform"],
        "model": ai.model,
        "chars": len(text),
        "text": text,
        "issues": check_text(text, account["platform"]),
    }


async def main() -> int:
    ai, publisher, accounts = load_test_config()
    print(f"AI endpoint: {ai.base_url}")
    print(f"Global model: {ai.model}")

    if not accounts:
        print("No enabled accounts")
        return 1

    ok = True
    for account in accounts:
        print("\n" + "=" * 60)
        try:
            result = await run_one(ai, publisher, account)
        except Exception as exc:
            ok = False
            print(f"[FAIL] account={account['account_id']} platform={account['platform']}")
            print(f"error: {exc!r}")
            continue

        status = "PASS" if not result["issues"] else "WARN"
        if result["issues"]:
            ok = False
        print(
            f"[{status}] account={result['account']} platform={result['platform']} "
            f"model={result['model']} chars={result['chars']}"
        )
        if result["issues"]:
            print("issues:", ", ".join(result["issues"]))
        print("text:")
        print(result["text"])

    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
