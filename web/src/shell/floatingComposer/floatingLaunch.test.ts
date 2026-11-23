import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type * as SessionsApi from "@/lib/sessionsApi";

const bridge = vi.hoisted(() => ({
  openInMainWindow: vi.fn(),
  dismiss: vi.fn(),
  filePicker: vi.fn(),
  announceSupport: vi.fn(),
  canHandOffLaunch: vi.fn(),
  takeLaunch: vi.fn(),
}));
const bridgeOf = vi.hoisted(() => vi.fn());
vi.mock("@/lib/nativeBridge", () => ({ floatingComposerBridge: bridgeOf }));
vi.mock("@/lib/sessionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof SessionsApi>()),
  postEvent: vi.fn(),
  fetchSessionItemsPage: vi.fn(),
}));
vi.mock("@/lib/userInputDelivery", () => ({ sendVerdictFromServer: vi.fn() }));
vi.mock("@/store/chatStore", () => ({
  uploadFileBlocks: vi.fn(),
  setPendingInitialPrompt: vi.fn(),
}));

import { ApiError, fetchSessionItemsPage, postEvent } from "@/lib/sessionsApi";
import { sendVerdictFromServer } from "@/lib/userInputDelivery";
import { setPendingInitialPrompt, uploadFileBlocks } from "@/store/chatStore";
import type { FloatingLaunchRequest } from "@/shell/composerPresentation";
import {
  launchFromPanel,
  OPEN_FAILED_ERROR,
  SEND_FAILED_ERROR,
  SEND_UNKNOWN_ERROR,
  takeLaunchOffer,
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

const delivered = { error: null, freshComposer: true };
const failed = { error: SEND_FAILED_ERROR, freshComposer: false };
const unknown = { error: SEND_UNKNOWN_ERROR, freshComposer: false };

beforeEach(() => {
  vi.clearAllMocks();
  bridgeOf.mockReturnValue(bridge);
  bridge.openInMainWindow.mockResolvedValue({ ok: true });
  bridge.canHandOffLaunch.mockResolvedValue(null);
  bridge.takeLaunch.mockResolvedValue(null);
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

    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
    expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [file]);
    expect(postEvent).toHaveBeenCalledTimes(1);
    // No hold for an offline host: the panel can't resend later.
    expect(postEvent).toHaveBeenCalledWith("conv_1", {
      type: "message",
      data: {
        role: "user",
        content: [
          { type: "input_file", file_id: "file_1", filename: "notes.txt" },
          { type: "input_text", text: "Fix the build" },
        ],
        stable_id: postedStableId(),
      },
    });
    expect(outcome).toEqual(delivered);
  });

  it("posts nothing when the create already carried the message", async () => {
    const outcome = await launchFromPanel(request({ inputAccepted: true }));

    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
    expect(postEvent).not.toHaveBeenCalled();
    expect(outcome).toEqual(delivered);
  });

  it("sends a bundled skill as a slash command", async () => {
    await launchFromPanel(request({ text: "/review 12", skill: { name: "review", args: "12" } }));

    expect(uploadFileBlocks).not.toHaveBeenCalled();
    expect(postEvent).toHaveBeenCalledWith("conv_1", {
      type: "slash_command",
      data: { kind: "skill", name: "review", arguments: "12", stable_id: postedStableId() },
    });
  });

  it("keeps the prompt when the server answers with a refusal", async () => {
    vi.mocked(postEvent).mockRejectedValue(
      new ApiError("The runner didn't come online", 503, "runner_unavailable"),
    );

    expect(await launchFromPanel(request())).toEqual(failed);
    expect(sendVerdictFromServer).not.toHaveBeenCalled();
  });

  it("keeps the prompt when a policy denies the message", async () => {
    vi.mocked(postEvent).mockResolvedValue({ queued: false, denied: true } as Awaited<
      ReturnType<typeof postEvent>
    >);

    expect(await launchFromPanel(request())).toEqual(failed);
  });

  it("keeps the prompt without posting when an attachment can't upload", async () => {
    vi.mocked(uploadFileBlocks).mockRejectedValue(new TypeError("Failed to fetch"));

    expect(await launchFromPanel(request({ files: [new File(["x"], "a.txt")] }))).toEqual(failed);
    expect(postEvent).not.toHaveBeenCalled();
  });

  describe("with no answer to the post, asks the server", () => {
    beforeEach(() => {
      vi.mocked(postEvent).mockRejectedValue(new TypeError("Failed to fetch"));
      vi.mocked(sendVerdictFromServer).mockResolvedValue("delivered");
    });

    const withItems = (ids: (stableId: string) => string[], hasMore = false) =>
      vi.mocked(fetchSessionItemsPage).mockImplementation(async () => ({
        items: ids(postedStableId()).map((id) => ({ id }) as never),
        hasMore,
      }));

    it("and starts fresh once the stored message wasn't refused", async () => {
      withItems((stableId) => [stableId]);

      expect(await launchFromPanel(request())).toEqual(delivered);
      expect(sendVerdictFromServer).toHaveBeenCalledWith("conv_1", postedStableId());
    });

    it("and keeps the prompt when the runner refused it", async () => {
      vi.mocked(sendVerdictFromServer).mockResolvedValue("refused");

      expect(await launchFromPanel(request())).toEqual(failed);
    });

    // A native runner takes the message before its transcript stores it.
    it("and can't tell when history doesn't have the message yet", async () => {
      withItems(() => ["other"]);

      expect(await launchFromPanel(request())).toEqual(unknown);
    });

    it("and can't tell from a history page with more before it", async () => {
      withItems(() => ["other"], true);

      expect(await launchFromPanel(request())).toEqual(unknown);
    });

    it("and can't tell when the server can't be read either", async () => {
      vi.mocked(sendVerdictFromServer).mockResolvedValue(null);

      expect(await launchFromPanel(request())).toEqual(unknown);
      expect(fetchSessionItemsPage).not.toHaveBeenCalled();
    });

    it("and can't tell when history can't be read", async () => {
      vi.mocked(fetchSessionItemsPage).mockRejectedValue(new TypeError("Failed to fetch"));

      expect(await launchFromPanel(request())).toEqual(unknown);
    });
  });

  it("never rejects: a delivery step that throws comes back as unknown", async () => {
    vi.mocked(postEvent).mockRejectedValue(new TypeError("Failed to fetch"));
    vi.mocked(sendVerdictFromServer).mockRejectedValue(new Error("unexpected"));

    await expect(launchFromPanel(request())).resolves.toEqual(unknown);
  });

  it("still sends the message when the shell throws while opening the session", async () => {
    bridge.openInMainWindow.mockImplementation(() => {
      throw new Error("gone");
    });

    expect(await launchFromPanel(request())).toEqual({
      error: OPEN_FAILED_ERROR,
      freshComposer: true,
    });
    expect(postEvent).toHaveBeenCalledTimes(1);
  });

  it("keeps the prompt when neither the open nor the send worked", async () => {
    bridge.openInMainWindow.mockRejectedValue(new Error("gone"));
    vi.mocked(postEvent).mockResolvedValue({ queued: false, denied: true } as Awaited<
      ReturnType<typeof postEvent>
    >);

    // The failure text points to the sidebar, so it holds whether or not a window opened.
    expect(await launchFromPanel(request())).toEqual(failed);
    expect(SEND_FAILED_ERROR).toMatch(/sidebar/);
  });

  it.each([
    [
      "couldn't be shown",
      () => bridge.openInMainWindow.mockResolvedValue({ ok: false, error: "window-loading" }),
    ],
    ["failed to open", () => bridge.openInMainWindow.mockRejectedValue(new Error("gone"))],
    ["has no shell to open it", () => bridgeOf.mockReturnValue(undefined)],
  ])("still sends the message when the session %s", async (_case, arrange) => {
    arrange();

    const outcome = await launchFromPanel(request());

    expect(postEvent).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual({ error: OPEN_FAILED_ERROR, freshComposer: true });
  });
});

