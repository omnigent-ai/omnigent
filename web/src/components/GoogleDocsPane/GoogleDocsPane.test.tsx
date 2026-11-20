import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { classifyGoogleDocsUrl } from "@/lib/googleDocs";
import { GoogleDocsPane } from "./GoogleDocsPane";

const FIRST_URL = "https://docs.google.com/document/d/first/edit";
const SECOND_URL = "https://docs.google.com/document/d/second/edit?tab=t.two";

function reference(url: string) {
  const result = classifyGoogleDocsUrl(url);
  if (result.kind !== "embed") throw new Error("Expected a Google Docs editor URL");
  return result.reference;
}

const first = reference(FIRST_URL);
const second = reference(SECOND_URL);

function renderPane(overrides: Partial<React.ComponentProps<typeof GoogleDocsPane>> = {}) {
  const props = {
    document: first,
    savedDocument: first,
    canEditReference: true,
    onSave: vi.fn(async (_url: string | null) => undefined),
    onUseSavedDocument: vi.fn(),
    onRequestRevision: vi.fn(async (_url: string) => undefined),
    ...overrides,
  };
  return { ...render(<GoogleDocsPane {...props} />), props };
}

function button(name: string) {
  return screen.getByRole("button", { name });
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

it("opens deep links externally and rejects invalid URLs without replacing the editor", () => {
  const open = vi.spyOn(window, "open").mockImplementation(() => null);
  const pane = renderPane();
  const frame = screen.getByTitle("Google Docs editor");
  fireEvent.click(button("Change document"));
  const input = screen.getByRole("textbox", { name: "Google Docs URL" });
  fireEvent.change(input, { target: { value: "https://example.test/document/d/first/edit" } });
  fireEvent.click(button("Open document"));
  expect(screen.getByRole("alert")).toHaveTextContent("Expected an HTTPS Google Docs editor URL");

  const deepLink = FIRST_URL + "?tab=t.one#heading=h.example";
  fireEvent.change(input, { target: { value: deepLink } });
  fireEvent.click(button("Open document"));
  expect(open).toHaveBeenCalledWith(deepLink, "_blank", "noopener,noreferrer");
  expect(pane.props.onSave).not.toHaveBeenCalled();
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);
});

it("asks before reload, replacement and unlink, keeping the editor on cancel", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const pane = renderPane();
  const frame = screen.getByTitle("Google Docs editor");

  fireEvent.click(button("Reload"));
  fireEvent.click(button("Change document"));
  fireEvent.change(screen.getByRole("textbox", { name: "Google Docs URL" }), {
    target: { value: SECOND_URL },
  });
  fireEvent.click(button("Open document"));
  fireEvent.click(button("Unlink document"));
  expect(confirm.mock.calls.map(([message]) => message)).toEqual([
    expect.stringContaining("Reload the editor"),
    expect.stringContaining("Change the document"),
    expect.stringContaining("Unlink this document"),
  ]);
  expect(pane.props.onSave).not.toHaveBeenCalled();
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);

  confirm.mockReturnValue(true);
  await act(async () => fireEvent.click(button("Open document")));
  expect(pane.props.onSave).toHaveBeenCalledWith(SECOND_URL);
  fireEvent.click(button("Change document"));
  await act(async () => fireEvent.click(button("Unlink document")));
  expect(pane.props.onSave).toHaveBeenLastCalledWith(null);
  fireEvent.click(button("Reload"));
  expect(screen.getByTitle("Google Docs editor")).not.toBe(frame);
});

it("requires confirmation before switching to a shared update or closing after an unlink", () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const pane = renderPane({ savedDocument: second, canEditReference: false });
  const frame = screen.getByTitle("Google Docs editor");
  fireEvent.click(button("Switch to saved document"));
  expect(pane.props.onUseSavedDocument).not.toHaveBeenCalled();
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);

  pane.rerender(<GoogleDocsPane {...pane.props} savedDocument={null} />);
  confirm.mockReturnValue(true);
  fireEvent.click(button("Close editor"));
  expect(confirm).toHaveBeenLastCalledWith(
    expect.stringContaining("Check that Google Docs has saved"),
  );
  expect(pane.props.onUseSavedDocument).toHaveBeenCalledOnce();
});

it("reports revision delivery failure for the requested document", async () => {
  const onRequestRevision = vi.fn().mockRejectedValue(new Error("The agent is offline."));
  renderPane({ onRequestRevision });
  fireEvent.click(button("Request agent revision"));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("The agent is offline."));
  expect(onRequestRevision).toHaveBeenCalledWith(FIRST_URL);
});
