"""主 LLM 单轮本地验收入口；不连接网站账户、不建立记忆或研究任务。

示例：python -m chainscope_research --question "解释交易与日志的区别"
加 --events 可逐行查看流事件；这些是调试输出，不是已接入的前端 SSE 路由。
"""

import argparse
import asyncio
import sys
from pathlib import Path
from uuid import uuid4

from chainscope_research.bootstrap import build_main_application
from chainscope_research.contracts.main_agent import (
    ErrorEvent,
    MainTurnContext,
    MainTurnRequest,
    TextDeltaEvent,
)
from chainscope_research.core.errors import ResearchRuntimeError


async def run(question: str, show_events: bool) -> int:
    """输入一个问题，逐块打印真实模型正文；本地临时身份不映射网站用户。"""
    root = Path(__file__).resolve().parents[2]
    application = build_main_application(root)
    context = MainTurnContext(
        user_id="local-check",
        session_id=str(uuid4()),
        turn_id=str(uuid4()),
        request_revision=1,
    )
    request = MainTurnRequest.model_validate(
        {"messages": ({"role": "user", "content": question},)}
    )
    failed = False
    async for event in application.astream_turn(request, context):
        if show_events:
            print(event.model_dump_json(), flush=True)
        elif isinstance(event.payload, TextDeltaEvent):
            print(event.payload.text, end="", flush=True)
        if isinstance(event.payload, ErrorEvent):
            failed = True
            if not show_events:
                print(
                    f"\n{event.payload.code}：{event.payload.message}", file=sys.stderr
                )
    print()
    return 1 if failed else 0


def main() -> int:
    """解析单轮验收参数；配置错误只显示安全消息，不打印密钥和供应商异常。"""
    parser = argparse.ArgumentParser(
        description="ChainScope 主 LLM 流式验收（不保存记忆）"
    )
    parser.add_argument("--question", required=True, help="本轮问题")
    parser.add_argument("--events", action="store_true", help="显示类型化流事件")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.question, args.events))
    except ResearchRuntimeError as error:
        print(f"{error.code}：{error.public_message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
