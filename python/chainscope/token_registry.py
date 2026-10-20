"""Operator-maintained BSC admission registry; no model can mutate this file."""

import hashlib
import json
import tomllib
from pathlib import Path

from pydantic import Field

from .agent_contracts import Contract, Policy, Token
from .config import ROOT


class AgentError(Exception):
    def __init__(self, code, message, recovery="human_action_required", run_id=None):
        super().__init__(message)
        self.code, self.recovery, self.run_id = code, recovery, run_id


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


class Registry(Contract):
    version: int
    policy: Policy
    tokens: list[Token] = Field(min_length=1)

    @classmethod
    def load(cls, path: Path = ROOT / "config" / "supported_tokens.toml"):
        try:
            result = cls.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            raise AgentError(
                "REGISTRY_INVALID", "代币服务列表缺失或格式不正确"
            ) from None
        identities = [(t.chain_id, t.address) for t in result.tokens]
        if (
            result.version != 1
            or len(set(identities)) != len(identities)
            or len({t.token_id for t in result.tokens}) != len(result.tokens)
        ):
            raise AgentError("REGISTRY_INVALID", "服务列表版本不支持或代币身份重复")
        return result

    def resolve(self, token_id, *, enabled=True):
        token = next((t for t in self.tokens if t.token_id == token_id), None)
        if token is None:
            raise AgentError("TOKEN_NOT_SUPPORTED", "当前代币不在服务范围内", "none")
        if enabled and not token.enabled:
            raise AgentError(
                "TOKEN_DISABLED", "当前代币已停用，不能开始或继续采集", "none"
            )
        return token

    def fingerprint(self, token):
        return digest({"token": token.model_dump(), "policy": self.policy.model_dump()})
