"""Bind executable tool scope to the actual local user's turn, never model prose."""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .agent_contracts import PlanData, RunData
from .query import BEIJING
from .token_registry import AgentError

CLOCK = re.compile(
    r"(?<![\d:])(\d{1,2})[:：](\d{2})(?:[:：](\d{2})(?:\.(\d{1,6}))?)?(?![\d:])"
)
ISO = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?"
)
DATE = re.compile(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})日?")
QUESTION = re.compile(
    r"[?？]|吗|么|呢|能否|是否|能不能|可不可以|怎么|如何|为什么|什么意思|介绍|解释|演示|举例|假设|如果"
)
PREVIEW = re.compile(
    r"先.*(?:计划|展示|看看|检查)|只.*(?:计划|查看|查询)|准备.*计划|(?:查看|查询).*(?:状态|进度|历史)|(?:不要|不用|别|暂不|先不|不)(?:再)?(?:启动|执行|采集|入库)|只看|只读|暂不执行"
)


def deadline(text, issued_at):
    """Explicit clocks use Beijing time and today's date unless a date is stated."""
    iso = list(ISO.finditer(text))
    clocks = list(CLOCK.finditer(text))
    zone = re.search(r"(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::(\d{2}))?", text, re.I)
    explicit_zone = None
    if zone:
        if int(zone[2]) > 23 or int(zone[3] or 0) > 59:
            raise AgentError(
                "TIME_NEEDS_CLARIFICATION", "时区偏移无效，请明确有效的时区。"
            )
        offset = int(zone[2]) * 60 + int(zone[3] or 0)
        if zone[1] == "-":
            offset = -offset
        explicit_zone = timezone(timedelta(minutes=offset))
        clocks = [c for c in clocks if not (zone.start() <= c.start() < zone.end())]
    elif re.search(r"\b(?:UTC|GMT)\b", text, re.I):
        from datetime import UTC

        explicit_zone = UTC
    chinese = list(re.finditer(r"(\d{1,2})点(?:(\d{1,2})分?)?(?:(\d{1,2})秒)?", text))
    if iso:
        extra = [
            c for c in clocks if not any(i.start() <= c.start() < i.end() for i in iso)
        ]
        if len(iso) != 1 or extra or chinese:
            raise AgentError(
                "TIME_NEEDS_CLARIFICATION", "检测到多个时间，请明确一个采集截止时间。"
            )
        value = datetime.fromisoformat(iso[0].group())
        if (
            value.utcoffset() is not None
            and explicit_zone is not None
            and value.utcoffset() != explicit_zone.utcoffset(None)
        ):
            raise AgentError(
                "TIME_NEEDS_CLARIFICATION", "检测到不一致的时区，请明确采用哪个时区。"
            )
        return (
            value.replace(tzinfo=explicit_zone or BEIJING).astimezone(BEIJING)
            if value.utcoffset() is None
            else value.astimezone(BEIJING)
        )
    if not clocks and not chinese:
        if re.search(
            r"昨天|前天|昨晚|今早|今晚|分钟前|小时前|秒前|\d{4}[-年/]\d{1,2}[-月/]\d{1,2}|\d{1,2}月\d{1,2}日|\d{1,2}[-/]\d{1,2}",
            text,
        ):
            raise AgentError(
                "TIME_NEEDS_CLARIFICATION",
                "请补充具体截止时刻，例如‘2026-10-19 19:40（北京时间）’。",
            )
        return None
    if re.search(
        r"[上下本]周|上个月|上月|去年|星期|周[一二三四五六日天]|天前|天后|昨晚|今早|今晚|点半|点.*刻",
        text,
    ):
        raise AgentError(
            "TIME_NEEDS_CLARIFICATION",
            "请把相对日期写成具体日期和时间，避免定位到错误的一天。",
        )
    if len(clocks) + len(chinese) > 1:
        raise AgentError(
            "TIME_NEEDS_CLARIFICATION",
            "检测到多个时间；请明确一个采集截止时间。当前从数据库已有进度续采。",
        )
    match = clocks[-1] if clocks else chinese[-1]
    hour, minute = int(match[1]), int(match[2] or 0)
    second = int(match[3] or 0)
    microsecond = int((match[4] or "").ljust(6, "0")) if clocks else 0
    prefix = text[max(0, match.start() - 5) : match.start()]
    if re.search(r"下午|晚上|傍晚", prefix):
        if hour > 12:
            raise AgentError(
                "TIME_NEEDS_CLARIFICATION",
                "上下午与小时写法不一致，请用 24 小时制明确截止时间。",
            )
        hour = hour + 12 if hour < 12 else hour
    elif "凌晨" in prefix and hour == 12:
        hour = 0
    elif chinese and hour < 13 and not re.search(r"上午|早上|凌晨|中午", prefix):
        raise AgentError(
            "TIME_NEEDS_CLARIFICATION",
            "请用 24 小时制说明截止时间，例如‘07:40’或‘19:40’。",
        )
    base = issued_at.astimezone(BEIJING)
    date = DATE.search(text)
    if date:
        base = base.replace(year=int(date[1]), month=int(date[2]), day=int(date[3]))
    elif partial := re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", text):
        base = base.replace(month=int(partial[1]), day=int(partial[2]))
    elif partial := re.search(r"(?<![\d/-])(\d{1,2})[-/](\d{1,2})(?![\d/-])", text):
        base = base.replace(month=int(partial[1]), day=int(partial[2]))
    elif "前天" in text:
        base -= timedelta(days=2)
    elif "昨天" in text:
        base -= timedelta(days=1)
    elif "明天" in text or "后天" in text:
        base += timedelta(days=2 if "后天" in text else 1)
    value = base.replace(
        hour=hour, minute=minute, second=second, microsecond=microsecond
    )
    if explicit_zone is not None:
        value = value.replace(tzinfo=explicit_zone).astimezone(BEIJING)
    return value


