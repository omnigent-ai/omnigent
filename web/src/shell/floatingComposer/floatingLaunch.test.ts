import { beforeEach, describe, expect, it, vi } from "vitest";
import type * as SessionsApi from "@/lib/sessionsApi";

const bridge = vi.hoisted(() => ({
  openInMainWindow: vi.fn(),
  dismiss: vi.fn(),
  filePicker: vi.fn(),
  announceSupport: vi.fn(),
  canHandOffLaunch: vi.fn(),
  takeLaunchOffer: vi.fn(),
  confirmLaunchOffer: vi.fn(),
  declineLaunchOffer: vi.fn(),
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
  isListeningForInitialPrompt: vi.fn(() => true),
}));

import { ApiError, fetchSessionItemsPage, postEvent } from "@/lib/sessionsApi";
import { sendVerdictFromServer } from "@/lib/userInputDelivery";
import {
  isListeningForInitialPrompt,
  setPendingInitialPrompt,
  uploadFileBlocks,
} from "@/store/chatStore";
import type { FloatingLaunchRequest } from "@/shell/composerPresentation";
import {
  acceptLaunchOffer,
  launchFromPanel,
  LAUNCH_MESSAGE_MAX_BYTES,
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

const delivered = { error: null, freshComposer: true };
const failed = { error: SEND_FAILED_ERROR, freshComposer: false };
const unknown = { error: SEND_UNKNOWN_ERROR, freshComposer: false };

beforeEach(() => {
  vi.clearAllMocks();
  bridgeOf.mockReturnValue(bridge);
  bridge.openInMainWindow.mockResolvedValue({ ok: true });
  bridge.canHandOffLaunch.mockResolvedValue(false);
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
    expect(bridge.canHandOffLaunch).not.toHaveBeenCalled();
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

  describe("when the main window can send the message", () => {
    it("hands over the message with its attachments as bytes, and posts nothing it sends", async () => {
      const file = new File(["hello"], "notes.txt", { type: "text/plain" });
      bridge.canHandOffLaunch.mockResolvedValue(true);
      bridge.openInMainWindow.mockResolvedValue({ ok: true, delivery: "page" });

      const outcome = await launchFromPanel(request({ files: [file] }));

      expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", {
        text: "Fix the build",
        skill: null,
        files: [{ name: "notes.txt", type: "text/plain", bytes: expect.any(Uint8Array) }],
      });
      const [handed] = vi.mocked(bridge.openInMainWindow).mock.calls[0]![1]!.files;
      expect(new TextDecoder().decode(handed!.bytes)).toBe("hello");
      expect(uploadFileBlocks).not.toHaveBeenCalled();
      expect(postEvent).not.toHaveBeenCalled();
      expect(outcome).toEqual(delivered);
    });

    it("delivers the message itself when the window didn't send it", async () => {
      bridge.canHandOffLaunch.mockResolvedValue(true);
      bridge.openInMainWindow.mockResolvedValue({ ok: true });

      const outcome = await launchFromPanel(request());

      expect(postEvent).toHaveBeenCalledTimes(1);
      expect(outcome).toEqual(delivered);
    });

    it("hands over a skill launch's name and arguments, never its attachments", async () => {
      bridge.canHandOffLaunch.mockResolvedValue(true);
      bridge.openInMainWindow.mockResolvedValue({ ok: true, delivery: "page" });

      await launchFromPanel(
        request({
          text: "/review 12",
          skill: { name: "review", args: "12" },
          files: [new File(["x"], "a.txt")],
        }),
      );

      expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", {
        text: "/review 12",
        skill: { name: "review", args: "12" },
        files: [],
      });
      expect(postEvent).not.toHaveBeenCalled();
    });

    it.each([
      [
        "says it can't take the message",
        (_cause: Error) => bridge.canHandOffLaunch.mockResolvedValue(false),
        false,
      ],
      [
        "has no way to ask",
        (_cause: Error) => bridgeOf.mockReturnValue({ ...bridge, canHandOffLaunch: undefined }),
        false,
      ],
      ["fails to answer", (cause: Error) => bridge.canHandOffLaunch.mockRejectedValue(cause), true],
    ])("reads no attachment bytes when the shell %s", async (_case, setUp, logged) => {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const cause = new Error("shell gone");
      setUp(cause);
      const file = new File(["x"], "a.txt");
      const read = vi.spyOn(file, "arrayBuffer");

      await launchFromPanel(request({ files: [file] }));

      expect(read).not.toHaveBeenCalled();
      expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
      if (logged) {
        expect(warn).toHaveBeenCalledWith(
          "[floatingComposer] couldn't prepare a launch's message:",
          cause,
        );
      } else {
        expect(warn).not.toHaveBeenCalled();
      }
      warn.mockRestore();
    });

    it("reads no attachment bytes past the shared size cap", async () => {
      const big = new File(["x"], "big.bin");
      Object.defineProperty(big, "size", { value: LAUNCH_MESSAGE_MAX_BYTES + 1 });
      const read = vi.spyOn(big, "arrayBuffer");
      bridge.canHandOffLaunch.mockResolvedValue(true);

      await launchFromPanel(request({ files: [big] }));

      expect(read).not.toHaveBeenCalled();
      expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
      expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [big]);
    });

    it("keeps the panel's own files when an attachment can't be read", async () => {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      const file = new File(["x"], "a.txt");
      vi.spyOn(file, "arrayBuffer").mockRejectedValue(new Error("unreadable"));
      bridge.canHandOffLaunch.mockResolvedValue(true);
      vi.mocked(uploadFileBlocks).mockResolvedValue([]);

      await launchFromPanel(request({ files: [file] }));

      expect(bridge.openInMainWindow).toHaveBeenCalledWith("/c/conv_1", null);
      expect(uploadFileBlocks).toHaveBeenCalledWith("conv_1", [file]);
      expect(postEvent).toHaveBeenCalledTimes(1);
      expect(warn).toHaveBeenCalledWith(
        "[floatingComposer] couldn't prepare a launch's message:",
        expect.any(Error),
      );
      warn.mockRestore();
    });

    it("reports unknown, not a resend, when opening rejects after the message was handed over", async () => {
      bridge.canHandOffLaunch.mockResolvedValue(true);
      bridge.openInMainWindow.mockRejectedValue(new Error("reply lost"));

      const outcome = await launchFromPanel(request());

      expect(postEvent).not.toHaveBeenCalled();
      expect(outcome).toEqual(unknown);
    });
  });
});

