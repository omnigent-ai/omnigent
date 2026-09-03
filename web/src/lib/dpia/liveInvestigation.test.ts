import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { bindOnlyOnlineRunner, getSession, postEvent } from "@/lib/sessionsApi";
import type { SessionEventInput } from "@/lib/types";
import { createStudentSuccessAlertSeed } from "./seed";
import { runLiveInvestigation } from "./liveInvestigation";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({
  bindOnlyOnlineRunner: vi.fn(),
  getSession: vi.fn(),
  postEvent: vi.fn(),
}));

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe("live DPIA investigation adapter", () => {
  it("submits the complete current model to the labelled root and waits for completion", async () => {
    vi.useFakeTimers();
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [
            {
              id: "conv_dpia",
              runner_id: "runner_dpia",
              labels: {
                "omnigent.product": "dpia-investigation",
                "omnigent.case_id": "student-success-alert",
              },
            },
          ],
        }),
        { status: 200, headers: { "Content-Type": "application/json" } },
      ),
    );
    vi.mocked(bindOnlyOnlineRunner).mockResolvedValue(null);
    vi.mocked(postEvent).mockResolvedValue({ queued: true });
    vi.mocked(getSession)
      .mockResolvedValueOnce({ status: "running" } as Awaited<ReturnType<typeof getSession>>)
      .mockResolvedValueOnce({ status: "idle" } as Awaited<ReturnType<typeof getSession>>);
    const caseData = createStudentSuccessAlertSeed();
    caseData.processingModel.version = 4;
    caseData.processingModel.facts[0].value = "Current edited purpose";
    const progress: string[] = [];

    const resultPromise = runLiveInvestigation(caseData, (message) => progress.push(message));
    await vi.runAllTimersAsync();
    const result = await resultPromise;

    expect(result.sessionId).toBe("conv_dpia");
    expect(postEvent).toHaveBeenCalledWith(
      "conv_dpia",
      expect.objectContaining({ type: "message" }),
    );
    const event = vi.mocked(postEvent).mock.calls[0][1];
    expect(event.type).toBe("message");
    if (event.type !== "message") throw new Error("Expected a message event");
    const messageEvent = event as Extract<SessionEventInput, { type: "message" }>;
    const inputText = messageEvent.data.content.find((block) => block.type === "input_text");
    expect(inputText?.type === "input_text" ? inputText.text : "").toContain(
      '"processing_model_version":4',
    );
    expect(inputText?.type === "input_text" ? inputText.text : "").toContain('"id":"EV-01"');
    expect(inputText?.type === "input_text" ? inputText.text : "").toContain(
      "Current edited purpose",
    );
    expect(progress).toContain("Professional-role sessions are working");
  });
});