@dataclass
class UserIntent:
    text: str
    token_id: str | None = None
    target: dict | None = None
    start: bool = False
    stop: bool = False
    clarification: str | None = None
    prepared: PlanData | None = None
    started: dict | None = None
    pending_id: str | None = None
    allowed_plans: set[str] = field(default_factory=set)
    allowed_run_id: str | None = None
    error: dict | None = None
    observed_runs: dict = field(default_factory=dict)

    @classmethod
    def capture(cls, text, registry, *, issued_at=None, pending=None, known_runs=None):
        issued_at = issued_at or datetime.now(BEIJING)
        value = cls(text=text)
        clean = text.strip().rstrip("。.!！").strip()
        if re.fullmatch(
            r"确认(?:采集|执行)?|好的?[,， ]*(?:执行|开始)?|执行|开始|按这个计划执行|可以",
            clean,
        ):
            if pending is None:
                value.clarification = (
                    "当前没有待确认的计划，请直接说明要采集的代币和截止时间。"
                )
                return value
            value.token_id, value.prepared, value.pending_id, value.start = (
                pending.token.token_id,
                pending,
                pending.plan_id,
                True,
            )
            value.allowed_plans.add(pending.plan_id)
            value.target = (
                {"mode": "resume", "run_id": pending.resume_of_run_id}
                if pending.resume_of_run_id
                else (
                    {"mode": "time", "end_time": pending.target.requested_time}
                    if pending.target.requested_time
                    else {"mode": "block", "end_block": pending.target.block}
                )
            )
            return value
        known_runs = known_runs or {}
        matches = {
            t.token_id
            for t in registry.tokens
            if (
                re.search(
                    r"(?<![a-z0-9_])" + re.escape(t.token_id) + r"(?![a-z0-9_])",
                    text,
                    re.I,
                )
                or t.address.lower() in text.lower()
            )
        }
        active_words = bool(
            re.search(r"采集|同步|抓取|续采|恢复|入库|collect|sync", text, re.I)
        )
        semantic = text.replace("如果没有问题", "").replace("如果没问题", "")
        if re.search(r"(?:请|麻烦|能否|可以)?帮我", semantic) and not re.search(
            r"解释|看看|介绍|如何|怎么", semantic
        ):
            semantic = (
                semantic.replace("能否", "").replace("可以", "").rstrip("？?吗。！! ")
            )
        command_prefix = r"^\s*(?:(?:好的?|可以|那么|现在|今天|目前|就|直接|立即|马上|请|麻烦|帮我|请帮我|我想要?|我要|我需要|我希望)[，,\s]*)*(?:把|将|采集|同步|抓取|开始|启动|继续|恢复|续采|停止|暂停|collect|sync|stop)"
        clauses = re.split(r"[，,。；;\n]", semantic)
        directive = any(re.search(command_prefix, clause, re.I) for clause in clauses)
        for token_id in matches:
            directive = directive or bool(
                re.search(
                    r"^\s*"
                    + re.escape(token_id)
                    + r"\s*(?:继续|采集|同步|抓取|停止|暂停)",
                    semantic,
                    re.I,
                )
            )
        statement = bool(
            re.search(
                r"采集(?:已)?(?:完成|结束|好了|完了)|(?:已经|正在|已完成).*(?:采集|同步|停止|暂停)",
                semantic,
            )
        )
        if statement and not re.search(
            r"请|帮我|麻烦|(?:现在|再)继续|重新(?:采集|启动)", semantic
        ):
            directive = False
        value.stop = (
            directive
            and bool(re.search(r"停止|暂停|stop", semantic, re.I))
            and not QUESTION.search(semantic)
            and not PREVIEW.search(semantic)
        )
        value.start = (
            active_words
            and directive
            and not value.stop
            and not QUESTION.search(semantic)
            and not PREVIEW.search(semantic)
        )
        if len(matches) != 1:
            if value.start or value.stop:
                value.clarification = (
                    "请明确一个服务列表中的代币标识或完整合约地址；一次操作一个代币。"
                )
            value.start = value.stop = False
            return value
        value.token_id = next(iter(matches))
        if value.stop:
            identities = re.findall(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", text
            )
            value.allowed_run_id = identities[0] if len(identities) == 1 else None
            if value.allowed_run_id is None and value.token_id in known_runs:
                value.allowed_run_id = known_runs[value.token_id].run_id
            return value
        if not active_words:
            return value
        try:
            moment = deadline(text, issued_at)
            block = re.search(r"(?:到|至|截止)\s*区块\s*(\d+)", text)
            if moment is not None and block:
                raise AgentError(
                    "TIME_NEEDS_CLARIFICATION",
                    "请指定截止时间或截止区块中的一种，避免两个边界不一致。",
                )
            if moment is not None:
                if moment > issued_at:
                    raise AgentError(
                        "TARGET_IN_FUTURE",
                        "指定截止时间尚未到达。请明确一个过去的日期和时间，不会自动改成最新时间或昨天。",
                    )
                value.target = {"mode": "time", "end_time": moment.isoformat()}
            elif block:
                value.target = {"mode": "block", "end_block": int(block[1])}
            else:
                value.target = {"mode": "latest_finalized"}
            previous = known_runs.get(value.token_id)
            resume_words = bool(
                re.search(
                    r"恢复|(?<!继)续采|继续.*(?:原|上次|刚才)|(?:原|上次|刚才).*继续",
                    text,
                )
            )
            continue_paused = (
                previous is not None
                and previous.state in {"paused", "stopped", "interrupted"}
                and "继续" in text
                and not re.search(r"新建|新任务", text)
            )
            if resume_words or continue_paused:
                if previous is None:
                    raise AgentError(
                        "RUN_NEEDS_CLARIFICATION",
                        "尚未找到这个代币的原运行，请先查看它的运行状态，或明确新建一次采集。",
                    )
                if moment is not None and (
                    previous.target.requested_time is None
                    or datetime.fromisoformat(previous.target.requested_time) != moment
                ):
                    raise AgentError(
                        "TARGET_CHANGED",
                        "恢复原运行不能改变原截止时间；请说明是恢复原目标，还是新建不同范围的采集。",
                    )
                if (
                    moment is None
                    and not resume_words
                    and re.search(r"现在|当前|最新", text)
                ):
                    raise AgentError(
                        "TARGET_CHANGED",
                        "这个代币有暂停的运行。请说明是恢复原目标，还是新建采集到现在的任务；不会自动绕过原运行预算。",
                    )
                value.target = {"mode": "resume", "run_id": previous.run_id}
        except (AgentError, ValueError) as exc:
            value.clarification = (
                str(exc)
                if isinstance(exc, AgentError)
                else "截止时间格式无效，请用‘2026-10-19 19:40（北京时间）’这样的写法。"
            )
            value.start = False
        return value

    @property
    def evidence(self):
        return {
            "source": "explicit_user_turn",
            "user_text_sha256": hashlib.sha256(self.text.encode()).hexdigest(),
            "token_id": self.token_id,
            "target": self.target,
            "confirmed_plan_id": self.pending_id,
        }

    @property
    def tool_names(self):
        names = {
            "get_collection_status",
            "get_collection_run",
            "get_collection_event",
            "list_collection_runs",
            "prepare_collection",
        }
        if self.start:
            names.add("start_collection")
        if self.stop:
            names.add("stop_collection")
        return frozenset(names)

    def prepare_args(self, arguments):
        if self.token_id is not None and self.target is not None:
            if arguments.get("token_id") != self.token_id:
                raise AgentError(
                    "USER_SCOPE_MISMATCH", "模型选择的代币与当前用户指令不符"
                )
            # Binding is made from stdin before the model call. The model cannot
            # silently replace an explicit human clock with latest_finalized.
            return {**arguments, "target": dict(self.target)}
        return arguments

    def observe(self, reply):
        data = reply.get("data") or {}
        if reply.get("outcome") == "error":
            self.error = reply
            return
        self.error = None
        if data.get("kind") == "plan":
            plan = PlanData.model_validate(data)
            if self.token_id is None or plan.token.token_id == self.token_id:
                self.prepared = plan
                self.allowed_plans.add(plan.plan_id)
        elif data.get("kind") == "start":
            self.started = data
            if data.get("run"):
                run = RunData.model_validate(data["run"])
                self.observed_runs[run.token_id] = run
        elif data.get("kind") == "run":
            run = RunData.model_validate(data)
            self.observed_runs[run.token_id] = run
        elif self.stop and data.get("kind") == "status":
            active = data.get("active_execution") or {}
            if data.get("token", {}).get("token_id") == self.token_id and active.get(
                "managed"
            ):
                self.allowed_run_id = active.get("run_id")

    def finish_requested_start(self, service, operation_id):
        """Complete an already explicit instruction if the model only prepared it."""
        if not self.start or self.clarification or self.started:
            return None
        if self.error:
            return self.error
        if self.prepared is None:
            reply = service.call(
                "prepare_collection",
                {"token_id": self.token_id, "target": self.target},
                operation_id,
            )
            self.observe(reply)
            if reply["outcome"] == "error":
                return reply
        if self.prepared.plan_state == "blocked":
            return None
        reply = service.call(
            "start_collection",
            {"plan_id": self.prepared.plan_id},
            operation_id,
            authorization=self,
        )
        self.observe(reply)
        return reply

    def model_hint(self):
        return (
            f"当前用户原话由本地程序捕获。允许直接采集={self.start}，允许停止={self.stop}；"
            f"代币标识={self.token_id}，锁定截止条件={self.target}。"
            "明确采集要求已经构成该范围内的授权，不再要求用户复制编号或重复确认。"
            "如果存在阻断，说明具体原因；不能声称已经启动或完成。"
        )

    def __call__(self, action, data):
        if action == "stop":
            return (
                self.stop
                and data.token_id == self.token_id
                and data.run_id == self.allowed_run_id
            )
        if (
            not self.start
            or data.token.token_id != self.token_id
            or data.plan_id not in self.allowed_plans
        ):
            return False
        if self.target and self.target["mode"] == "time":
            expected = datetime.fromisoformat(self.target["end_time"])
            return (
                data.target.requested_time is not None
                and datetime.fromisoformat(data.target.requested_time) == expected
                and datetime.fromisoformat(data.target.time_beijing) <= expected
            )
        if self.target and self.target["mode"] == "block":
            return data.target.block == self.target["end_block"]
        if self.target and self.target["mode"] == "resume":
            return data.resume_of_run_id == self.target["run_id"]
        return self.target is not None and self.target["mode"] == "latest_finalized"


