import { authenticatedFetch } from "./identity";
import { randomUUID } from "./randomUUID";

export type DiagnosticStatus = "idle" | "launching" | "running" | "waiting" | "failed" | "unknown";
type BlockedReason = "none" | "permission_prompt" | "dialog_open" | "other" | "unknown";
export interface BrowserDiagnostic {
  event_name:
    | "browser_approval_received"
    | "browser_approval_applied"
    | "browser_approval_rendered"
    | "browser_approval_visibility"
    | "browser_approval_verdict_submitted"
    | "browser_approval_verdict_request_completed"
    | "browser_status_received"
    | "browser_status_applied"
    | "browser_status_displayed"
    | "browser_status_reconnect";
  target_session_id?: string;
  elicitation_id?: string;
  card_instance_id?: string;
  status?: DiagnosticStatus;
  previous_status?: DiagnosticStatus;
  blocked_on?: BlockedReason;
  previous_blocked_on?: BlockedReason;
  snapshot_blocked_on?: BlockedReason;
  actionable?: boolean;
  in_view?: boolean;
  tab_visible?: boolean;
  status_indicator_visible?: boolean;
  action?: "accept" | "decline" | "cancel";
  outcome?: "success" | "error";
  http_status?: number;
  observation_source?: "stream" | "snapshot" | "card";
  observation_trigger?: "periodic_reconcile" | "stream_reconnect";
}

type QueuedEvent = BrowserDiagnostic & { sequence: number; client_time_ms: number };
interface DiagnosticBatch {
  client_instance_id: string;
  client_bundle?: string;
  /** Cumulative losses across this browser instance, including its other sessions. */
  dropped_events: number;
  events: QueuedEvent[];
}
type Transport = (sessionId: string, batch: DiagnosticBatch) => Promise<void>;

/** Convert potentially free-form native status into an allowlisted classification. */
export function diagnosticBlockedReason(value: string | null | undefined): BlockedReason {
  if (value === undefined) return "unknown";
  if (value === null || value === "") return "none";
  if (value === "permission prompt") return "permission_prompt";
  if (value === "dialog open") return "dialog_open";
  return "other";
}

/** Lossy, bounded telemetry: transport failure never gates a user action. */
export class BrowserDiagnostics {
  private readonly clientInstanceId = randomUUID();
  private readonly clientBundle = new URL(import.meta.url).pathname.split("/").pop();
  private sequence = 0;
  private dropped = 0;
  private queue: { sessionId: string; event: QueuedEvent }[] = [];
  private readonly recent = new Map<string, { value: string; time: number }>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private sending = false;
  private readonly transport: Transport;

  constructor(transport: Transport) {
    this.transport = transport;
  }

  record(sessionId: string | null | undefined, event: BrowserDiagnostic): void {
    if (!sessionId) return;
    const now = Date.now();
    // Deduplicate render effects and repeated state snapshots, not lifecycle edges.
    if (
      event.event_name === "browser_approval_rendered" ||
      event.event_name === "browser_approval_visibility" ||
      event.event_name === "browser_status_applied" ||
      event.event_name === "browser_status_displayed" ||
      event.event_name === "browser_status_reconnect"
    ) {
      const key = `${sessionId}:${event.event_name}:${event.elicitation_id ?? ""}:${event.card_instance_id ?? ""}`;
      const value = JSON.stringify(event);
      const previous = this.recent.get(key);
      if (previous?.value === value && now - previous.time < 60_000) return;
      this.recent.delete(key);
      this.recent.set(key, { value, time: now });
      if (this.recent.size > 200) this.recent.delete(this.recent.keys().next().value!);
    }
    const queued = { ...event, sequence: ++this.sequence, client_time_ms: now };
    if (this.queue.length === 100) {
      this.queue.shift();
      this.dropped = Math.min(this.dropped + 1, 2 ** 31 - 1);
    }
    this.queue.push({ sessionId, event: queued });
    this.schedule();
  }

  private schedule(): void {
    if (this.timer !== null || this.sending || this.queue.length === 0) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      void this.flush();
    }, 1000);
  }

  async flush(): Promise<void> {
    if (this.sending || this.queue.length === 0) return;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.sending = true;
    const sessionId = this.queue[0]!.sessionId;
    const events: QueuedEvent[] = [];
    this.queue = this.queue.filter((entry) => {
      if (entry.sessionId !== sessionId || events.length === 20) return true;
      events.push(entry.event);
      return false;
    });
    try {
      await this.transport(sessionId, {
        client_instance_id: this.clientInstanceId,
        ...(this.clientBundle && /^[A-Za-z0-9_.:-]{1,128}$/.test(this.clientBundle)
          ? { client_bundle: this.clientBundle }
          : {}),
        dropped_events: this.dropped,
        events,
      });
    } catch {
      this.dropped = Math.min(this.dropped + events.length, 2 ** 31 - 1);
    } finally {
      this.sending = false;
      this.schedule();
    }
  }
}

let diagnostics: BrowserDiagnostics | undefined;
const transport: Transport = async (sessionId, batch) => {
  const response = await authenticatedFetch(
    `/v1/sessions/${encodeURIComponent(sessionId)}/diagnostics`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(batch),
      signal: AbortSignal.timeout(5000),
    },
  );
  if (!response.ok) throw new Error("Diagnostic batch rejected");
};

export function recordBrowserDiagnostic(
  sessionId: string | null | undefined,
  event: BrowserDiagnostic,
): void {
  // Diagnostics must not prevent a verdict or stream state update, even in old webviews.
  try {
    diagnostics ??= new BrowserDiagnostics(transport);
    diagnostics.record(sessionId, event);
  } catch {
    // Best effort only; missing observations are not proof of absence.
  }
}
