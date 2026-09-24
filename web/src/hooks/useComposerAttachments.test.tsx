// Unit tests for useComposerAttachments — the shared attachment state behind
// both composer surfaces. Exercised through the hook's public API so the
// assertions pin behavior (which files attach, what the error reads, when
// paste is claimed) rather than internal state wiring.

import { act, renderHook } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useComposerAttachments } from "./useComposerAttachments";

function textFile(name = "notes.txt"): File {
  return new File(["hello"], name, { type: "text/plain" });
}

function pngFile(name = "shot.png"): File {
  return new File([new Uint8Array(10)], name, { type: "image/png" });
}

/** An unsupported file — validation rejects it before any upload exists. */
function videoFile(name = "clip.mp4"): File {
  return new File([new Uint8Array(10)], name, { type: "video/mp4" });
}

/** Clipboard items as a real paste carries them: text and/or file entries. */
function pasteEvent({
  text,
  files = [],
  nullFile = false,
}: {
  text?: string;
  files?: File[];
  nullFile?: boolean;
}) {
  const preventDefault = vi.fn();
  const items: { kind: string; type: string; getAsFile: () => File | null }[] = [];
  if (text !== undefined) {
    items.push({ kind: "string", type: "text/plain", getAsFile: () => null });
  }
  for (const file of files) {
    items.push({ kind: "file", type: file.type, getAsFile: () => file });
  }
  if (nullFile) {
    // Some clipboard entries report kind "file" but yield no File object;
    // they must be skipped, not attached.
    items.push({ kind: "file", type: "image/png", getAsFile: () => null });
  }
  const event = { clipboardData: { items }, preventDefault };
  return {
    event: event as unknown as React.ClipboardEvent<HTMLTextAreaElement>,
    preventDefault,
  };
}

