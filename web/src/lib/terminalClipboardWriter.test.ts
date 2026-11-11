import { beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import type * as ClipboardModule from "./clipboard";
import {
  isTerminalClipboardWritePending,
  queueTerminalClipboardWrite,
  writeExplicitTerminalClipboardText,
} from "./terminalClipboardWriter";

const clipboardMock = vi.hoisted(() => ({
  copyText: vi.fn<(text: string) => Promise<void>>(),
}));
vi.mock("./clipboard", () => ({ copyText: clipboardMock.copyText }));

beforeEach(() => {
  expect(isTerminalClipboardWritePending()).toBe(false);
  clipboardMock.copyText.mockReset().mockResolvedValue(undefined);
});

function request(text: string, isCurrent = () => true) {
  return { text, isCurrent, onResult: vi.fn<(copied: boolean) => void>() };
}

function deferredCopy() {
  let resolve!: () => void;
  let reject!: (error: Error) => void;
  clipboardMock.copyText.mockImplementationOnce(
    () =>
      new Promise<void>((success, failure) => {
        resolve = success;
        reject = failure;
      }),
  );
  return { resolve: () => resolve(), reject: () => reject(new Error("browser denied")) };
}

async function expectIdle() {
  await vi.waitFor(() => expect(isTerminalClipboardWritePending()).toBe(false));
}

describe("terminal clipboard writer", () => {
  it("starts the first write synchronously to preserve the click gesture", async () => {
    const write = request("clicked text");
    queueTerminalClipboardWrite(write);
    expect(clipboardMock.copyText).toHaveBeenCalledWith("clicked text");
    expect(isTerminalClipboardWritePending()).toBe(true);
    await expectIdle();
    expect(write.onResult).toHaveBeenCalledWith(true);
  });

  it("writes an explicit copy after the one in flight and drops queued program copies", async () => {
    const first = deferredCopy();
    const queued = request("queued program text");
    queueTerminalClipboardWrite(request("first"));
    queueTerminalClipboardWrite(queued);
    const explicit = writeExplicitTerminalClipboardText("chosen text");
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"]]);
    first.resolve();
    await expect(explicit).resolves.toBe(true);
    await expectIdle();
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"], ["chosen text"]]);
    expect(queued.onResult).not.toHaveBeenCalled();
  });

  it("starts an explicit copy synchronously when nothing is in flight", async () => {
    const explicit = writeExplicitTerminalClipboardText("chosen text");
    expect(clipboardMock.copyText).toHaveBeenCalledWith("chosen text");
    clipboardMock.copyText.mockRejectedValueOnce(new Error("browser denied"));
    await expect(explicit).resolves.toBe(true);
    await expect(writeExplicitTerminalClipboardText("denied")).resolves.toBe(false);
    await expectIdle();
  });

  it("keeps a program copy that arrives during an explicit copy behind it", async () => {
    const explicitCopy = deferredCopy();
    const explicit = writeExplicitTerminalClipboardText("chosen text");
    const later = request("later program text");
    queueTerminalClipboardWrite(later);
    expect(clipboardMock.copyText.mock.calls).toEqual([["chosen text"]]);
    explicitCopy.resolve();
    await expect(explicit).resolves.toBe(true);
    await expectIdle();
    expect(clipboardMock.copyText.mock.calls).toEqual([["chosen text"], ["later program text"]]);
  });

  it("is not overwritten by an older program copy whose late fallback succeeds", async () => {
    const { copyText } = await vi.importActual<typeof ClipboardModule>("./clipboard");
    clipboardMock.copyText.mockImplementation(copyText);
    let clipboard = "";
    let rejectProgramWrite!: () => void;
    const writeText = vi
      .fn<(text: string) => Promise<void>>()
      .mockImplementationOnce(
        () =>
          new Promise<void>((_resolve, reject) => {
            rejectProgramWrite = () => reject(new Error("browser denied"));
          }),
      )
      .mockImplementation(async (text) => {
        clipboard = text;
      });
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    const execCommand = vi.fn(() => {
      clipboard = (document.activeElement as HTMLTextAreaElement).value;
      return true;
    });
    Object.defineProperty(document, "execCommand", { configurable: true, value: execCommand });
    onTestFinished(() => {
      Reflect.deleteProperty(navigator, "clipboard");
      Reflect.deleteProperty(document, "execCommand");
    });

    queueTerminalClipboardWrite(request("program text"));
    const explicit = writeExplicitTerminalClipboardText("chosen text");
    rejectProgramWrite();

    await expect(explicit).resolves.toBe(true);
    expect(execCommand).toHaveBeenCalledOnce();
    expect(clipboard).toBe("chosen text");
  });

  it("serializes writers and keeps only the latest pending text", async () => {
    const first = deferredCopy();
    const superseded = request("superseded");
    const latest = request("latest");
    queueTerminalClipboardWrite(request("first"));
    queueTerminalClipboardWrite(superseded);
    queueTerminalClipboardWrite(latest);
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"]]);
    first.resolve();
    await expectIdle();
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"], ["latest"]]);
    expect(superseded.onResult).not.toHaveBeenCalled();
    expect(latest.onResult).toHaveBeenCalledWith(true);
  });

  it("rechecks queued permissions before starting a write", async () => {
    const first = deferredCopy();
    let allowed = true;
    const queued = request("revoked", () => allowed);
    queueTerminalClipboardWrite(request("first"));
    queueTerminalClipboardWrite(queued);
    allowed = false;
    first.resolve();
    await expectIdle();
    expect(clipboardMock.copyText.mock.calls).toEqual([["first"]]);
    expect(queued.onResult).not.toHaveBeenCalled();
  });

  it("suppresses obsolete results without releasing the in-flight lock early", async () => {
    const first = deferredCopy();
    let mounted = true;
    const unmounted = request("unmounted", () => mounted);
    queueTerminalClipboardWrite(unmounted);
    mounted = false;
    queueTerminalClipboardWrite(request("new view"));
    expect(clipboardMock.copyText.mock.calls).toEqual([["unmounted"]]);
    first.resolve();
    await expectIdle();
    expect(unmounted.onResult).not.toHaveBeenCalled();
    expect(clipboardMock.copyText.mock.calls).toEqual([["unmounted"], ["new view"]]);
  });

  it("continues with the latest write after a browser failure", async () => {
    const first = deferredCopy();
    const denied = request("denied");
    const latest = request("latest");
    queueTerminalClipboardWrite(denied);
    queueTerminalClipboardWrite(latest);
    first.reject();
    await expectIdle();
    expect(denied.onResult).toHaveBeenCalledWith(false);
    expect(latest.onResult).toHaveBeenCalledWith(true);
    expect(clipboardMock.copyText.mock.calls).toEqual([["denied"], ["latest"]]);
  });
});