describe("launchFromPanel hand-off", () => {
  const cap = 1000;
  const note = () => new File(["notes"], "notes.txt", { type: "text/plain" });

  beforeEach(() => {
    bridge.canHandOffLaunch.mockResolvedValue({ maxFileBytes: cap });
  });

  it("offers the message with its attachments, and posts nothing when the page takes it", async () => {
    bridge.openInMainWindow.mockResolvedValue({ ok: true, delivery: "page" });
    const outcome = await launchFromPanel(request({ files: [note()] }));
    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", {
      text: "Fix the build",
      skill: null,
      files: [{ name: "notes.txt", type: "text/plain", bytes: expect.any(ArrayBuffer) }],
    });
    expect(uploadFileBlocks).not.toHaveBeenCalled();
    expect(postEvent).not.toHaveBeenCalled();
    expect(outcome).toEqual({ error: null, freshComposer: true });
  });

  it("sends the message itself, once, when the window's page doesn't take it", async () => {
    const file = note();
    bridge.openInMainWindow.mockResolvedValue({ ok: true });
    const outcome = await launchFromPanel(request({ files: [file] }));
    expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [file]);
    expect(postEvent).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual(delivered);
  });

  it("reads no attachments over the shell's cap, and the panel sends them", async () => {
    const file = new File(["x".repeat(cap + 1)], "big.txt", { type: "text/plain" });
    const read = vi.spyOn(file, "arrayBuffer");
    bridge.openInMainWindow.mockResolvedValue({ ok: true });
    await launchFromPanel(request({ files: [file] }));
    expect(read).not.toHaveBeenCalled();
    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
    expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [file]);
  });

  it("falls back to the panel with the original files when an attachment can't be read", async () => {
    const file = note();
    vi.spyOn(file, "arrayBuffer").mockRejectedValue(new Error("unreadable"));
    bridge.openInMainWindow.mockResolvedValue({ ok: true });
    const outcome = await launchFromPanel(request({ files: [file] }));
    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
    expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [file]);
    expect(postEvent).toHaveBeenCalledTimes(1);
    expect(outcome).toEqual(delivered);
  });

  it("delivers as before in a shell that can't take hand-offs", async () => {
    bridgeOf.mockReturnValue({ ...bridge, canHandOffLaunch: undefined });
    await launchFromPanel(request());
    expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
    expect(postEvent).toHaveBeenCalledTimes(1);
  });

  it("never offers a message the create already carried", async () => {
    await launchFromPanel(request({ inputAccepted: true }));
    expect(bridge.canHandOffLaunch).not.toHaveBeenCalled();
    expect(postEvent).not.toHaveBeenCalled();
  });
});

