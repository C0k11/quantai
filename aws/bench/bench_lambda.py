"""Lambda 实测：冷启动、温调用、内存扫描。所有数字取自 Lambda 自己的 REPORT 行。

每次调用用 `LogType=Tail` 拿回日志尾巴，解析 REPORT 行里的
Init Duration / Duration / Billed Duration / Max Memory Used。不在客户端计时，
所以网络抖动不会混进函数耗时。

冷启动的制造方式：改一个无害的环境变量 BENCH_NONCE，Lambda 会为新配置拉起新的
执行环境，下一次调用必然是冷的（REPORT 里会出现 Init Duration，脚本会核对，
不是冷的就判这档数据作废）。

某档内存失败（例如 OOM）不中止整个扫描：失败本身就是一个结论，记进结果继续下一档。

**压测会临时改函数配置（内存、环境变量），这是 CloudFormation 之外的漂移。**
所以脚本在 finally 里把 MemorySize 和 Environment 原样恢复，结束后应跑一次
`aws cloudformation detect-stack-drift` 确认 IN_SYNC。

结果文件只含数值统计与函数名——**不写环境变量**（里面有桶名），**不写请求体**
（API 的请求体里有 key）。

用法：
    python aws/bench/bench_lambda.py --function quantai-etl-etl --memories 3008,1769,1024,512 --cold 1 --warm 3
    python aws/bench/bench_lambda.py --function quantai-etl-api --route options \\
        --memories 512,1024,1769,3008 --cold 20 --warm 30
"""
from __future__ import annotations

import argparse
import base64
import json
import math
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
RESULTS = Path(__file__).resolve().parent / "results"
REGION = "ca-central-1"

_REPORT = re.compile(
    r"REPORT RequestId: \S+\s+Duration: (?P<dur>[\d.]+) ms\s+"
    r"Billed Duration: (?P<billed>[\d.]+) ms\s+Memory Size: (?P<mem>\d+) MB\s+"
    r"Max Memory Used: (?P<used>\d+) MB(?:\s+Init Duration: (?P<init>[\d.]+) ms)?"
)

_ROUTES = {
    "options": ("POST /options/price",
                {"S": 100, "K": 105, "T": 0.5, "sigma": 0.25, "kind": "call"}),
    "signals": ("POST /signals", {"symbol": "SPY"}),
}


def _api_event(route: str) -> bytes:
    """HTTP API v2 形状的最小事件。key 从 gitignore 的本地文件读，不打印、不落结果。"""
    key_file = ROOT / ".aws-api-key.local"
    if not key_file.exists():
        raise SystemExit("缺 .aws-api-key.local：先用 aws ssm put-parameter 设好 API key")
    route_key, body = _ROUTES[route]
    return json.dumps({
        "version": "2.0",
        "routeKey": route_key,
        "headers": {"x-api-key": key_file.read_text(encoding="utf-8").strip(),
                    "content-type": "application/json"},
        "body": json.dumps(body),
        "isBase64Encoded": False,
    }).encode()


def _invoke(lam, fn: str, payload: bytes) -> dict:
    resp = lam.invoke(FunctionName=fn, LogType="Tail", Payload=payload)
    body = resp["Payload"].read()
    tail = base64.b64decode(resp["LogResult"]).decode("utf-8", "replace")
    if resp.get("FunctionError"):
        raise RuntimeError(f"FunctionError {body[:160]!r} | log tail {tail[-200:]!r}")
    m = _REPORT.search(tail)
    if not m:
        raise RuntimeError(f"REPORT 行解析失败：{tail[-300:]!r}")
    out = {
        "duration_ms": float(m["dur"]),
        "billed_ms": float(m["billed"]),
        "memory_mb": int(m["mem"]),
        "max_used_mb": int(m["used"]),
        "init_ms": float(m["init"]) if m["init"] else None,
    }
    # API 函数：顺手核对业务返回码，免得量了一堆 401
    try:
        status = json.loads(body).get("statusCode")
        if status is not None:
            out["status"] = int(status)
    except (ValueError, AttributeError):
        pass
    return out


def _reconfigure(lam, fn: str, memory: int, env: dict) -> None:
    lam.update_function_configuration(FunctionName=fn, MemorySize=memory,
                                      Environment={"Variables": env})
    lam.get_waiter("function_updated_v2").wait(FunctionName=fn)


