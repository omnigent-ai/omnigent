import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getOmnigentHostGeneration, getOmnigentServerIdentity } from "@/lib/host";
import { authenticatedFetch } from "@/lib/identity";
import { useViewerId } from "./useViewerId";

/** The caller's view of ``GET /v1/proactive-agent/settings``. */
export interface ProactiveAgentSettings {
  /** The admin's workspace-wide setting. */
  workspace_enabled: boolean;
  /** The caller's own setting. */
  user_enabled: boolean;
  /** Whether the agent runs for the caller: both settings on. */
  enabled: boolean;
  /** Whether the caller may change the workspace setting. */
  can_manage_workspace: boolean;
}

/** Which setting a change targets: the caller's own, or the workspace's. */
export type ProactiveAgentSettingScope = "me" | "workspace";

async function readJson(res: Response): Promise<ProactiveAgentSettings> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as ProactiveAgentSettings;
}

function useSettingsKey() {
  const viewerId = useViewerId();
  return [
    "proactive-agent-settings",
    getOmnigentServerIdentity() ?? `mount:${getOmnigentHostGeneration()}`,
    viewerId,
  ];
}

/**
 * The proactive agent's settings, when the server offers the agent; ``undefined``
 * while loading or where it is not offered, so callers treat both as off.
 */
export function useProactiveAgentSettings() {
  const info = useServerInfo();
  const available = info !== "loading" && info.proactive_agent_available === true;
  const queryKey = useSettingsKey();
  const query = useQuery({
    queryKey,
    enabled: available,
    staleTime: 60_000,
    queryFn: async () => readJson(await authenticatedFetch("/v1/proactive-agent/settings")),
  });
  return { available, settings: available ? query.data : undefined, query };
}

/** Change the caller's own setting or, for an admin, the workspace's. */
export function useSetProactiveAgentSetting() {
  const queryClient = useQueryClient();
  const queryKey = useSettingsKey();
  return useMutation({
    mutationFn: async ({
      scope,
      enabled,
    }: {
      scope: ProactiveAgentSettingScope;
      enabled: boolean;
    }) =>
      readJson(
        await authenticatedFetch(`/v1/proactive-agent/settings/${scope}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ enabled }),
        }),
      ),
    // A settings GET in flight when the switch is flipped may carry the old
    // value and land after the PUT; drop it before and after the request so
    // the accepted setting is what the cache keeps.
    onMutate: () => queryClient.cancelQueries({ queryKey }),
    onSuccess: async (data) => {
      await queryClient.cancelQueries({ queryKey });
      queryClient.setQueryData(queryKey, data);
      // Turning it off hides the suggestions; turning it on shows them again.
      void queryClient.invalidateQueries({ queryKey: ["proactive-inbox"] });
    },
  });
}

/** ``GET /v1/proactive-agent/prompt`` (admins). */
export interface ProactiveAgentPrompt {
  /** The admin's override, or the bundled prompt without one. */
  prompt: string;
  /** The bundled prompt, which a release can change. */
  default_prompt: string;
  overridden: boolean;
}

/** Result of ``POST /v1/proactive-agent/runs``. */
export interface ProactiveAgentRuns {
  started: { user_id: string; task_id: string; run_id: string | null }[];
  skipped: {
    user_id: string;
    reason: "no_task" | "turned_off" | "already_running" | "unavailable";
  }[];
  truncated: boolean;
}

async function readBody<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body?.error?.message ?? `${res.status} ${res.statusText}`);
  }
  return (await res.json()) as T;
}

/** The agent's prompt, for an admin who manages the workspace setting. */
export function useProactiveAgentPrompt(enabled: boolean) {
  const queryKey = [...useSettingsKey(), "prompt"];
  const queryClient = useQueryClient();
  const query = useQuery({
    queryKey,
    enabled,
    staleTime: 0,
    queryFn: async () =>
      readBody<ProactiveAgentPrompt>(await authenticatedFetch("/v1/proactive-agent/prompt")),
  });
  const save = useMutation({
    /** ``null`` restores the bundled prompt. */
    mutationFn: async (prompt: string | null) =>
      readBody<ProactiveAgentPrompt>(
        await authenticatedFetch("/v1/proactive-agent/prompt", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ prompt }),
        }),
      ),
    onSuccess: (data) => queryClient.setQueryData(queryKey, data),
  });
  return { query, save };
}

/** Start users' proactive runs now: user ids, or ``["*"]`` for everyone with a task. */
export function useRunProactiveAgent() {
  return useMutation({
    mutationFn: async (users: string[]) =>
      readBody<ProactiveAgentRuns>(
        await authenticatedFetch("/v1/proactive-agent/runs", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ users }),
        }),
      ),
  });
}
