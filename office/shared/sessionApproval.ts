// "Approve for this session": which Omnigent approval requests may be answered
// automatically, and the harness's own session-wide grant when it offers one.

export interface AutoApproveRule {
  sessionId: string;
  projectId: string | null;
  /** Epoch seconds the PO turned it on. */
  since: number;
  approvedCount: number;
  lastApprovedAt: number | null;
  lastMessage: string | null;
}

/** The harness-native grant to send with an approval so the harness itself stops asking. */
export interface NativeSessionGrant {
  content?: Record<string, unknown>;
  meta?: Record<string, unknown>;
  /** What the harness will stop asking about, for the PO. */
  label: string;
}

type Params = Record<string, unknown> | undefined;

function hasFields(schema: unknown): boolean {
  if (!schema || typeof schema !== "object") return false;
  const properties = (schema as Record<string, unknown>).properties;
  return Boolean(properties && typeof properties === "object" && Object.keys(properties).length > 0);
}

/**
 * Only a plain yes/no approval may be answered automatically. Questions, forms,
 * plan reviews, and sign-in links need the PO's own answer.
 */
export function isPlainApproval(params: Params): boolean {
  if (!params) return true;
  if (params.ask_user_question || params.exit_plan_mode) return false;
  if (params.mode === "url" || typeof params.url === "string") return false;
  return !hasFields(params.requestedSchema);
}

/** Mirrors the session-scoped options Omnigent's own approval card offers. */
export function nativeSessionGrant(params: Params): NativeSessionGrant | null {
  if (!params) return null;
  const remember = params.remember_scope as { tool?: unknown; host?: unknown } | undefined;
  if (remember && typeof remember.tool === "string") {
    const target = typeof remember.host === "string" ? remember.host : remember.tool;
    return { content: { remember: true }, label: `${target} requests` };
  }
  const meta = params._meta as Record<string, unknown> | undefined;
  const persist = meta?.codex_approval_kind === "mcp_tool_call" ? meta.persist : null;
  if (persist === "session" || (Array.isArray(persist) && persist.includes("session"))) {
    return { meta: { persist: "session" }, label: "this tool" };
  }
  if (params.phase === "agy_permission" && typeof params.always_allow_pattern === "string" && params.always_allow_pattern) {
    return { meta: { persist: "session" }, label: `commands starting with “${params.always_allow_pattern}”` };
  }
  if (Array.isArray(params.permission_directories) && params.permission_directories.length > 0) {
    return { content: { allow_directories: true }, label: "these directories" };
  }
  return null;
}
