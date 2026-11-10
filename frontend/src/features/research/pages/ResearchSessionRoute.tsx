/** 研究路由容器：账户/会话隔离、可信草稿来源筛选及异步服务适配。 */
import { useEffect, useRef } from "react";
import { useLocation, useNavigate, useParams } from "react-router";
import { useAuth } from "../../auth/session/AuthContext";
import type { ResearchSessionService, SessionActions, SessionDraft } from "../contracts/session";
import { useResearchSession } from "../hooks/useResearchSession";
import { ResearchSessionPage } from "./ResearchSessionPage";
/** history state 是显示草稿，不是授权凭据；换账户后不显示旧账户的输入。 */
function readDraft(value: unknown, owner: string): SessionDraft | undefined {
  if (!value || typeof value !== "object" || !("owner" in value) || value.owner !== owner || !("draft" in value)) return undefined;
  const draft = value.draft;
  if (!draft || typeof draft !== "object" || !("question" in draft) || typeof draft.question !== "string" || !("contexts" in draft) || !Array.isArray(draft.contexts)) return undefined;
  const objects = draft.contexts.filter((item: unknown): item is SessionDraft["objects"][number] => !!item && typeof item === "object" && "chainId" in item && Number.isSafeInteger(item.chainId) && "address" in item && typeof item.address === "string" && "symbol" in item && typeof item.symbol === "string");
  return { question: draft.question, objects, time: null };
}
/** RequireAuth 提供页面保护；key 在切换账户、会话或新草稿时重建所有本地状态。 */
export function ResearchSessionRoute({ service = null }: { service?: ResearchSessionService | null }) {
  const { state } = useAuth();
  const location = useLocation();
  const { sessionId } = useParams();
  if (state.status !== "authenticated") return null;
  const id = sessionId ?? null;
  return <SessionController key={`${state.account.user_id}:${id ?? location.key}`} id={id} service={service} initialDraft={readDraft(location.state, state.account.user_id)} />;
}
/** 适配器未连接时仅展示草稿，不调用虚构接口或创建本地伪会话。 */
function SessionController({ id, service, initialDraft }: { id: string | null; service: ResearchSessionService | null; initialDraft?: SessionDraft }) {
  const navigate = useNavigate();
  const { state, disconnected, accept } = useResearchSession(id, service);
  const active = useRef(true);
  // 换账户或离开会话后，晚到的发送结果不能触发导航或覆盖新页面。
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  const snapshot = state.snapshot;
  const actions: SessionActions | null = service ? {
    send: async (draft) => {
      const result = await service.send({ sessionId: id, requestRevision: snapshot?.requestRevision ?? null, text: draft.question, objects: draft.objects, time: draft.time });
      if (!active.current) throw new Error("obsolete request");
      if (id && result.id !== id) throw new Error("session mismatch");
      accept(result);
      if (!id) navigate(`/research/${encodeURIComponent(result.id)}`, { replace: true });
    },
    clarify: async (request, time) => {
      if (!id || request.requestRevision !== snapshot?.requestRevision) throw new Error("stale clarification");
      await service.clarify({ sessionId: id, clarificationId: request.id, requestRevision: request.requestRevision, time });
    },
    cancel: async () => { if (!id || !snapshot) throw new Error("missing session"); await service.cancel({ sessionId: id, requestRevision: snapshot.requestRevision }); },
  } : null;
  return <ResearchSessionPage state={state} disconnected={disconnected} initialDraft={initialDraft} actions={actions} onNew={() => navigate("/research/new")} />;
}