describe("takeLaunchOffer", () => {
  const offer = { id: "offer-1", sessionId: "conv_1", path: "/c/conv_1" };
  const taken = {
    text: "Fix the build",
    skill: null,
    files: [
      { name: "notes.txt", type: "text/plain", bytes: new TextEncoder().encode("notes").buffer },
    ],
  };

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("queues the taken message for the session's chat, then opens that chat", async () => {
    bridge.takeLaunch.mockResolvedValue(taken);
    const navigate = vi.fn();
    await takeLaunchOffer(offer, navigate);
    expect(bridge.takeLaunch).toHaveBeenCalledWith("offer-1");
    expect(setPendingInitialPrompt).toHaveBeenCalledWith("conv_1", {
      text: "Fix the build",
      skill: null,
      files: [expect.any(File)],
    });
    expect(navigate).toHaveBeenCalledWith("/c/conv_1");
  });

  it("does nothing for an offer that already expired", async () => {
    const navigate = vi.fn();
    await takeLaunchOffer(offer, navigate);
    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("opens nothing and doesn't reject when taking the offer fails", async () => {
    bridge.takeLaunch.mockRejectedValue(new Error("renderer went away"));
    const navigate = vi.fn();
    await expect(takeLaunchOffer(offer, navigate)).resolves.toBeUndefined();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("still queues the text and opens the chat when an attachment can't be rebuilt", async () => {
    bridge.takeLaunch.mockResolvedValue(taken);
    vi.stubGlobal("File", function BrokenFile(): never {
      throw new TypeError("can't build it");
    });
    const navigate = vi.fn();
    await takeLaunchOffer(offer, navigate);
    expect(setPendingInitialPrompt).toHaveBeenCalledWith("conv_1", {
      text: "Fix the build",
      skill: null,
      files: [],
    });
    expect(navigate).toHaveBeenCalledWith("/c/conv_1");
  });

  it("still opens the chat when its message can't be queued", async () => {
    bridge.takeLaunch.mockResolvedValue(taken);
    vi.mocked(setPendingInitialPrompt).mockImplementationOnce(() => {
      throw new Error("store unavailable");
    });
    const navigate = vi.fn();
    await expect(takeLaunchOffer(offer, navigate)).resolves.toBeUndefined();
    expect(navigate).toHaveBeenCalledWith("/c/conv_1");
  });
});
