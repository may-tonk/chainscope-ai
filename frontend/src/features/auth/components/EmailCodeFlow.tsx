/**
 * 通用的“邮箱验证三步流程”：① 填邮箱 → ② 输验证码 → ③ 设置密码。
 *
 * 注册和忘记密码的流程完全一样，区别只在调用哪组后端接口、页面上写什么字。
 * 所以流程本身（现在在第几步、每步成功后去哪、重新发送、修改邮箱、流程失效后重新开始）
 * 只在这里写一份；页面（RegisterPage、ResetPasswordPage）只传入“配置”。
 * 以后改流程只改这里，两个页面同时生效；再有类似的流程（例如更换邮箱）也可以直接复用。
 *
 * 安全约定（方案 3.4）：challenge_id 和凭证（注册凭证 / 重置凭证）只放在这个组件的内存状态里，
 * 不写进网址或浏览器存储；刷新页面会回到第 1 步，这是有意的。
 * 后端还把整个流程绑定在当前浏览器的会话上，别的浏览器拿到验证码或凭证也用不了。
 */
import { useState } from "react";

import { Steps } from "../../../shared/ui/Steps";
import type { ChallengeResponse } from "../api";
import { CodeStep } from "./CodeStep";
import { EmailStep } from "./EmailStep";
import { NewPasswordStep } from "./NewPasswordStep";

/** 流程的配置：每一步调用的接口和显示的文字。 */
export interface EmailCodeFlowProps {
  /** 三个步骤的名称，例如 ["验证邮箱", "输入验证码", "设置密码"]。 */
  stepLabels: readonly [string, string, string];
  /** 给读屏软件的步骤条说明，例如“注册进度”。 */
  stepsAriaLabel: string;
  /** 第 1 步：邮箱框的初始内容（例如从登录页带过来的邮箱）。 */
  initialEmail?: string;
  /** 第 1 步：邮箱框下方的说明。 */
  emailHint: string;
  /** 第 1 步：按钮文字，例如“获取验证码”。 */
  emailSubmitLabel: string;
  /** 第 3 步：密码框的名称，例如“密码”“新密码”。 */
  passwordLabel: string;
  /** 第 3 步：按钮文字，例如“完成注册”。 */
  passwordSubmitLabel: string;
  /** 第 3 步：后端表示“整个流程已失效、要从头开始”的错误编号。 */
  restartCode: string;
  /** 申请验证码（第 1 步和“重新发送”都调用它）。 */
  requestCode: (email: string) => Promise<ChallengeResponse>;
  /** 核对验证码，返回下一步要用的凭证。 */
  verifyCode: (challengeId: string, code: string) => Promise<string>;
  /** 用凭证和新密码完成流程。 */
  complete: (token: string, password: string) => Promise<void>;
  /** 流程完成后调用（通常是跳到登录页），参数是流程使用的邮箱。 */
  onCompleted: (email: string) => void;
}

/**
 * 流程进行到哪一步，以及这一步需要的数据。
 * 每一步只带上它真正需要的东西，例如只有第 3 步才有凭证 token。
 */
type FlowState =
  | { step: "email"; email: string }
  | { step: "code"; email: string; challengeId: string; notice: string }
  | { step: "password"; email: string; token: string };

const STEP_INDEX: Record<FlowState["step"], number> = { email: 0, code: 1, password: 2 };

/** 三步流程组件：步骤条 + 当前步骤的表单。 */
export function EmailCodeFlow(props: EmailCodeFlowProps) {
  const [flow, setFlow] = useState<FlowState>({ step: "email", email: props.initialEmail ?? "" });

  /** 第 1 步提交：申请验证码，成功后进入第 2 步。 */
  async function handleEmail(email: string) {
    const result = await props.requestCode(email);
    setFlow({ step: "code", email, challengeId: result.challenge_id, notice: result.message });
  }

  /** 第 2 步“重新发送”：后端作废旧验证码并返回新的 challenge_id，换上新的编号。 */
  async function handleResend() {
    if (flow.step !== "code") {
      return;
    }
    const result = await props.requestCode(flow.email);
    setFlow({ ...flow, challengeId: result.challenge_id, notice: result.message });
  }

  /** 第 2 步提交：核对验证码，成功后拿着凭证进入第 3 步。 */
  async function handleVerify(code: string) {
    if (flow.step !== "code") {
      return;
    }
    const token = await props.verifyCode(flow.challengeId, code);
    setFlow({ step: "password", email: flow.email, token });
  }

  /** 第 3 步提交：完成流程，成功后交给页面决定去哪（通常是登录页）。 */
  async function handleComplete(password: string) {
    if (flow.step !== "password") {
      return;
    }
    await props.complete(flow.token, password);
    props.onCompleted(flow.email);
  }

  /** 回到第 1 步（“修改邮箱”或流程失效后“重新开始”），保留已填的邮箱。 */
  function backToEmail() {
    setFlow({ step: "email", email: flow.email });
  }

  return (
    <>
      <Steps labels={[...props.stepLabels]} current={STEP_INDEX[flow.step]} ariaLabel={props.stepsAriaLabel} />

      {flow.step === "email" && (
        <EmailStep
          initialEmail={flow.email}
          hint={props.emailHint}
          submitLabel={props.emailSubmitLabel}
          onSubmit={handleEmail}
        />
      )}
      {flow.step === "code" && (
        <CodeStep
          email={flow.email}
          notice={flow.notice}
          onVerify={handleVerify}
          onResend={handleResend}
          onChangeEmail={backToEmail}
        />
      )}
      {flow.step === "password" && (
        <NewPasswordStep
          email={flow.email}
          passwordLabel={props.passwordLabel}
          submitLabel={props.passwordSubmitLabel}
          restartCode={props.restartCode}
          onSubmit={handleComplete}
          onRestart={backToEmail}
        />
      )}
    </>
  );
}
