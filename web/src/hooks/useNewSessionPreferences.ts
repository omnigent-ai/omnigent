import { useEffect, useState, useSyncExternalStore } from "react";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { getOmnigentServerIdentity } from "@/lib/host";
import { getCurrentUserId, subscribeIdentity } from "@/lib/identity";
import {
  hostPreferenceAction,
  readSharedPreferences,
  type PreferenceDefaults,
  type PreferenceLaunch,
} from "@/lib/sharedPreferences";

interface PreparedPreferences {
  launch: PreferenceLaunch;
  defaults: PreferenceDefaults;
}

export async function prepareNewSessionPreferences(
  hostId: string | null,
  workspace: string,
  signal: AbortSignal,
): Promise<PreparedPreferences> {
  if (hostId !== null) {
    const result = await hostPreferenceAction(hostId, { action: "prepare", workspace }, signal);
    if (!result.launch || !result.defaults)
      throw new Error("Update this host and retry loading preferences.");
    return { launch: result.launch, defaults: result.defaults };
  }
  const shared = await readSharedPreferences(signal);
  if (shared.revision === 0)
    throw new Error("Choose shared defaults once in Settings, or use temporary defaults.");
  const defaults: PreferenceDefaults = {};
  for (const entry of shared.entries) {
    if (typeof entry.value !== "string") continue;
    if (entry.key.kind === "harness" && entry.key.name === "default")
      defaults.default_harness = entry.value;
    if (
      entry.key.kind === "model" &&
      (entry.key.name === "model" || entry.key.name === "opencode_model")
    )
      defaults[entry.key.name] = entry.value;
  }
  return {
    launch: {
      protocol_version: 1,
      shared,
      installation_id: "managed",
      machine: [],
      state: "current",
      warning: shared.conflicts.length
        ? "Competing preference edits need review in Settings."
        : null,
    },
    defaults,
  };
}

/** One preparation per account, target and explicit retry; never refresh a running session. */
export function useNewSessionPreferences(
  hostId: string | null,
  sandbox: boolean,
  workspace: string,
) {
  const info = useServerInfo();
  const user = useSyncExternalStore(subscribeIdentity, getCurrentUserId);
  const server = getOmnigentServerIdentity();
  const enabled = info !== "loading" && info.shared_preferences === true;
  const key = JSON.stringify([server, user, sandbox ? null : hostId, sandbox ? "" : workspace]);
  const [attempt, setAttempt] = useState(0);
  const [temporaryKey, setTemporaryKey] = useState<string | null>(null);
  const [state, setState] = useState<{
    key: string;
    prepared?: PreparedPreferences;
    error?: string;
  }>();
  const temporary = temporaryKey === key;
  useEffect(() => {
    if (!enabled || temporary || (!sandbox && !hostId)) return;
    const controller = new AbortController();
    let cancelled = false;
    setState({ key });
    // Workspace typing must not start an action on every keystroke.
    const debounce = setTimeout(() => {
      if (server === null) {
        setState({
          key,
          error: "This client needs a stable workspace identity to load preferences.",
        });
        return;
      }
      void prepareNewSessionPreferences(sandbox ? null : hostId, workspace, controller.signal)
        .then((prepared) => {
          if (!cancelled) setState({ key, prepared });
        })
        .catch((error: unknown) => {
          if (!cancelled)
            setState({
              key,
              error: error instanceof Error ? error.message : "Preferences could not load.",
            });
        });
    }, 250);
    const deadline = setTimeout(() => {
      cancelled = true;
      controller.abort();
      setState((current) =>
        current?.key === key && (current.prepared || current.error)
          ? current
          : {
              key,
              error:
                "Preferences could not load in time. Retry or explicitly use temporary defaults.",
            },
      );
    }, 5_000);
    return () => {
      cancelled = true;
      clearTimeout(debounce);
      clearTimeout(deadline);
      controller.abort();
    };
  }, [enabled, key, attempt, temporary, sandbox, hostId, workspace, server]);
  const current = state?.key === key ? state : undefined;
  const prepared = enabled && !temporary ? current?.prepared : undefined;
  return {
    enabled,
    temporary,
    blocked: enabled && !temporary && !prepared,
    error: enabled && !temporary ? current?.error : undefined,
    launch: prepared?.launch,
    defaults: prepared?.defaults,
    retry: () => {
      setTemporaryKey(null);
      setAttempt((value) => value + 1);
    },
    useTemporary: () => setTemporaryKey(key),
  };
}
