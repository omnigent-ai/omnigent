/** UI-facing terminal record returned by the session resources API. */
export interface TerminalInfo {
  /** Opaque, stable resource id used for addressing and tab keys. */
  id: string;
  /** Terminal name from the spec, e.g. ``"bash"``. */
  name: string;
  /** Session key, e.g. ``"s1"``. */
  session: string;
  /** Whether the underlying tmux session is currently running. */
  running: boolean;
  /** Web-attach transport, when supplied by the server. */
  transport?: "control" | "pty";
}

/** TanStack Query key for a conversation's terminal resources. */
export function terminalsQueryKey(conversationId: string): readonly unknown[] {
  return ["conversation", conversationId, "terminals"];
}

/** Convert a terminal resource wire payload into the UI-facing record. */
export function terminalInfoFromResource(resource: Record<string, unknown>): TerminalInfo | null {
  const id = resource.id;
  if (typeof id !== "string" || !id) return null;
  const rawMetadata = resource.metadata;
  const metadata =
    rawMetadata && typeof rawMetadata === "object" && !Array.isArray(rawMetadata)
      ? (rawMetadata as Record<string, unknown>)
      : {};
  const terminalName = metadata.terminal_name;
  const sessionKey = metadata.session_key;
  const running = metadata.running;
  const rawTransport = metadata.terminal_transport;
  const transport = rawTransport === "control" || rawTransport === "pty" ? rawTransport : undefined;
  const fallbackName = resource.name;
  return {
    id,
    name:
      typeof terminalName === "string" && terminalName
        ? terminalName
        : typeof fallbackName === "string"
          ? fallbackName
          : "",
    session: typeof sessionKey === "string" ? sessionKey : "",
    running: typeof running === "boolean" ? running : false,
    transport,
  };
}
