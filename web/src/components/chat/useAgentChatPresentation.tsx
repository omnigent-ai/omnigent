import { useMemo } from "react";
import { ComposerAgentIcon } from "@/components/ComposerAgentIcon";
import type { Agent } from "@/hooks/useAgents";
import { sessionAgentIdentity } from "@/lib/sessionAgentIdentity";
import { AgentChatIdentity } from "./AgentChatIdentity";
import type { ChatPresentation } from "./assistantPresentation";

/** The same bound-agent identity in project Chat and the full session transcript. */
export function useAgentChatPresentation(
  agent: Pick<Agent, "name" | "harness" | "description"> | undefined,
  persona: string | null = null,
): ChatPresentation {
  return useMemo<ChatPresentation>(() => {
    const identity = agent
      ? sessionAgentIdentity({ agentName: agent.name, harness: agent.harness })
      : null;
    if (!agent || !identity) return {};
    return {
      header: () => (
        <div className="flex shrink-0 justify-center">
          <AgentChatIdentity agent={agent} persona={persona} compact />
        </div>
      ),
      assistant: {
        name: identity.displayName,
        icon: <ComposerAgentIcon agent={identity} />,
      },
    };
  }, [agent, persona]);
}