describe("useComposerAttachments", () => {
  it("starts empty by default and seeds from initialFiles", () => {
    const empty = renderHook(() => useComposerAttachments());
    expect(empty.result.current.files).toEqual([]);
    expect(empty.result.current.attachmentError).toBeNull();

    const seeded = renderHook(() =>
      useComposerAttachments({ initialFiles: [textFile(), pngFile()] }),
    );
    expect(seeded.result.current.files.map((f) => f.name)).toEqual(["notes.txt", "shot.png"]);
  });

  it("accepts valid files and appends them to the existing list", () => {
    const { result } = renderHook(() => useComposerAttachments({ initialFiles: [textFile()] }));
    const image = pngFile();

    act(() => result.current.addFiles([image]));

    expect(result.current.files.map((f) => f.name)).toEqual(["notes.txt", "shot.png"]);
    expect(result.current.attachmentError).toBeNull();
  });

  it("keeps the supported files from a mixed batch and names every rejection", () => {
    const { result } = renderHook(() => useComposerAttachments());

    act(() => result.current.addFiles([textFile(), videoFile(), videoFile("clip2.mp4")]));

    expect(result.current.files.map((f) => f.name)).toEqual(["notes.txt"]);
    const error = result.current.attachmentError ?? "";
    // Both rejections surface, joined so the notice shows them together.
    expect(error).toContain("clip.mp4");
    expect(error).toContain("clip2.mp4");
    expect(error.split("\n")).toHaveLength(2);
  });

  it("clears a stale rejection when a clean batch arrives", () => {
    const { result } = renderHook(() => useComposerAttachments());
    act(() => result.current.addFiles([videoFile()]));
    expect(result.current.attachmentError).not.toBeNull();

    act(() => result.current.addFiles([textFile()]));

    expect(result.current.attachmentError).toBeNull();
    expect(result.current.files.map((f) => f.name)).toEqual(["notes.txt"]);
  });

  it("removeFile drops the indexed chip and clears the error", () => {
    const { result } = renderHook(() => useComposerAttachments());
    act(() => result.current.addFiles([textFile(), videoFile(), pngFile()]));
    expect(result.current.attachmentError).not.toBeNull();

    act(() => result.current.removeFile(0));

    expect(result.current.files.map((f) => f.name)).toEqual(["shot.png"]);
    expect(result.current.attachmentError).toBeNull();
  });

  it("fires onAccepted with the accepted files only when something attached", () => {
    const onAccepted = vi.fn();
    const { result } = renderHook(() => useComposerAttachments({ onAccepted }));
    const ok = textFile();

    act(() => result.current.addFiles([ok, videoFile()]));
    expect(onAccepted).toHaveBeenCalledTimes(1);
    expect(onAccepted).toHaveBeenCalledWith([ok]);

    // A fully rejected batch appends nothing, so the side effect stays silent.
    act(() => result.current.addFiles([videoFile("nope.mp4")]));
    expect(onAccepted).toHaveBeenCalledTimes(1);
  });

  it("fires onRemoved after a removal", () => {
    const onRemoved = vi.fn();
    const { result } = renderHook(() =>
      useComposerAttachments({ initialFiles: [textFile()], onRemoved }),
    );

    act(() => result.current.removeFile(0));

    expect(onRemoved).toHaveBeenCalledTimes(1);
    expect(result.current.files).toEqual([]);
  });

  it("onPaste attaches clipboard files and claims the event", () => {
    const { result } = renderHook(() => useComposerAttachments());
    const { event, preventDefault } = pasteEvent({ files: [pngFile(), textFile()] });

    act(() => result.current.onPaste(event));

    expect(preventDefault).toHaveBeenCalledTimes(1);
    expect(result.current.files.map((f) => f.name)).toEqual(["shot.png", "notes.txt"]);
  });

  it("onPaste leaves a text-only paste to the browser", () => {
    const { result } = renderHook(() => useComposerAttachments());
    const { event, preventDefault } = pasteEvent({ text: "hello world" });

    act(() => result.current.onPaste(event));

    expect(preventDefault).not.toHaveBeenCalled();
    expect(result.current.files).toEqual([]);
  });

  it("onPaste ignores non-file kinds and null file payloads", () => {
    const { result } = renderHook(() => useComposerAttachments());
    const { event, preventDefault } = pasteEvent({ text: "hi", nullFile: true });

    act(() => result.current.onPaste(event));

    // No usable file → nothing attached and the paste stays native.
    expect(preventDefault).not.toHaveBeenCalled();
    expect(result.current.files).toEqual([]);
  });

  it("replaceFiles swaps the list wholesale and revalidates the replacement", () => {
    const { result } = renderHook(() => useComposerAttachments({ initialFiles: [textFile()] }));

    act(() => result.current.replaceFiles([pngFile(), videoFile()]));

    // The seeded file is gone; the rejected replacement never lands.
    expect(result.current.files.map((f) => f.name)).toEqual(["shot.png"]);
    expect(result.current.attachmentError).toContain("clip.mp4");
  });

  it("replaceFiles with a clean batch clears a stale error", () => {
    const { result } = renderHook(() => useComposerAttachments());
    act(() => result.current.addFiles([videoFile()]));
    expect(result.current.attachmentError).not.toBeNull();

    act(() => result.current.replaceFiles([pngFile()]));

    expect(result.current.files.map((f) => f.name)).toEqual(["shot.png"]);
    expect(result.current.attachmentError).toBeNull();
  });

  it("restoreFiles sets the list without validating or touching the error", () => {
    const { result } = renderHook(() => useComposerAttachments());
    // A previously-stored draft is restored verbatim — re-validating could
    // silently drop files the user already attached.
    act(() => result.current.restoreFiles([videoFile()]));

    expect(result.current.files.map((f) => f.name)).toEqual(["clip.mp4"]);
    expect(result.current.attachmentError).toBeNull();
  });

  it("clearError clears the notice without touching the files", () => {
    const { result } = renderHook(() => useComposerAttachments());
    act(() => result.current.addFiles([textFile(), videoFile()]));

    act(() => result.current.clearError());

    expect(result.current.attachmentError).toBeNull();
    expect(result.current.files.map((f) => f.name)).toEqual(["notes.txt"]);
  });

  it("clear empties the files and the notice", () => {
    const { result } = renderHook(() => useComposerAttachments());
    act(() => result.current.addFiles([textFile(), videoFile()]));

    act(() => result.current.clear());

    expect(result.current.files).toEqual([]);
    expect(result.current.attachmentError).toBeNull();
  });
});
