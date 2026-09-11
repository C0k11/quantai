"""Lambda 入口：QuantAI 计算 API（HTTP API 后端）。

暴露两个**确定性计算**端点，不碰任何头寸数据：

    POST /options/price   Black-Scholes 理论价 + Greeks（入参全部由调用方给）
    POST /signals         对某标的重算组合信号（数据源是 S3 里的公开行情）

外加 GET /health（不带 key）：只回 {"ready": true/false}，即服务它的执行环境是否已经读到 API key
（读成功就缓存；读失败后 60 秒内不再重试，从那次读开始算），不带任何报错细节；CI 每次部署后查它。

鉴权：`x-api-key` 头，与 SSM Parameter Store 的 SecureString 比对。
**参数没设值就一律拒绝**（fail closed，不是 fail open）；读失败不永久缓存，退避后重试。

init 阶段（冷启动）只做 import：scipy、pandas、信号模块在模块顶层导入。新镜像部署后，CI 发几个
不带 key 的请求（全部 401），每个都会起一个已经把这些依赖加载好的执行环境，部署后首调的代价由 CI
付，不落到第一个真实请求上，CI 也不需要持有 API key。
网络读取（API key、行情表）不放在 init：init 有 10 s 上限，一个卡住的 S3/SSM 调用会让整个函数起不来，
连 401 路径都会失败。它们留在请求路径上，受函数超时约束，客户端超时收短，失败按退避重试：
key 读不到时，需要 key 的路由一律 401，GET /health 回 503 {"ready": false}；行情表读不到 /signals 返回 503。

环境变量：
    QUANTAI_S3_BUCKET     数据湖桶名
    QUANTAI_API_KEY_PARAM SSM 参数名（SecureString）
"""
from __future__ import annotations

import hmac
import json
import os
import time
from io import BytesIO
from typing import Any

import boto3
import pandas as pd
from botocore.config import Config

from quantai.analysis.options import bs_greeks, bs_price
from quantai.signals.generator import SignalGenerator

# boto3 默认连接、读取超时各 60 s。这里一次 S3/SSM 调用最坏约 2 x (1 + 3) + 1 = 9 s（第一次重试前的退避不超过 1 s）。
# /signals 在新执行环境上会先读 key 再读行情表，最坏约 18 s；函数超时 20 s（aws/template.yaml），
# 所以依赖出问题时返回的是 401/503，而不是被超时掐断。
_AWS_CFG_KWARGS = {"connect_timeout": 1, "read_timeout": 3,
                   "retries": {"total_max_attempts": 2, "mode": "standard"}}
_AWS_CFG = Config(**_AWS_CFG_KWARGS)
_s3 = boto3.client("s3", config=_AWS_CFG)
_ssm = boto3.client("ssm", config=_AWS_CFG)

KEY_RETRY_SEC = 60.0
PRICES_RETRY_SEC = 30.0
_now = time.monotonic          # 单独引用一份，测试里可以换成假时钟

_API_KEY: str | None = None
_KEY_RETRY_AT = 0.0            # 读 key 失败后，这个时刻之前不再打 SSM
_PRICES: Any = None
_PRICES_RETRY_AT = 0.0         # 读行情表失败后，这个时刻之前不再打 S3


def _json(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(body),
    }


def _load_api_key() -> str:
    """从 SSM 读期望的 key。取不到就返回空串 -> 所有请求被拒（fail closed）。

    读成功才缓存；读失败不永久缓存，KEY_RETRY_SEC 之后再试。否则 key 建好之前起来的执行环境
    会一直 401 到被回收；退避期内不再打 SSM，被没 key 的请求刷也只会每分钟试一次。
    """
    global _API_KEY, _KEY_RETRY_AT
    if _API_KEY:
        return _API_KEY
    now = _now()
    if now < _KEY_RETRY_AT:
        return ""
    name = os.environ.get("QUANTAI_API_KEY_PARAM", "")
    try:
        value = _ssm.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]
    except Exception as exc:  # 参数不存在/无权限/未设值/超时
        print(f"[api] API key 不可用，全部拒绝，{KEY_RETRY_SEC:.0f} 秒后重试：{type(exc).__name__}")
        _KEY_RETRY_AT = now + KEY_RETRY_SEC
        return ""
    if not value:
        print(f"[api] API key 为空值，全部拒绝，{KEY_RETRY_SEC:.0f} 秒后重试")
        _KEY_RETRY_AT = now + KEY_RETRY_SEC
        return ""
    _API_KEY = value
    return _API_KEY