describe("acceptLaunchOffer", () => {
  const offer = { offerId: 7, sessionId: "conv_1" };
  const message = {
    text: "Fix the build",
    skill: null,
    files: [{ name: "a.txt", type: "text/plain", bytes: new Uint8Array([104, 105]) }],
  };

  it("queues the rebuilt message for its session only after the shell confirms it", async () => {
    bridge.takeLaunchOffer.mockResolvedValue(message);
    let confirm: (value: boolean) => void = () => {};
    bridge.confirmLaunchOffer.mockReturnValue(
      new Promise<boolean>((resolve) => {
        confirm = resolve;
      }),
    );

    const accepted = acceptLaunchOffer(offer);
    await vi.waitFor(() => expect(bridge.confirmLaunchOffer).toHaveBeenCalledWith(7));
    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
    confirm(true);
    await accepted;

    expect(bridge.takeLaunchOffer).toHaveBeenCalledWith(7);
    expect(bridge.declineLaunchOffer).not.toHaveBeenCalled();
    expect(setPendingInitialPrompt).toHaveBeenCalledTimes(1);
    const [sessionId, prompt] = vi.mocked(setPendingInitialPrompt).mock.calls[0]!;
    expect(sessionId).toBe("conv_1");
    expect(prompt.text).toBe("Fix the build");
    expect(prompt.skill).toBeNull();
    const [file] = prompt.files!;
    expect(file).toBeInstanceOf(File);
    expect(file).toMatchObject({ name: "a.txt", type: "text/plain", size: 2 });
    expect(new TextDecoder().decode(new Uint8Array(await file!.arrayBuffer()))).toBe("hi");
    expect(vi.mocked(setPendingInitialPrompt).mock.invocationCallOrder[0]!).toBeGreaterThan(
      bridge.confirmLaunchOffer.mock.invocationCallOrder[0]!,
    );
  });

  it("confirms nothing and queues nothing when an attachment can't be rebuilt", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const broken = {
      text: "Fix the build",
      skill: null,
      files: [{ name: "a.txt", type: "text/plain", bytes: new Uint8Array([1]) }],
    };
    // A rebuild that throws part way through, as a corrupt attachment would.
    Object.defineProperty(broken.files[0], "bytes", {
      get() {
        throw new Error("corrupt");
      },
    });
    bridge.takeLaunchOffer.mockResolvedValue(broken);

    await acceptLaunchOffer(offer);

    expect(bridge.confirmLaunchOffer).not.toHaveBeenCalled();
    expect(bridge.declineLaunchOffer).toHaveBeenCalledWith(7);
    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
    expect(warn).toHaveBeenCalledWith(
      "[floatingComposer] couldn't take a launch's message:",
      expect.any(Error),
    );
    warn.mockRestore();
  });

  it("confirms nothing when no page is showing the session to send it", async () => {
    bridge.takeLaunchOffer.mockResolvedValue(message);
    vi.mocked(isListeningForInitialPrompt).mockReturnValueOnce(false);

    await acceptLaunchOffer(offer);

    expect(isListeningForInitialPrompt).toHaveBeenCalledWith("conv_1");
    expect(bridge.confirmLaunchOffer).not.toHaveBeenCalled();
    expect(bridge.declineLaunchOffer).toHaveBeenCalledWith(7);
    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
  });

  it("queues nothing when the shell refuses the confirm", async () => {
    bridge.takeLaunchOffer.mockResolvedValue(message);
    bridge.confirmLaunchOffer.mockResolvedValue(false);

    await acceptLaunchOffer(offer);

    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
  });

  it("confirms nothing when there is no message to take", async () => {
    bridge.takeLaunchOffer.mockResolvedValue(null);

    await acceptLaunchOffer(offer);

    expect(bridge.confirmLaunchOffer).not.toHaveBeenCalled();
    expect(bridge.declineLaunchOffer).not.toHaveBeenCalled();
    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
  });

  it("leaves the message to the panel when taking it fails, and never throws", async () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    bridge.takeLaunchOffer.mockRejectedValue(new Error("reply lost"));

    await expect(acceptLaunchOffer(offer)).resolves.toBeUndefined();
    expect(warn).toHaveBeenCalledWith(
      "[floatingComposer] couldn't take a launch's message:",
      expect.any(Error),
    );
    warn.mockRestore();

    expect(bridge.confirmLaunchOffer).not.toHaveBeenCalled();
    expect(bridge.declineLaunchOffer).toHaveBeenCalledWith(7);
    expect(setPendingInitialPrompt).not.toHaveBeenCalled();
  });

  it.each([["takeLaunchOffer"], ["confirmLaunchOffer"]] as const)(
    "does nothing on a shell without %s, so the panel delivers",
    async (missing) => {
      bridgeOf.mockReturnValueOnce({ ...bridge, [missing]: undefined });

      await acceptLaunchOffer(offer);

      expect(bridge.takeLaunchOffer).not.toHaveBeenCalled();
    },
  );
});
