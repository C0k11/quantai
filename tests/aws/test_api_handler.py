"""aws/api/handler.py: the init phase only imports (no network I/O), so a keyless 401 request warms
a new image without any S3/SSM call being able to push init past its 10 s limit; the key and the
price table are read on the request path with back-offs; auth stays fail-closed.

No AWS access: boto3 and botocore are replaced by fakes, and the handler is loaded from its file
path as a fresh module per test, so module-level caches never leak between tests.
"""
from __future__ import annotations

import importlib.util
import io
import itertools
import json
import sys
import types
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

HANDLER = Path(__file__).resolve().parents[2] / "aws" / "api" / "handler.py"
TEMPLATE = HANDLER.parents[1] / "template.yaml"
_counter = itertools.count()


def _prices_csv(n: int = 260) -> bytes:
    dates = pd.bdate_range("2025-01-02", periods=n)
    close = 100 + np.cumsum(np.random.default_rng(3).normal(0, 1, n))
    df = pd.DataFrame({"symbol": "SPY", "date": dates.strftime("%Y-%m-%d"), "open": close,
                       "high": close + 1, "low": close - 1, "close": close, "volume": 1_000_000})
    return df.to_csv(index=False).encode()


PRICES_CSV = _prices_csv()


class Scripted:
    """Plays back scripted outcomes in order, then repeats the last one; exceptions are raised."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def _next(self):
        self.calls += 1
        out = self.outcomes[min(self.calls, len(self.outcomes)) - 1]
        if isinstance(out, BaseException):
            raise out
        return out


class FakeSSM(Scripted):
    def get_parameter(self, Name, WithDecryption):  # noqa: N803 - boto3 signature
        return {"Parameter": {"Value": self._next()}}


class FakeS3(Scripted):
    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 signature
        return {"Body": io.BytesIO(self._next())}


class FakeConfig:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture
def load(monkeypatch):
    def _load(ssm_outcomes=("k-123",), s3_outcomes=(PRICES_CSV,)):
        ssm, s3 = FakeSSM(ssm_outcomes), FakeS3(s3_outcomes)
        monkeypatch.setitem(sys.modules, "boto3",
                            types.SimpleNamespace(client=lambda name, **_: {"ssm": ssm, "s3": s3}[name]))
        monkeypatch.setitem(sys.modules, "botocore", types.ModuleType("botocore"))
        monkeypatch.setitem(sys.modules, "botocore.config", types.SimpleNamespace(Config=FakeConfig))
        monkeypatch.setenv("QUANTAI_API_KEY_PARAM", "/test/api-key")
        monkeypatch.setenv("QUANTAI_S3_BUCKET", "test-bucket")
        spec = importlib.util.spec_from_file_location(f"api_handler_under_test_{next(_counter)}", HANDLER)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)          # this is the Lambda init phase
        return mod, ssm, s3

    return _load


def event(key=None, route="POST /options/price", body=None):
    headers = {"content-type": "application/json"}
    if key is not None:
        headers["x-api-key"] = key
    if body is None:
        body = {"S": 100, "K": 100, "T": 0.5, "sigma": 0.2} if route == "POST /options/price" else {"symbol": "SPY"}
    return {"version": "2.0", "routeKey": route, "rawPath": route.split()[1], "headers": headers,
            "requestContext": {"http": {"method": "POST"}}, "body": json.dumps(body), "isBase64Encoded": False}


def status(mod, ev):
    return mod.handler(ev, None)["statusCode"]


# ---------------------------------------------------------------- init phase
def test_init_does_no_network_io(load):
    mod, ssm, s3 = load()
    assert ssm.calls == 0 and s3.calls == 0


def _function_timeout(name: str) -> int:
    lines = TEMPLATE.read_text(encoding="utf-8").splitlines()
    for line in lines[lines.index(f"  {name}:") + 1:]:
        if line.startswith("  ") and not line.startswith("   "):   # the next resource
            break
        if line.strip().startswith("Timeout:"):
            return int(line.split(":", 1)[1])
    raise AssertionError(f"{name} has no Timeout")


def test_client_budget_fits_the_function_timeout(load):
    mod, _, _ = load()
    real = pytest.importorskip("botocore.config")
    real.Config(**mod._AWS_CFG_KWARGS)                  # raises on an unknown option
    k = mod._AWS_CFG_KWARGS
    # every attempt is connect + read; the first retry waits at most 1 s (botocore standard mode)
    per_call = k["retries"]["total_max_attempts"] * (k["connect_timeout"] + k["read_timeout"]) + 1
    # /signals on a new environment reads the key and then the price table in one request
    assert 2 * per_call < _function_timeout("ApiFunction")


# ---------------------------------------------------------------- auth
def test_unauthenticated_request_is_401_and_reads_the_key_once(load):
    mod, ssm, s3 = load()
    assert status(mod, event()) == 401                 # what the CI warm-up sends
    assert status(mod, event()) == 401
    assert ssm.calls == 1 and s3.calls == 0


def test_correct_key_prices_an_option(load):
    mod, _, _ = load()
    out = mod.handler(event(key="k-123"), None)
    assert out["statusCode"] == 200 and json.loads(out["body"])["price"] > 0


@pytest.mark.parametrize("key", ["not-the-key", "", "k-123 ", "k-\u00e9\u00e9"])
def test_wrong_or_non_ascii_key_is_401_not_500(load, key):
    mod, _, _ = load()
    assert status(mod, event(key=key)) == 401


def test_non_numeric_rate_is_400(load):
    mod, _, _ = load()
    assert status(mod, event(key="k-123", body={"S": 100, "K": 100, "T": 0.5, "sigma": 0.2, "r": "x"})) == 400


def test_failed_key_read_is_retried_after_backoff_not_cached(load, monkeypatch):
    mod, ssm, _ = load(ssm_outcomes=(RuntimeError("ParameterNotFound"), "k-123"))
    clock = [1000.0]
    monkeypatch.setattr(mod, "_now", lambda: clock[0])
    assert status(mod, event(key="k-123")) == 401      # fail closed
    assert ssm.calls == 1
    clock[0] += mod.KEY_RETRY_SEC - 1                  # inside the back-off: no SSM call
    assert status(mod, event(key="k-123")) == 401
    assert ssm.calls == 1
    clock[0] += 2                                      # back-off expired
    assert status(mod, event(key="k-123")) == 200
    assert ssm.calls == 2
    clock[0] += 3600                                   # a success is cached
    assert status(mod, event(key="k-123")) == 200
    assert ssm.calls == 2


def test_empty_parameter_value_counts_as_unavailable(load, monkeypatch):
    mod, ssm, _ = load(ssm_outcomes=("", "k-123"))
    clock = [0.0]
    monkeypatch.setattr(mod, "_now", lambda: clock[0])
    assert status(mod, event(key="")) == 401
    clock[0] += mod.KEY_RETRY_SEC + 1
    assert status(mod, event(key="k-123")) == 200
    assert ssm.calls == 2


# ---------------------------------------------------------------- /signals
def test_signals_loads_prices_lazily_once(load):
    mod, _, s3 = load()
    out = mod.handler(event(key="k-123", route="POST /signals"), None)
    assert out["statusCode"] == 200 and json.loads(out["body"])["bars"] == 260
    assert status(mod, event(key="k-123", route="POST /signals")) == 200
    assert s3.calls == 1


def test_signals_503_while_prices_unavailable_then_recovers(load, monkeypatch):
    mod, _, s3 = load(s3_outcomes=(RuntimeError("AccessDenied"), PRICES_CSV))
    clock = [0.0]
    monkeypatch.setattr(mod, "_now", lambda: clock[0])
    assert status(mod, event(key="k-123", route="POST /signals")) == 503
    clock[0] += mod.PRICES_RETRY_SEC - 1               # inside the back-off: no S3 call
    assert status(mod, event(key="k-123", route="POST /signals")) == 503
    assert s3.calls == 1
    clock[0] += 2
    assert status(mod, event(key="k-123", route="POST /signals")) == 200
    assert s3.calls == 2


def test_signals_unknown_symbol_is_404(load):
    mod, _, _ = load()
    assert status(mod, event(key="k-123", route="POST /signals", body={"symbol": "NOPE"})) == 404
