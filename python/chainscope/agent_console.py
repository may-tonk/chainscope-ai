"""Single input reader; model and background notifications never read stdin directly."""

import asyncio
import queue
import sys
import threading
from contextlib import nullcontext

from prompt_toolkit import PromptSession
from prompt_toolkit.patch_stdout import patch_stdout


class AsyncConsole:
    def __init__(self, *, stream=None, output=None):
        self.stream, self.output = stream or sys.stdin, output or sys.stdout
        self.loop = asyncio.get_running_loop()
        self.session = (
            PromptSession() if self.stream.isatty() and self.output.isatty() else None
        )
        self.requests = queue.Queue()
        self.guard = patch_stdout(raw=True) if self.session else nullcontext()
        self.guard.__enter__()
        if self.session is None:
            threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        while True:
            request = self.requests.get()
            if request is None:
                return
            prompt, future = request
            print(prompt, end="", file=self.output, flush=True)
            try:
                value = self.stream.readline()
                error = EOFError() if value == "" else None
            except (OSError, ValueError):
                value, error = "", EOFError()

            def complete(future=future, value=value, error=error):
                if not future.done():
                    if error:
                        future.set_exception(error)
                    else:
                        future.set_result(value.rstrip("\r\n"))

            try:
                self.loop.call_soon_threadsafe(complete)
            except RuntimeError:
                return

    async def ask(self, prompt):
        if self.session:
            return await self.session.prompt_async(prompt)
        future = self.loop.create_future()
        self.requests.put((prompt, future))
        return await future

    def ask_sync(self, prompt):
        return asyncio.run_coroutine_threadsafe(self.ask(prompt), self.loop).result()

    async def display(self, text):
        print(
            f"\n助手 > {text}\n",
            file=sys.stdout if self.session else self.output,
            flush=True,
        )

    def approve(self, action, data):
        if action == "start":
            from .agent_intent import preview_summary

            print(preview_summary(data), file=self.output, flush=True)
        else:
            print(
                f"准备停止 {data.token_id.upper()} 的当前采集。",
                file=self.output,
                flush=True,
            )
        return self.ask_sync("确认 / 取消 > ").strip() in {
            "确认",
            "确认执行",
            "好的",
            "可以",
        }

    def close(self):
        self.requests.put(None)
        self.guard.__exit__(None, None, None)
