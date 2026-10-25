"""认证接口（方案第 9 节），统一以 /api/auth 开头。

接口函数只做三件事：取出请求里的数据 → 交给 services 里的业务函数 → 设置 Cookie、返回结果。
所有检查（CSRF、是否登录）在 dependencies.py；所有规则（限速、密码、会话）在 services。
出错时业务函数抛出的异常由 app.py 统一转换成 HTTP 状态码和错误格式。

这里的函数都是普通 def（不是 async def）：FastAPI 会把它们放进线程池执行，
数据库操作和密码哈希不会卡住其他请求。
"""

from fastapi import APIRouter, Depends, Request, Response

from ..core.errors import NotAuthenticated
from ..services import accounts, registration
from ..services.sessions import (
    SessionInfo,
    csrf_token_for,
    revoke,
    start_anonymous,
)
from .cookies import clear_session_cookie, set_session_cookie
from .dependencies import (
    client_source,
    current_session,
    ensure_csrf,
    request_id,
    require_csrf,
    require_user,
    require_user_csrf,
    web_context,
)
from .schemas import (
    AccountResponse,
    ChallengeResponse,
    CsrfResponse,
    CurrentPassword,
    LoginRequest,
    LoginResponse,
    MessageResponse,
    PasswordChange,
    RegistrationComplete,
    RegistrationRequest,
    RegistrationTokenResponse,
    RegistrationVerify,
)

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _account(request: Request, user_id) -> AccountResponse:
    account = accounts.get_account(web_context(request).engine, user_id)
    if account is None:  # 账户刚被注销
        raise NotAuthenticated()
    return AccountResponse(
        user_id=account.user_id, email=account.email, created_at=account.created_at
    )


def _context_args(request: Request) -> dict:
    """几乎每个业务函数都要的两个参数：来源 IP 和请求编号（写进审计）。"""
    return {"source": client_source(request), "request_id": request_id(request)}


# ── 打开页面时调用 ──


@router.get("/csrf", response_model=CsrfResponse)
def get_csrf(
    request: Request,
    response: Response,
    session: SessionInfo | None = Depends(current_session),
):
    """取得 CSRF 令牌。浏览器还没有有效会话时，顺便新建一个匿名会话并设置 Cookie。"""
    context = web_context(request)
    if session is None:
        issued = start_anonymous(
            context.engine, context.keys, source=client_source(request)
        )
        set_session_cookie(response, issued.token, "anonymous")
        return CsrfResponse(csrf_token=issued.csrf_token, authenticated=False)
    return CsrfResponse(
        csrf_token=csrf_token_for(context.keys, session.session_id),
        authenticated=session.kind == "user",
    )


# ── 注册三步 ──


@router.post("/registration/request", response_model=ChallengeResponse, status_code=202)
def registration_request(
    body: RegistrationRequest,
    request: Request,
    session: SessionInfo = Depends(require_csrf),
):
    """第 1 步：提交邮箱。无论邮箱是否已注册，返回内容都一样。"""
    context = web_context(request)
    result = registration.start(
        context.engine,
        context.keys,
        context.mailer,
        body.email,
        flow_session_id=session.session_id,
        **_context_args(request),
    )
    return ChallengeResponse(
        challenge_id=result.challenge_id,
        message="如果这个邮箱可以注册，验证码已经发出，请查收（也请检查垃圾邮件）",
    )


@router.post("/registration/verify", response_model=RegistrationTokenResponse)
def registration_verify(
    body: RegistrationVerify,
    request: Request,
    session: SessionInfo = Depends(require_csrf),
):
    """第 2 步：提交验证码，换取 10 分钟有效、只能用一次的注册凭证。"""
    context = web_context(request)
    token = registration.verify(
        context.engine,
        context.keys,
        body.challenge_id,
        body.code,
        flow_session_id=session.session_id,
        **_context_args(request),
    )
    return RegistrationTokenResponse(registration_token=token)


@router.post("/registration/complete", response_model=MessageResponse, status_code=201)
def registration_complete(
    body: RegistrationComplete,
    request: Request,
    session: SessionInfo = Depends(require_csrf),
):
    """第 3 步：设置密码，创建账户。成功后需要去登录页登录。"""
    context = web_context(request)
    registration.complete(
        context.engine,
        context.keys,
        body.registration_token,
        body.password,
        flow_session_id=session.session_id,
        **_context_args(request),
    )
    return MessageResponse(message="注册成功，请登录")


# ── 登录和账户 ──


@router.post("/login", response_model=LoginResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: SessionInfo = Depends(require_csrf),
):
    """登录成功后：撤销原来的会话（匿名会话或之前的登录），换成新的已登录 Cookie。

    换新令牌是为了防“会话固定”攻击：别人事先塞给你的令牌，在你登录后会作废。
    """
    context = web_context(request)
    result = accounts.login(
        context.engine,
        context.keys,
        body.email,
        body.password,
        **_context_args(request),
    )
    with context.engine.begin() as connection:
        revoke(connection, session.session_id)
    set_session_cookie(response, result.session.token, "user")
    return LoginResponse(
        account=_account(request, result.user_id),
        csrf_token=result.session.csrf_token,
    )


@router.get("/me", response_model=AccountResponse)
def me(request: Request, session: SessionInfo = Depends(require_user)):
    """当前账户的基本信息。用户编号只来自会话，不接受前端指定，所以只能看自己的。"""
    return _account(request, session.user_id)


@router.post("/logout", response_model=MessageResponse)
def logout(
    request: Request,
    response: Response,
    session: SessionInfo | None = Depends(current_session),
):
    """退出当前设备。已经没有有效会话时也返回成功并清除 Cookie（重复点退出不会报错）。"""
    if session is not None:
        ensure_csrf(request, session)  # 有会话时必须通过 CSRF，别的网站不能替你退出
        accounts.logout(
            web_context(request).engine,
            session.session_id,
            session.user_id,
            **_context_args(request),
        )
    clear_session_cookie(response)
    return MessageResponse(message="已退出")


@router.post("/logout-all", response_model=MessageResponse)
def logout_all(
    body: CurrentPassword,
    request: Request,
    response: Response,
    session: SessionInfo = Depends(require_user_csrf),
):
    """退出所有设备（需要当前密码）。"""
    context = web_context(request)
    accounts.logout_everywhere(
        context.engine,
        context.keys,
        session.user_id,
        body.current_password,
        **_context_args(request),
    )
    clear_session_cookie(response)
    return MessageResponse(message="已退出所有设备，请重新登录")


@router.post("/password/change", response_model=MessageResponse)
def change_password(
    body: PasswordChange,
    request: Request,
    response: Response,
    session: SessionInfo = Depends(require_user_csrf),
):
    """修改密码。成功后所有设备（包括当前这个）都需要用新密码重新登录。"""
    context = web_context(request)
    accounts.change_password(
        context.engine,
        context.keys,
        session.user_id,
        body.current_password,
        body.new_password,
        **_context_args(request),
    )
    clear_session_cookie(response)
    return MessageResponse(message="密码已修改，请用新密码重新登录")


@router.post("/account/delete", response_model=MessageResponse)
def delete_account(
    body: CurrentPassword,
    request: Request,
    response: Response,
    session: SessionInfo = Depends(require_user_csrf),
):
    """注销账户：立即彻底删除，不可恢复。"""
    context = web_context(request)
    accounts.delete_account(
        context.engine,
        context.keys,
        session.user_id,
        body.current_password,
        **_context_args(request),
    )
    clear_session_cookie(response)
    return MessageResponse(message="账户已注销")
