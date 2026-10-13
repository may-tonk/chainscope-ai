"""Run the real RpcClient against controlled simulated responses for W1 learning."""

import argparse
import json

import httpx

from chainscope.errors import ChainScopeError
from chainscope.rpc_client import RpcClient

SCENARIOS = ("network", "timeout", "rate-limit", "rpc-error", "empty")


def simulate(scenario: str) -> dict:
    attempts = []
    waits = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        attempts.append({"attempt": len(attempts) + 1, "method": payload["method"]})
        if scenario == "network":
            raise httpx.ConnectError("simulated disconnected network", request=request)
        if scenario == "timeout":
            raise httpx.ReadTimeout("simulated read timeout", request=request)
        if scenario == "rate-limit" and len(attempts) < 3:
            return httpx.Response(429)
        body = {"jsonrpc": "2.0", "id": payload["id"]}
        if scenario == "rpc-error":
            body["error"] = {"code": -32601, "message": "simulated missing method"}
        else:
            body["result"] = {"transfers": []} if scenario == "empty" else "0x38"
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rpc = RpcClient("https://example.invalid", client=client, sleep=waits.append)
        report = {
            "scenario": scenario,
            "source": "simulation",
            "planned_wait_seconds": waits,
        }
        method = "alchemy_getAssetTransfers" if scenario == "empty" else "eth_chainId"
        try:
            result = rpc.call(method, [])
        except ChainScopeError as exc:
            report.update(outcome="failure", error=str(exc))
        else:
            report.update(outcome="success", result=result)
        report.update(attempts=attempts, request_count=rpc.requests)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="W1：真实请求代码的可控异常演示")
    parser.add_argument("--scenario", choices=(*SCENARIOS, "all"), default="all")
    args = parser.parse_args()
    selected = SCENARIOS if args.scenario == "all" else (args.scenario,)
    print(
        "模拟练习：使用项目 RpcClient，响应由 MockTransport 提供；不请求真实 API，不操作 MySQL。"
    )
    print("planned_wait_seconds 是生产代码计划的等待；演示中记录数值并跳过实际等待。")
    for scenario in selected:
        print(json.dumps(simulate(scenario), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
