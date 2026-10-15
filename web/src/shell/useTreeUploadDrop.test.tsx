import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useRef, useState } from "react";
import { afterEach, expect, it, vi } from "vitest";
import { resolveUploadTarget } from "./WorkspaceUploads";
import { useTreeUploadDrop } from "./useTreeUploadDrop";

const transfer = { types: ["Files"], files: [new File(["payload"], "new.txt")] };
function Harness({ enabled = true, onFiles = vi.fn(), onExpand = vi.fn() }) {
  const ref = useRef<HTMLDivElement>(null);
  const [mounted, setMounted] = useState(true);
  const { active, hover } = useTreeUploadDrop(ref, {
    enabled,
    directory: "src",
    onFiles,
    onExpand,
  });
  return (
    <>
      <div ref={ref} data-testid="scroll">
        {mounted && (
          <div data-drop-dir="assets" data-drop-folder data-testid="folder">
            <button type="button" aria-expanded="false">
              <span data-testid="folder-label">assets</span>
            </button>
          </div>
        )}
        {!mounted && (
          <div data-drop-dir="lib" data-drop-folder data-testid="replacement">
            <button type="button" aria-expanded="false">
              lib
            </button>
          </div>
        )}
        <div data-drop-dir="assets" data-testid="file">
          <span>existing.txt</span>
        </div>
        <div data-drop-dir="" data-testid="root-file">
          top.txt
        </div>
        <span data-testid="space" />
      </div>
      <button type="button" onClick={() => setMounted(!mounted)}>
        Slide window
      </button>
      <span data-testid="feedback">
        {active && hover ? `Upload to ${hover.directory}` : "idle"}
      </span>
      <span data-testid="target-folder">{active && hover?.folderRow?.dataset.dropDir}</span>
      <div data-testid="outside" />
    </>
  );
}
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

it("delegates to newly mounted rows and never uses stale hover to resolve a drop", () => {
  const onFiles = vi.fn((_: File[], event: DragEvent) => resolveUploadTarget(event, "src"));
  render(<Harness onFiles={onFiles} />);
  fireEvent.dragOver(screen.getByTestId("folder-label"), { dataTransfer: transfer });
  expect(screen.getByTestId("feedback")).toHaveTextContent("Upload to src/assets");
  fireEvent.click(screen.getByText("Slide window"));
  fireEvent.drop(screen.getByTestId("replacement"), { dataTransfer: transfer });
  expect(onFiles).toHaveLastReturnedWith("src/lib");
  expect(onFiles).toHaveBeenCalledTimes(1);
  expect(screen.getByTestId("feedback")).toHaveTextContent("idle");
});

it("spring-loads a collapsed folder after 1000ms without restarting on repeated dragover", () => {
  vi.useFakeTimers();
  const onExpand = vi.fn();
  render(<Harness onExpand={onExpand} />);
  fireEvent.dragOver(screen.getByTestId("folder-label"), { dataTransfer: transfer });
  act(() => vi.advanceTimersByTime(999));
  expect(onExpand).not.toHaveBeenCalled();
  fireEvent.dragOver(screen.getByTestId("folder-label"), { dataTransfer: transfer });
  act(() => vi.advanceTimersByTime(1));
  expect(onExpand).toHaveBeenCalledExactlyOnceWith("assets");
});

it.each(["leave", "scroll", "unmount", "outside", "removed-row", "drop", "end"])(
  "cancels spring-load on %s and clears feedback",
  (action) => {
    vi.useFakeTimers();
    const onExpand = vi.fn();
    const rendered = render(<Harness onExpand={onExpand} />);
    const row = screen.getByTestId("folder");
    fireEvent.dragOver(row, { dataTransfer: transfer });
    expect(screen.getByTestId("feedback")).toHaveTextContent("Upload to src/assets");
    if (action === "leave")
      fireEvent.dragLeave(screen.getByTestId("scroll"), { dataTransfer: transfer });
    if (action === "scroll") fireEvent.scroll(screen.getByTestId("scroll"));
    if (action === "unmount") rendered.unmount();
    if (action === "outside")
      fireEvent.dragOver(screen.getByTestId("outside"), { dataTransfer: transfer });
    if (action === "removed-row") fireEvent.click(screen.getByText("Slide window"));
    if (action === "drop") fireEvent.drop(row, { dataTransfer: transfer });
    if (action === "end") fireEvent.dragEnd(row, { dataTransfer: transfer });
    act(() => vi.advanceTimersByTime(1000));
    expect(onExpand).not.toHaveBeenCalled();
    if (action !== "unmount") expect(screen.getByTestId("feedback")).toHaveTextContent("idle");
  },
);

