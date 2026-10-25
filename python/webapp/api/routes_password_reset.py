"""重置密码接口（2c），统一以 /api/auth/password-reset 开头。

三步都要求 CSRF（和注册一样绑定发起重置的那个浏览器），都不需要登录。

邮件用 FastAPI 的 BackgroundTasks 在“回复发出之后”再发送：
邮箱有没有账户，回复的内容和速度都一样，别人没法借这个接口探测谁注册过。
"""

from fastapi import APIRouter, BackgroundTasks, Depends, Request

from ..services import password_reset
from ..services.sessions import SessionInfo
from .dependencies import client_source, request_id, require_csrf, web_context
from .schemas import (
    ChallengeResponse,
    MessageResponse,
    PasswordResetComplete,
    PasswordResetRequest,
    PasswordResetVerify,
    ResetTokenResponse,
)

router = APIRouter(prefix="/api/auth/password-reset", tags=["password-reset"])


def _context_args(request: Request) -> dict:
    return {"source": client_source(request), "request_id": request_id(request)}


@router.post("/request", response_model=ChallengeResponse, status_code=202)
def request_reset(
    body: PasswordResetRequest,
    request: Request,
    background: BackgroundTasks,
    session: SessionInfo = Depends(require_csrf),
):
    """第 1 步：提交邮箱。无论邮箱是否有账户，返回内容都一样；邮件在回复之后发送。"""
    context = web_context(request)
    args = _context_args(request)
    started = password_reset.start(
        context.engine,
        context.keys,
        body.email,
        flow_session_id=session.session_id,
        **args,
    )
    background.add_task(
        password_reset.deliver_code, context.engine, context.mailer, started, **args
    )
    return ChallengeResponse(
        challenge_id=started.challenge_id,
        message="如果这个邮箱有账户，验证码已经发出，请查收（也请检查垃圾邮件）",
    )


@router.post("/verify", response_model=ResetTokenResponse)
def verify_reset(
    body: PasswordResetVerify,
    request: Request,
    session: SessionInfo = Depends(require_csrf),
):
    """第 2 步：提交验证码，换取 10 分钟有效、只能用一次的重置凭证。"""
    context = web_context(request)
    token = password_reset.verify(
        context.engine,
        context.keys,
        body.challenge_id,
        body.code,
        flow_session_id=session.session_id,
        **_context_args(request),
    )
    return ResetTokenResponse(reset_token=token)


@router.post("/complete", response_model=MessageResponse)
def complete_reset(
    body: PasswordResetComplete,
    request: Request,
    background: BackgroundTasks,
    session: SessionInfo = Depends(require_csrf),
):
    """第 3 步：设置新密码。所有设备退出登录，账户和内容保留；之后用新密码登录。"""
    context = web_context(request)
    args = _context_args(request)
    email = password_reset.complete(
        context.engine,
        context.keys,
        body.reset_token,
        body.new_password,
        flow_session_id=session.session_id,
        **args,
    )
    background.add_task(
        password_reset.deliver_changed_notice,
        context.engine,
        context.mailer,
        email,
        **args,
    )
    return MessageResponse(message="密码已重置，所有设备已退出登录，请用新密码登录")
