/** 首页与研究功能的组装层：只通过路由携带用户自己的未发送草稿，不触发分析。 */
import { useNavigate } from "react-router";
import { useAuth } from "../features/auth/session/AuthContext";
import { HomePage } from "../features/workspace/pages/HomePage";
import { upcomingFeatures } from "./navigation";
/** 在 app 层连接两个功能，避免 workspace 与 research 功能相互依赖。 */
export function ResearchHomeRoute() {
  const navigate = useNavigate();
  const { state } = useAuth();
  if (state.status !== "authenticated") return null;
  return <HomePage upcoming={upcomingFeatures()} execution={{ status: "available", submit: async (draft) => { navigate("/research/new", { state: { owner: state.account.user_id, draft } }); } }} submitLabel="进入研究会话" executionHint="先进入会话整理问题；分析服务尚未接入。" />;
}