it.each([false, true])("accepts drops and hover only with the tree gate enabled=%s", (enabled) => {
  vi.useFakeTimers();
  const onFiles = vi.fn();
  const onExpand = vi.fn();
  render(<Harness enabled={enabled} onFiles={onFiles} onExpand={onExpand} />);
  const row = screen.getByTestId("folder");
  expect(fireEvent.dragOver(row, { dataTransfer: transfer })).toBe(!enabled);
  expect(screen.getByTestId("feedback")).toHaveTextContent(
    enabled ? "Upload to src/assets" : "idle",
  );
  fireEvent.drop(row, { dataTransfer: transfer });
  expect(onFiles).toHaveBeenCalledTimes(enabled ? 1 : 0);
  act(() => vi.advanceTimersByTime(1000));
  expect(onExpand).not.toHaveBeenCalled();
});

it("file rows target their parent without spring-loading; text drags do nothing", () => {
  vi.useFakeTimers();
  const onFiles = vi.fn();
  const onExpand = vi.fn();
  render(<Harness onFiles={onFiles} onExpand={onExpand} />);
  fireEvent.dragOver(screen.getByTestId("file"), { dataTransfer: transfer });
  expect(screen.getByTestId("feedback")).toHaveTextContent("Upload to src/assets");
  expect(screen.getByTestId("target-folder")).toHaveTextContent("assets");
  act(() => vi.advanceTimersByTime(1000));
  expect(onExpand).not.toHaveBeenCalled();
  fireEvent.dragEnd(screen.getByTestId("file"), { dataTransfer: transfer });
  const text = { types: ["text/plain"], files: [] };
  expect(fireEvent.dragOver(screen.getByTestId("folder"), { dataTransfer: text })).toBe(true);
  fireEvent.drop(screen.getByTestId("folder"), { dataTransfer: text });
  expect(onFiles).not.toHaveBeenCalled();
  expect(screen.getByTestId("feedback")).toHaveTextContent("idle");
});

it("keeps the root drop zone for top-level file rows and empty space", () => {
  render(<Harness />);
  for (const target of ["root-file", "space"]) {
    fireEvent.dragOver(screen.getByTestId(target), { dataTransfer: transfer });
    expect(screen.getByTestId("target-folder")).toBeEmptyDOMElement();
    expect(screen.getByTestId("feedback")).toHaveTextContent("Upload to src");
  }
});

it("falls back to the named destination when virtualization removes the parent row", async () => {
  render(<Harness />);
  fireEvent.dragOver(screen.getByTestId("file"), { dataTransfer: transfer });
  expect(screen.getByTestId("target-folder")).toHaveTextContent("assets");
  fireEvent.click(screen.getByText("Slide window"));
  await waitFor(() => expect(screen.getByTestId("target-folder")).toBeEmptyDOMElement());
  expect(screen.getByTestId("feedback")).toHaveTextContent("Upload to src/assets");
});

it("preserves depth after scroll so crossing rows does not clear hover or restart spring-load", () => {
  vi.useFakeTimers();
  const onExpand = vi.fn();
  render(<Harness onExpand={onExpand} />);
  const file = screen.getByTestId("file");
  const folder = screen.getByTestId("folder");
  fireEvent.dragEnter(file, { dataTransfer: transfer });
  fireEvent.scroll(screen.getByTestId("scroll"));
  expect(screen.getByTestId("feedback")).toHaveTextContent("idle");
  fireEvent.dragEnter(folder, { dataTransfer: transfer });
  fireEvent.dragLeave(file, { dataTransfer: transfer });
  expect(screen.getByTestId("feedback")).toHaveTextContent("Upload to src/assets");
  act(() => vi.advanceTimersByTime(999));
  fireEvent.dragEnter(screen.getByTestId("folder-label"), { dataTransfer: transfer });
  fireEvent.dragLeave(folder, { dataTransfer: transfer });
  act(() => vi.advanceTimersByTime(1));
  expect(onExpand).toHaveBeenCalledExactlyOnceWith("assets");
});