def start_summary(data):
    if data.get("disposition") == "no_work":
        return "指定范围已经采集并入库完成，本次没有重复采集。"
    run = RunData.model_validate(data["run"])
    status = (
        "这次采集已经完成"
        if run.state == "completed"
        else "已接受启动请求，后台正在启动"
        if run.state == "queued"
        else "已找到对应采集运行"
    )
    requested = (
        f"你指定的截止时间是 {run.target.requested_time}；"
        if run.target.requested_time
        else ""
    )
    return f"{status}：{run.token_id.upper()}。{requested}本次实际目标为北京时间 {run.target.time_beijing}（区块 {run.target.block}）。\n运行预算上限：{run.budget.max_runtime_seconds / 60:g} 分钟、{run.budget.max_http_attempts} 次采集 HTTP 尝试。结束、暂停或异常时会主动报告。"


def preview_summary(plan):
    requested = (
        f"指定截止时间：{plan.target.requested_time}。\n"
        if plan.target.requested_time
        else ""
    )
    text = f"{plan.token.symbol} 采集计划：\n{requested}实际目标：北京时间 {plan.target.time_beijing}（区块 {plan.target.block}）。\n预算上限：{plan.budget.max_runtime_seconds / 60:g} 分钟、{plan.budget.max_http_attempts} 次采集 HTTP 尝试。"
    if plan.plan_state == "blocked":
        return text + "\n暂时不能启动：" + "；".join(i.message for i in plan.blockers)
    if plan.plan_state == "no_work":
        return text + "\n指定范围已经完成，无需重复采集。"
    return (
        text
        + "\n当前只展示计划，尚未启动。需要执行时回复‘确认’，或直接说明新的采集要求。"
    )
