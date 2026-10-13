"""Small synchronous JSON-RPC client with bounded, explicit retries."""

import time
from typing import Any

import httpx

from .archive import JsonArchive
from .errors import ChainScopeError


class RpcClient:
    def __init__(
        self, url: str, *, client: httpx.Client | None = None, sleep=time.sleep
    ):
        self._url = url
        self._client = client or httpx.Client(timeout=30, follow_redirects=False)
        self._owned = client is None
        self._sleep = sleep
        self.requests = 0
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
                response = self._client.post(self._url, json=payload)
            except (httpx.TimeoutException, httpx.NetworkError):
                if attempt == 2:
                    raise ChainScopeError(
                        f"{method}: 网络连接或超时失败，已尝试 3 次"
                    ) from None
                self._sleep(2**attempt)
                continue
            if response.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                self._sleep(2**attempt)
                continue
            if response.status_code != 200:
                raise ChainScopeError(f"{method}: HTTP {response.status_code}")
            try:
                body = response.json()
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
