"""Explicit diagnostic JSON output with per-batch and shared disk limits."""

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .errors import ChainScopeError

DEFAULT_BATCH_LIMIT = 10 * 1024 * 1024
TOTAL_ARCHIVE_LIMIT = 100 * 1024 * 1024


class JsonArchive:
    def __init__(
        self,
        directory: Path,
        *,
        batch_limit: int = DEFAULT_BATCH_LIMIT,
        total_limit: int = TOTAL_ARCHIVE_LIMIT,
    ):
        if not 0 < batch_limit <= total_limit:
            raise ChainScopeError("调试归档容量设置不合法")
        self.directory = directory
        self.root = directory.parent
        self.batch_limit = batch_limit
        self.total_limit = total_limit
        directory.mkdir(parents=True, exist_ok=False)

    @contextmanager
    def _locked(self):
        # The lock also serializes the capacity check across collecting processes.
        with (self.root / ".archive-lock.json").open("a+b") as stream:
            stream.seek(0)
            deadline = time.monotonic() + 5
            while True:
                try:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ChainScopeError("调试归档繁忙，未取得写入锁") from None
                    time.sleep(0.05)
            try:
                if os.fstat(stream.fileno()).st_size == 0:
                    stream.write(b"{}")
                    stream.flush()
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def write_json(self, name: str, value) -> None:
        if Path(name).name != name or not name.endswith(".json"):
            raise ChainScopeError("调试归档文件名不合法")
        encoded = bytearray()
        encoder = json.JSONEncoder(ensure_ascii=False, default=str)
        for chunk in encoder.iterencode(value):
            data = chunk.encode("utf-8")
            if len(encoded) + len(data) > self.batch_limit:
                raise ChainScopeError("调试归档超过单批容量上限；缩小范围或关闭归档")
            encoded.extend(data)
        with self._locked():
            batch_bytes = sum(
                item.stat().st_size
                for item in self.directory.rglob("*")
                if item.is_file()
            )
            total_bytes = sum(
                item.stat().st_size for item in self.root.rglob("*") if item.is_file()
            )
            # Count temporary write space too, including during manifest replacement.
            if batch_bytes + len(encoded) > self.batch_limit:
                raise ChainScopeError("调试归档超过单批容量上限；缩小范围或关闭归档")
            if total_bytes + len(encoded) > self.total_limit:
                raise ChainScopeError("调试归档超过总容量上限；检查已有归档或关闭归档")
            target = self.directory / name
            temporary = self.directory / f".pending-{uuid4().hex}.json"
            try:
                with temporary.open("xb") as stream:
                    stream.write(encoded)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target)
            finally:
                if temporary.exists():
                    temporary.unlink()
