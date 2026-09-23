import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BrowserDiagnostics, diagnosticBlockedReason } from "./diagnostics";

describe("browser stall diagnostics", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("bounds the queue and batch and reports sequence gaps and dropped observations", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const diagnostics = new BrowserDiagnostics(send);
    for (let n = 0; n < 105; n++) {
      diagnostics.record("session", {
        event_name: "browser_approval_received",
        elicitation_id: `e${n}`,
      });
    }
    await diagnostics.flush();
    const [session, batch] = send.mock.calls[0]!;
    expect(session).toBe("session");
    expect(batch.events).toHaveLength(20);
    expect(batch.dropped_events).toBe(5);
    expect(batch.events[0].sequence).toBe(6);
    expect(batch.events.at(-1).sequence).toBe(25);
    expect(batch.client_instance_id).toMatch(/^[a-f0-9-]{36}$/);
  });

  it("deduplicates React effects but preserves repark receipts and changed visibility", async () => {
    const send = vi.fn().mockResolvedValue(undefined);
    const diagnostics = new BrowserDiagnostics(send);
    const rendered = {
      event_name: "browser_approval_rendered",
      elicitation_id: "e1",
      actionable: true,
    } as const;
    diagnostics.record("s1", rendered);
    diagnostics.record("s1", rendered);
    diagnostics.record("s1", { ...rendered, actionable: false });
    diagnostics.record("s1", { event_name: "browser_approval_received", elicitation_id: "e1" });
    diagnostics.record("s1", { event_name: "browser_approval_received", elicitation_id: "e1" });
    await diagnostics.flush();
    expect(send.mock.calls[0]![1].events).toHaveLength(4);
  });

  it("keeps sessions separate and reports failed batches without retrying them", async () => {
    const send = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue(undefined);
    const diagnostics = new BrowserDiagnostics(send);
    diagnostics.record("s1", {
      event_name: "browser_approval_verdict_submitted",
      action: "accept",
    });
    diagnostics.record("s2", { event_name: "browser_status_received", status: "idle" });
    await expect(diagnostics.flush()).resolves.toBeUndefined();
    await diagnostics.flush();
    expect(send.mock.calls.map(([session]) => session)).toEqual(["s1", "s2"]);
    expect(send.mock.calls[1]![1].dropped_events).toBe(1);
    expect(send.mock.calls[1]![1].events[0].sequence).toBe(2);
  });

  it("does not wait for transport to enqueue a verdict and serializes flushes", async () => {
    let finish!: () => void;
    const send = vi.fn().mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const diagnostics = new BrowserDiagnostics(send);
    diagnostics.record("s1", { event_name: "browser_approval_received" });
    const pending = diagnostics.flush();
    diagnostics.record("s1", {
      event_name: "browser_approval_verdict_submitted",
      action: "accept",
    });
    await diagnostics.flush();
    expect(send).toHaveBeenCalledTimes(1);
    finish();
    await pending;
  });

  it("classifies native reasons without forwarding arbitrary text", () => {
    expect(diagnosticBlockedReason("dialog open")).toBe("dialog_open");
    expect(diagnosticBlockedReason("permission prompt")).toBe("permission_prompt");
    expect(diagnosticBlockedReason("private command contents")).toBe("other");
    expect(diagnosticBlockedReason(null)).toBe("none");
    expect(diagnosticBlockedReason(undefined)).toBe("unknown");
  });
});
