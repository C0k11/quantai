"""Lambda 入口：QuantAI 计算 API（HTTP API 后端）。

暴露两个**确定性计算**端点，不碰任何头寸数据：

    POST /options/price   Black-Scholes 理论价 + Greeks（入参全部由调用方给）
    POST /signals         对某标的重算组合信号（数据源是 S3 里的公开行情）

鉴权：`x-api-key` 头，与 SSM Parameter Store 的 SecureString 比对。
**参数没设值就一律拒绝**（fail closed，不是 fail open）。

冷启动时把 fact_prices 一次性读进内存，请求路径上零 S3 往返——代价是冷启动更慢，
这个取舍有实测数字（见 README）。

环境变量：
    QUANTAI_S3_BUCKET     数据湖桶名
    QUANTAI_API_KEY_PARAM SSM 参数名（SecureString）
"""
from __future__ import annotations

import json
import hmac
import os
from io import BytesIO
from typing import Any

import boto3

_s3 = boto3.client("s3")
_ssm = boto3.client("ssm")

_PRICES: Any = None       # 冷启动填充
_API_KEY: str | None = None


def _json(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _load_api_key() -> str:
    """从 SSM 读期望的 key。取不到就返回空串 -> 所有请求被拒。"""
    global _API_KEY
    if _API_KEY is None:
        name = os.environ.get("QUANTAI_API_KEY_PARAM", "")
        try:
            _API_KEY = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
        except Exception as exc:  # 参数不存在/无权限/未设值
            print(f"[api] API key 不可用，全部拒绝：{type(exc).__name__}")
            _API_KEY = ""
    return _API_KEY


def _authorised(event: dict) -> bool:
    expected = _load_api_key()
    if not expected:
        return False
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return hmac.compare_digest(headers.get("x-api-key", ""), expected)


def _prices():
    """行情表只在冷启动读一次；请求路径上不再打 S3。"""
    global _PRICES
    if _PRICES is None:
        import pandas as pd

        obj = _s3.get_object(Bucket=os.environ["QUANTAI_S3_BUCKET"], Key="exports/fact_prices.csv")
        df = pd.read_csv(BytesIO(obj["Body"].read()), parse_dates=["date"])
        _PRICES = df.sort_values("date")
    return _PRICES


def _options_price(body: dict) -> dict:
    from quantai.analysis.options import bs_greeks, bs_price

    try:
        S, K, T = float(body["S"]), float(body["K"]), float(body["T"])
        sigma = float(body["sigma"])
    except (KeyError, TypeError, ValueError) as exc:
        return _json(400, {"error": f"S/K/T/sigma 必填且须为数字：{exc}"})
    kind = str(body.get("kind", "call"))
    kwargs = {"r": float(body["r"])} if "r" in body else {}
    try:
        price = bs_price(S, K, T, sigma, kind, **kwargs)
        greeks = bs_greeks(S, K, T, sigma, kind, **kwargs)
    except ValueError as exc:  # 引擎自己会拒绝非正参数，不静默给 0
        return _json(400, {"error": str(exc)})
    return _json(200, {"price": price, "greeks": greeks,
                       "inputs": {"S": S, "K": K, "T": T, "sigma": sigma, "kind": kind}})


def _signals(body: dict) -> dict:
    from quantai.signals.generator import SignalGenerator

    symbol = str(body.get("symbol", "")).upper()
    if not symbol:
        return _json(400, {"error": "symbol 必填"})
    df = _prices()
    sub = df[df["symbol"] == symbol]
    if sub.empty:
        return _json(404, {"error": f"仓库里没有 {symbol} 的行情"})
    sub = sub.set_index("date")
    out = SignalGenerator().generate(sub)
    last = out.iloc[-1]
    return _json(200, {
        "symbol": symbol,
        "as_of": str(sub.index[-1].date()),
        "bars": int(len(sub)),
        "signals": {c: (None if last[c] != last[c] else
                        (float(last[c]) if c != "signal_strength" else str(last[c])))
                    for c in out.columns},
    })


_ROUTES = {"POST /options/price": _options_price, "POST /signals": _signals}


def handler(event, context):  # noqa: ANN001 - Lambda 签名
    if not _authorised(event):
        return _json(401, {"error": "unauthorized"})
    route = event.get("routeKey", "")
    fn = _ROUTES.get(route)
    if fn is None:
        return _json(404, {"error": f"no route {route}"})
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _json(400, {"error": "body 不是合法 JSON"})
    return fn(body)
