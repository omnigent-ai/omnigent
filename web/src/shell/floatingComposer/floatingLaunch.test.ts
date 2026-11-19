import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as SessionsApi from "@/lib/sessionsApi";

const bridge = vi.hoisted(() => ({
  openInMainWindow: vi.fn(),
  dismiss: vi.fn(),
  filePicker: vi.fn(),
  announceSupport: vi.fn(),
}));
vi.mock("@/lib/nativeBridge", () => ({ floatingComposerBridge: () => bridge }));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  postEvent: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
}));
vi.mock("@/lib/userInputDelivery", () => ({ sendVerdictFromServer: vi.fn() }));
vi.mock("@/store/chatStore", () => ({ uploadFileBlocks: vi.fn() }));

import { ApiError, fetchSessionItemsPage, postEvent } from "@/lib/sessionsApi";
import { sendVerdictFromServer } from "@/lib/userInputDelivery";
import { uploadFileBlocks } from "@/store/chatStore";
import type { FloatingLaunchRequest } from "@/shell/composerPresentation";
import {
  launchFromPanel,
  OPEN_FAILED_ERROR,
  SEND_FAILED_ERROR,
  SEND_UNKNOWN_ERROR,
} from "./floatingLaunch";

const request = (overrides: Partial<FloatingLaunchRequest> = {}): FloatingLaunchRequest => ({
  sessionId: "conv_1",
  text: "Fix the build",
  skill: null,
  inputAccepted: false,
  ...overrides,
});

/** The stable id the launch posted its message under. */
const postedStableId = () =>
  (vi.mocked(postEvent).mock.calls[0]![1].data as { stable_id: string }).stable_id;

beforeEach(() => {
  vi.clearAllMocks();
  bridge.openInMainWindow.mockResolvedValue({ ok: true });
  vi.mocked(uploadFileBlocks).mockResolvedValue([]);
  vi.mocked(postEvent).mockResolvedValue({ queued: false } as Awaited<
    ReturnType<typeof postEvent>
  >);
});

describe("launchFromPanel", () => {
  it("opens the session in a main window, then posts the message once with its files", async () => {
    const file = new File(["x"], "notes.txt", { type: "text/plain" });
    vi.mocked(uploadFileBlocks).mockResolvedValue([
      { type: "input_file", file_id: "file_1", filename: "notes.txt" },
    ]);

    const outcome = await launchFromPanel(request({ files: [file] }));

    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1");
    expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [file]);
    expect(postEvent).toHaveBeenCalledTimes(1);
    expect(postEvent).toHaveBeenCalledWith(
      "conv_1",
      {
        type: "message",
        data: {
          role: "user",
          content: [
            { type: "input_file", file_id: "file_1", filename: "notes.txt" },
            { type: "input_text", text: "Fix the build" },
          ],
          stable_id: postedStableId(),
        },
      },
      { holdWhenHostOffline: true },
    );
    expect(outcome).toEqual({ error: null, freshComposer: true });
  });

  it("posts nothing when the create already carried the message", async () => {
    const outcome = await launchFromPanel(request({ inputAccepted: true }));

    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1");
    expect(postEvent).not.toHaveBeenCalled();
    expect(outcome).toEqual({ error: null, freshComposer: true });
  });

  it("sends a bundled skill as a slash command", async () => {
    await launchFromPanel(request({ text: "/review 12", skill: { name: "review", args: "12" } }));

    expect(postEvent).toHaveBeenCalledWith("conv_1", {
      type: "slash_command",
      data: { kind: "skill", name: "review", arguments: "12", stable_id: postedStableId() },
    });
  });

  it("keeps the prompt when the server refuses the message", async () => {
    vi.mocked(postEvent).mockRejectedValue(new ApiError("Host offline", 409, "host_offline"));

    const outcome = await launchFromPanel(request());

    expect(fetchSessionItemsPage).not.toHaveBeenCalled();
    expect(outcome).toEqual({ error: SEND_FAILED_ERROR, freshComposer: false });
  });

  it("keeps the prompt when a policy denies the message", async () => {
    vi.mocked(postEvent).mockResolvedValue({ queued: false, denied: true } as Awaited<
      ReturnType<typeof postEvent>
    >);

    expect(await launchFromPanel(request())).toEqual({
      error: SEND_FAILED_ERROR,
      freshComposer: false,
    });
  });

  describe("with no answer to the post, asks the server", () => {
    beforeEach(() => {
      vi.mocked(postEvent).mockRejectedValue(new TypeError("Failed to fetch"));
    });

    const withItems = (ids: (stableId: string) => string[]) =>
      vi.mocked(fetchSessionItemsPage).mockImplementation(async () => ({
        items: ids(postedStableId()).map((id) => ({ id }) as never),
        hasMore: false,
      }));

    it("and starts fresh once the stored message wasn't refused", async () => {
      withItems((stableId) => [stableId]);
      vi.mocked(sendVerdictFromServer).mockResolvedValue("delivered");

      expect(await launchFromPanel(request())).toEqual({ error: null, freshComposer: true });
      expect(sendVerdictFromServer).toHaveBeenCalledWith("conv_1", postedStableId());
    });

    it("and keeps the prompt when the runner refused it", async () => {
      withItems((stableId) => [stableId]);
      vi.mocked(sendVerdictFromServer).mockResolvedValue("refused");

      expect(await launchFromPanel(request())).toEqual({
        error: SEND_FAILED_ERROR,
        freshComposer: false,
      });
    });

    it("and keeps the prompt when the message was never stored", async () => {
      withItems(() => ["other"]);

      expect(await launchFromPanel(request())).toEqual({
        error: SEND_FAILED_ERROR,
        freshComposer: false,
      });
      expect(sendVerdictFromServer).not.toHaveBeenCalled();
    });

    it("and says it couldn't tell when the server can't be read either", async () => {
      vi.mocked(fetchSessionItemsPage).mockRejectedValue(new TypeError("Failed to fetch"));

      expect(await launchFromPanel(request())).toEqual({
        error: SEND_UNKNOWN_ERROR,
        freshComposer: false,
      });
    });
  });

  it("still sends the message when the session couldn't be shown", async () => {
    bridge.openInMainWindow.mockResolvedValue({ ok: false, error: "window-loading" });

    const outcome = await launchFromPanel(request());

    expect(postEvent).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({ error: OPEN_FAILED_ERROR, freshComposer: true });
  });
});