def _authorised(event: dict) -> bool:
    expected = _load_api_key()
    if not expected:
        return False
    headers = {str(k).lower(): v for k, v in (event.get("headers") or {}).items()}
    given = headers.get("x-api-key")
    if not isinstance(given, str):
        return False
    # 按字节比较：带非 ASCII 字符的 str 会让 compare_digest 抛 TypeError（变成 500 而不是 401）。
    return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def _prices():
    """行情表只读一次；读失败返回 None，PRICES_RETRY_SEC 之内不再打 S3。"""
    global _PRICES, _PRICES_RETRY_AT
    if _PRICES is not None:
        return _PRICES
    now = _now()
    if now < _PRICES_RETRY_AT:
        return None
    try:
        obj = _s3.get_object(Bucket=os.environ["QUANTAI_S3_BUCKET"], Key="exports/fact_prices.csv")
        df = pd.read_csv(BytesIO(obj["Body"].read()), parse_dates=["date"])
    except Exception as exc:  # noqa: BLE001 - 读不到就 503，稍后再试
        print(f"[api] 行情表不可用，{PRICES_RETRY_SEC:.0f} 秒后重试：{type(exc).__name__}")
        _PRICES_RETRY_AT = now + PRICES_RETRY_SEC
        return None
    _PRICES = df.sort_values("date")
    return _PRICES


def _options_price(body: dict) -> dict:
    try:
        S, K, T = float(body["S"]), float(body["K"]), float(body["T"])
        sigma = float(body["sigma"])
        kwargs = {"r": float(body["r"])} if "r" in body else {}
    except (KeyError, TypeError, ValueError) as exc:
        return _json(400, {"error": f"S/K/T/sigma 必填且须为数字，r 可选且须为数字：{exc}"})
    kind = str(body.get("kind", "call"))
    try:
        price = bs_price(S, K, T, sigma, kind, **kwargs)
        greeks = bs_greeks(S, K, T, sigma, kind, **kwargs)
    except ValueError as exc:  # 引擎自己会拒绝非正参数，不静默给 0
        return _json(400, {"error": str(exc)})
    return _json(200, {"price": price, "greeks": greeks,
                       "inputs": {"S": S, "K": K, "T": T, "sigma": sigma, "kind": kind}})


def _signals(body: dict) -> dict:
    symbol = str(body.get("symbol", "")).upper()
    if not symbol:
        return _json(400, {"error": "symbol 必填"})
    df = _prices()
    if df is None:
        return _json(503, {"error": "行情表暂时不可用，请稍后重试"})
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


def _health() -> dict:
    """GET /health，不带 key：服务它的执行环境是否已经读到 API key。只回 ready，不带任何报错细节（原因看函数日志）。

    和不带 key 的 POST 走同一个 _load_api_key：读成功一次就缓存，读失败后 KEY_RETRY_SEC 之内（从那次读开始算）
    不再打 SSM，所以它读 SSM 不会比 401 路径更频繁；不读行情表。init 不联网（见模块说明），所以不能像 credit-default
    那样只报 init 阶段的状态，那样一个还没接过请求的新执行环境会被误报成读不到 key。
    """
    return _json(200, {"ready": True}) if _load_api_key() else _json(503, {"ready": False})


def handler(event, context):  # noqa: ANN001 - Lambda 签名
    route = event.get("routeKey", "")
    if route == "GET /health":
        return _health()
    if not _authorised(event):
        return _json(401, {"error": "unauthorized"})
    fn = _ROUTES.get(route)
    if fn is None:
        return _json(404, {"error": f"no route {route}"})
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _json(400, {"error": "body 不是合法 JSON"})
    return fn(body)
