import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  isTerminalClipboardWritePending,
  queueTerminalClipboardWrite,
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
