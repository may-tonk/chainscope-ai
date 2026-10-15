"""Small synchronous JSON-RPC client with bounded, explicit retries."""

import json
import time
from collections import Counter
from typing import Any

import httpx

from .archive import JsonArchive
from .errors import ChainScopeError

RETRY_STATUS = (429, 500, 502, 503, 504)
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_WAIT_SECONDS = 60
# Alchemy compute units per method (official table, checked 2026-10-14).
COMPUTE_UNITS = {
    "alchemy_getAssetTransfers": 120,
    "eth_blockNumber": 10,
    "eth_call": 26,
    "eth_chainId": 0,
    "eth_getBlockByNumber": 20,
    "eth_getCode": 20,
    "eth_getTransactionReceipt": 20,
}


def retry_wait(headers, attempt: int) -> float:
    """Server-provided Retry-After when present, else exponential backoff."""
    if isinstance(headers, httpx.Response):
        headers = headers.headers
    value = headers.get("Retry-After", "").strip()
    if value.isdigit():
        return min(int(value), MAX_WAIT_SECONDS)
    return min(2**attempt, MAX_WAIT_SECONDS)


def read_limited(response: httpx.Response, limit: int, message: str) -> bytes:
    """Read a streamed body, failing as soon as it passes `limit` bytes.

    A declared Content-Length over the limit fails before reading; a missing
    or false one (chunked, compressed or lying) is caught by counting the bytes
    actually received, so memory never holds more than about one chunk extra.
    """
    size = response.headers.get("Content-Length", "")
    if size.isdigit() and int(size) > limit:
        raise ChainScopeError(message)
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > limit:
            raise ChainScopeError(message)
    return bytes(body)


class RpcClient:
    def __init__(
        self, url: str, *, client: httpx.Client | None = None, sleep=time.sleep
    ):
        self._url = url
        self._client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owned = client is None
        self._sleep = sleep
        self.requests = 0
        self.calls: Counter = Counter()
        self.rate_limited = 0
        self._id = 0
        self.archive: JsonArchive | None = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        if self._owned:
            self._client.close()

    def call(self, method: str, params: list) -> Any:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        for attempt in range(3):
            self.requests += 1
            try:
                status, headers, content = self._post(payload)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 2:
                    raise ChainScopeError(
                        f"{method}: 网络连接或超时失败，已尝试 3 次"
                    ) from None
                self._sleep(2**attempt)
                continue
            if status == 429:
                self.rate_limited += 1
            if status in RETRY_STATUS and attempt < 2:
                self._sleep(retry_wait(headers, attempt))
                continue
            if status != 200:
                raise ChainScopeError(f"{method}: HTTP {status}")
            # Answered requests are what the provider bills; count them here.
            self.calls[method] += 1
            try:
                body = json.loads(content)
            except ValueError:
                raise ChainScopeError(f"{method}: 返回内容不是 JSON") from None
            if (
                not isinstance(body, dict)
                or body.get("jsonrpc") != "2.0"
                or body.get("id") != self._id
            ):
                raise ChainScopeError(f"{method}: JSON-RPC 响应格式或 ID 错误")
            if "error" in body:
                error = body["error"]
                code = error.get("code") if isinstance(error, dict) else None
                # Provider messages can echo credentials. Only expose numeric codes.
                code = code if isinstance(code, int) else "unknown"
                raise ChainScopeError(f"{method}: JSON-RPC 错误 {code}")
            if "result" not in body:
                raise ChainScopeError(f"{method}: 缺少 result 字段")
            if self.archive:
                self.archive.write_json(
                    f"rpc-{self._id:04d}-{method}.json",
                    {"request": payload, "response": body},
                )
            return body["result"]
        raise AssertionError("unreachable retry state")

    def _post(self, payload: dict) -> tuple[int, httpx.Headers, bytes]:
        """POST and read the body, stopping as soon as it passes the size limit."""
        with self._client.stream("POST", self._url, json=payload) as response:
            if response.status_code != 200:
                return response.status_code, response.headers, b""
            body = read_limited(
                response, MAX_RESPONSE_BYTES, "RPC 响应超过 16 MiB 上限"
            )
            return response.status_code, response.headers, body

    def compute_units(self) -> int:
        """Estimated Alchemy compute units for the calls made so far."""
        return sum(COMPUTE_UNITS.get(m, 26) * n for m, n in self.calls.items())
