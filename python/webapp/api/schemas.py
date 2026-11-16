"""每个接口“收什么、回什么”的数据格式（Pydantic 模型）。

FastAPI 会按这些模型自动检查请求：缺字段、类型不对、多了不认识的字段、字符串太长，
都会在进入业务逻辑之前被拒绝（app.py 把这类错误统一转成 400，并且不回显用户输入）。
这里的长度上限只是“明显不正常就别往下传”的粗过滤，真正的规则（邮箱格式、密码 8～128 位）
仍在 core/emails.py 和 security/passwords.py 里检查。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Request(BaseModel):
    # extra="forbid"：请求里多了不认识的字段就拒绝，防止前端以为传了某个参数其实没生效。
    model_config = ConfigDict(extra="forbid")


# ── 请求 ──


class RegistrationRequest(Request):
    email: str = Field(max_length=320)


class RegistrationVerify(Request):
    challenge_id: UUID
    code: str = Field(max_length=16)


class RegistrationComplete(Request):
    registration_token: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class LoginRequest(Request):
    email: str = Field(max_length=320)
    password: str = Field(max_length=1024)


class CurrentPassword(Request):
    """退出所有设备、注销账户：只需要当前密码。"""

    current_password: str = Field(max_length=1024)


class PasswordChange(Request):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class PasswordResetRequest(Request):
    email: str = Field(max_length=320)


class PasswordResetVerify(Request):
    challenge_id: UUID
    code: str = Field(max_length=16)


class PasswordResetComplete(Request):
    reset_token: str = Field(max_length=200)
    new_password: str = Field(max_length=1024)


# ── 响应 ──


class CsrfResponse(BaseModel):
    csrf_token: str  # 前端放在内存里，提交修改操作时放进 X-CSRF-Token 请求头
    authenticated: bool  # 当前是否已登录


class ChallengeResponse(BaseModel):
    challenge_id: UUID  # 第二步提交验证码时一起带上
    message: str


class RegistrationTokenResponse(BaseModel):
    # 第三步设置密码时带上；只在内存里保存，不放进网址或 localStorage。
    registration_token: str


class ResetTokenResponse(BaseModel):
    # 第三步设置新密码时带上；只在内存里保存，不放进网址或 localStorage。
    reset_token: str


class AccountResponse(BaseModel):
    user_id: UUID
    email: str
    created_at: datetime


class LoginResponse(BaseModel):
    account: AccountResponse
    csrf_token: str  # 登录后换了新会话，CSRF 令牌也随之更换


class MessageResponse(BaseModel):
    message: str


class ErrorResponse(BaseModel):
    """所有错误统一的格式。"""

    code: str  # 固定的英文编号，前端据此判断，例如 rate_limited
    message: str  # 给用户看的中文提示
    request_id: str  # 请求编号，排查问题时和后端审计、日志对应
