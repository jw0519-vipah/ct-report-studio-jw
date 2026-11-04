import asyncio
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app as target


def response(status, body, headers=None):
    req = httpx.Request("GET", "https://api.openai.com/v1/models")
    return httpx.Response(status, request=req, json=body, headers=headers or {})


def body_of(resp):
    return json.loads(resp.body.decode())


def test_401():
    out = target.classify_openai_http_error(response(401, {"error": {"message":"bad key","type":"invalid_request_error","code":"invalid_api_key"}}))
    b = body_of(out)
    assert out.status_code == 401 and b["error"]["kind"] == "authentication_error"


def test_429_quota():
    out = target.classify_openai_http_error(response(429, {"error": {"message":"no credits","type":"insufficient_quota","code":"credit_balance_exhausted"}}))
    b = body_of(out)
    assert out.status_code == 429 and b["error"]["kind"] == "quota_error"


def test_429_rate():
    out = target.classify_openai_http_error(response(429, {"error": {"message":"rate","type":"rate_limit_error","code":"rate_limit_exceeded"}}, {"retry-after":"2"}))
    b = body_of(out)
    assert out.status_code == 429 and b["error"]["kind"] == "rate_limit_error" and b["error"]["retry_after"] == "2"


def test_5xx():
    out = target.classify_openai_http_error(response(500, {"error":{"message":"upstream"}}))
    b = body_of(out)
    assert out.status_code == 502 and b["error"]["kind"] == "upstream_error"

if __name__ == "__main__":
    for fn in [test_401, test_429_quota, test_429_rate, test_5xx]: fn()
    print("error mapping tests: PASS")