def _pct(values: list[float], p: float) -> float:
    """最近秩法分位数（不插值，报样本里真实出现过的值）。"""
    s = sorted(values)
    return s[max(0, math.ceil(p / 100 * len(s)) - 1)]


def _summ(values: list[float]) -> dict:
    out = {"n": len(values), "min": min(values), "median": statistics.median(values),
           "max": max(values)}
    if len(values) >= 20:  # 样本太小时 p95 没有意义，不报
        out["p95"] = _pct(values, 95)
    return {k: round(v, 2) if isinstance(v, float) else v for k, v in out.items()}


def _one_tier(lam, fn: str, payload: bytes, mem: int, orig_env: dict,
              n_cold: int, n_warm: int) -> dict:
    """单档内存：先制造冷启动，再连打温调用。"""
    colds, warms = [], []
    for i in range(n_cold):
        _reconfigure(lam, fn, mem, {**orig_env, "BENCH_NONCE": f"{mem}-{i}-{time.time_ns()}"})
        r = _invoke(lam, fn, payload)
        if r["init_ms"] is None:
            raise RuntimeError("配置已改但这次不是冷启动——冷启动制造失败，本档作废")
        colds.append(r)
        print(f"[{mem}MB] cold {i + 1}/{n_cold}: init {r['init_ms']:.0f}ms "
              f"dur {r['duration_ms']:.0f}ms used {r['max_used_mb']}MB", flush=True)
    if not colds:  # 只测温调用时也要先落到这档内存，第一发是冷的，丢掉
        _reconfigure(lam, fn, mem, orig_env)
        _invoke(lam, fn, payload)
    for _ in range(n_warm):
        r = _invoke(lam, fn, payload)
        if r["init_ms"] is not None:
            raise RuntimeError("温调用里混进了冷启动——本档作废")
        warms.append(r)
    if warms:
        print(f"[{mem}MB] warm x{len(warms)}: median dur "
              f"{statistics.median(w['duration_ms'] for w in warms):.1f}ms", flush=True)
    return {
        "memory_mb": mem,
        "statuses": sorted({r["status"] for r in colds + warms if "status" in r}),
        "cold_init_ms": _summ([c["init_ms"] for c in colds]) if colds else None,
        "cold_duration_ms": _summ([c["duration_ms"] for c in colds]) if colds else None,
        "warm_duration_ms": _summ([w["duration_ms"] for w in warms]) if warms else None,
        "warm_billed_ms": _summ([w["billed_ms"] for w in warms]) if warms else None,
        "max_used_mb": max(r["max_used_mb"] for r in colds + warms),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--function", required=True)
    ap.add_argument("--route", choices=sorted(_ROUTES), help="API 函数必填")
    ap.add_argument("--memories", default="1024")
    ap.add_argument("--cold", type=int, default=1, help="每档内存的冷启动次数")
    ap.add_argument("--warm", type=int, default=10, help="每档内存的温调用次数")
    args = ap.parse_args()

    lam = boto3.client("lambda", region_name=REGION)
    fn = args.function
    payload = _api_event(args.route) if args.route else b"{}"

    base = lam.get_function_configuration(FunctionName=fn)
    orig_mem = base["MemorySize"]
    orig_env = dict(base.get("Environment", {}).get("Variables", {}))

    report: dict = {"function": fn, "route": args.route, "region": REGION,
                    "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "sweep": []}
    try:
        for mem in [int(x) for x in args.memories.split(",")]:
            try:
                report["sweep"].append(
                    _one_tier(lam, fn, payload, mem, orig_env, args.cold, args.warm))
            except Exception as exc:  # noqa: BLE001 - 某档失败（如 OOM）本身就是结论
                msg = f"{type(exc).__name__}: {str(exc)[:240]}"
                print(f"[{mem}MB] FAILED -> {msg}", flush=True)
                report["sweep"].append({"memory_mb": mem, "error": msg})
    finally:
        # 无论成败都把配置原样还回去——CloudFormation 才是事实来源
        _reconfigure(lam, fn, orig_mem, orig_env)
        print(f"[restore] {fn} -> {orig_mem}MB, env 原样恢复", flush=True)

    RESULTS.mkdir(exist_ok=True)
    tag = f"{fn}-{args.route}" if args.route else fn
    out = RESULTS / f"{tag}.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"[done] -> {out.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
