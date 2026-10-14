import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { installUploadWindowGuard, UploadWindowGuard } from "./UploadWindowGuard";
import { ownerKey, useUploadStore, type UploadRow } from "@/store/uploadStore";
import { discardUploads } from "@/lib/workspaceUploadController";

vi.mock("@/hooks/useViewerId", () => ({ useViewerId: () => "alice" }));
vi.mock("@/lib/workspaceUploadController", () => ({ discardUploads: vi.fn() }));
vi.mock("@/shell/UploadConflicts", () => ({ UploadConflicts: () => null }));
vi.mock("sonner", () => ({ toast: Object.assign(vi.fn(), { error: vi.fn(), dismiss: vi.fn() }) }));
function activeRow(): UploadRow {
  return {
    origin: window.location.origin,
    identity: "alice",
    session: "previous",
    environment: "default",
    key: "active",
    id: "active",
    batch: "batch",
    name: "a.txt",
    file: new File(["bytes"], "a.txt"),
    size: 5,
    offset: 2,
    directory: "",
    destination: "workspace",
    controller: new AbortController(),
    status: "uploading",
    conflict: "fail",
    limits: {
      chunk: { min: 2, initial: 4, max: 8 },
      max_file_bytes: 100,
      max_files_per_batch: 10,
      max_batch_bytes: 200,
      max_open_uploads: 16,
      max_open_bytes: 200,
      parallel_files: 2,
    },
  };
}
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useUploadStore.setState({ rows: {}, announcement: "" });
});

it("prevents file near-miss navigation, never stops propagation, and leaves non-file drags untouched", () => {
  const remove = installUploadWindowGuard(document);
  for (const type of ["dragover", "drop"]) {
    const event = new Event(type, { bubbles: true, cancelable: true });
    Object.defineProperty(event, "dataTransfer", { value: { types: ["Files"] } });
    const stop = vi.spyOn(event, "stopPropagation");
    document.body.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(stop).not.toHaveBeenCalled();
  }
  expect(fireEvent.drop(document.body, { dataTransfer: { types: ["text/plain"] } })).toBe(true);
  remove();
  expect(fireEvent.drop(document.body, { dataTransfer: { types: ["Files"] } })).toBe(true);
});

it("reports the disabled target's reason without choosing a destination", () => {
  const remove = installUploadWindowGuard(document);
  render(<div data-upload-disabled-reason="Reconnect session" data-testid="disabled" />);
  fireEvent.drop(screen.getByTestId("disabled"), { dataTransfer: { types: ["Files"] } });
  expect(toast.error).toHaveBeenCalledWith("Reconnect session");
  remove();
});

it("warns only during active uploads and discards the account on pagehide and unmount", () => {
  const view = render(<UploadWindowGuard />);
  const idle = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(idle);
  expect(idle.defaultPrevented).toBe(false);
  act(() => useUploadStore.getState().add([activeRow()]));
  const active = new Event("beforeunload", { cancelable: true });
  window.dispatchEvent(active);
  expect(active.defaultPrevented).toBe(true);
  window.dispatchEvent(new Event("pagehide"));
  expect(discardUploads).toHaveBeenCalledWith(window.location.origin, "alice");
  view.unmount();
  expect(discardUploads).toHaveBeenCalledTimes(2);
});

it("keeps an aggregate toast for an earlier session when its tray is hidden and dismisses it on reopening", () => {
  const row = activeRow();
  useUploadStore.getState().add([row]);
  render(<UploadWindowGuard />);
  expect(toast).toHaveBeenCalledWith(
    "Uploads: 0/1 file · 2 B / 5 B",
    expect.objectContaining({ duration: Infinity }),
  );
  let hide: (() => void) | undefined;
  act(() => {
    hide = useUploadStore.getState().showTray(ownerKey(row));
  });
  expect(toast.dismiss).toHaveBeenCalledWith(`workspace-uploads:${ownerKey(row)}`);
  act(() => hide?.());
  expect(vi.mocked(toast).mock.calls).toHaveLength(2);
});
