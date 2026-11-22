import { capitalizeAgentName } from "@/lib/agentLabels";
import { agentRootName } from "@/lib/forkHarness";
import {
  nativeCodingAgentForAgentName,
  nativeCodingAgentForHarness,
  nativeCodingAgentForSession,
  nativeCodingAgentForSubagentWrapper,
  WRAPPER_LABEL_KEY,
} from "@/lib/nativeCodingAgents";

export interface SessionAgentIdentity {
  name: string;
  displayName: string;
  harness: string | null;
}

/** Resolve the bound agent, never a session title, tool alias or model name. */
export function sessionAgentIdentity(session: {
  agentName?: string | null;
  harness?: string | null;
  labels?: Record<string, string>;
}): SessionAgentIdentity | null {
  const name = agentRootName(session.agentName ?? "").trim();
  if (name) {
    const native = nativeCodingAgentForAgentName(name) ?? nativeCodingAgentForHarness(name);
    return {
      name: native?.agentName ?? name,
      displayName: native?.displayName ?? capitalizeAgentName(name),
      harness: session.harness ?? native?.harness ?? null,
    };
  }
  const native =
    nativeCodingAgentForSession(session) ??
    nativeCodingAgentForSubagentWrapper(session.labels?.[WRAPPER_LABEL_KEY]);
  return native
    ? { name: native.agentName, displayName: native.displayName, harness: native.harness }
    : null;
}
