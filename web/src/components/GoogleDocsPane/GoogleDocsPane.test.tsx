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

function openActions() {
  fireEvent.keyDown(button("Document actions"), { key: "ArrowDown" });
  return screen.getByRole("menu", { name: "Document actions" });
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

it("reports review delivery failure for the requested document", async () => {
  const onRequestRevision = vi.fn().mockRejectedValue(new Error("The agent is offline."));
  renderPane({ onRequestRevision });
  fireEvent.click(button("Request agent review"));
  await waitFor(() =>
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Could not request a review. The agent is offline.",
    ),
  );
  expect(onRequestRevision).toHaveBeenCalledWith(FIRST_URL);
});

it.each([null, first])("offers linking or document controls without UI creation (%s)", (doc) => {
  renderPane({ document: doc, savedDocument: doc });
  if (doc === null) {
    expect(screen.getByRole("textbox", { name: "Google Docs URL" })).toBeVisible();
    expect(button("Open document")).toBeDisabled();
  } else {
    expect(button("Document actions")).toHaveAttribute("aria-haspopup", "menu");
    expect(screen.getByTitle("Google Docs editor")).toBeVisible();
  }
  expect(screen.queryByRole("button", { name: "New Google Doc" })).not.toBeInTheDocument();
});

it("opens actions with the keyboard and retains the editor through menu changes", async () => {
  const pane = renderPane();
  const frame = screen.getByTitle("Google Docs editor");
  const trigger = button("Document actions");
  trigger.focus();
  const menu = openActions();
  expect(trigger).toHaveAttribute("aria-expanded", "true");
  await waitFor(() =>
    expect(screen.getByRole("menuitem", { name: "Change document" })).toHaveFocus(),
  );
  const external = screen.getByRole("menuitem", { name: "Open in Google Docs" });
  expect(external).toHaveAttribute("href", FIRST_URL);
  expect(external).toHaveAttribute("target", "_blank");
  expect(external).toHaveAttribute("rel", "noopener noreferrer");
  fireEvent.keyDown(menu, { key: "Escape" });
  await waitFor(() => expect(trigger).toHaveFocus());
  expect(trigger).toHaveAttribute("aria-expanded", "false");

  openActions();
  fireEvent.click(screen.getByRole("menuitem", { name: "Change document" }));
  expect(screen.getByRole("textbox", { name: "Google Docs URL" })).toHaveValue(FIRST_URL);
  fireEvent.click(button("Cancel"));
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);
  expect(pane.props.onSave).not.toHaveBeenCalled();
});

it("confirms reload and unlink from the actions menu", async () => {
  const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
  const pane = renderPane();
  const frame = screen.getByTitle("Google Docs editor");
  for (const action of ["Reload", "Unlink document"]) {
    openActions();
    fireEvent.click(screen.getByRole("menuitem", { name: action }));
  }
  expect(pane.props.onSave).not.toHaveBeenCalled();
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);

  confirm.mockReturnValue(true);
  openActions();
  await act(async () => fireEvent.click(screen.getByRole("menuitem", { name: "Unlink document" })));
  expect(pane.props.onSave).toHaveBeenCalledWith(null);
  openActions();
  fireEvent.click(screen.getByRole("menuitem", { name: "Reload" }));
  expect(screen.getByTitle("Google Docs editor")).not.toBe(frame);
});

it("keeps viewer actions available without editing or review actions", () => {
  renderPane({ canEditReference: false });
  openActions();
  expect(screen.getAllByRole("menuitem").map((item) => item.textContent)).toEqual([
    "Reload",
    "Open in Google Docs",
  ]);
});

it("disables review in both controls until the session is ready", () => {
  const reason = "Wait for this chat to connect before sending a document request.";
  const pane = renderPane({ revisionDisabledReason: reason });
  expect(button("Request agent review")).toBeDisabled();
  expect(button("Request agent review")).toHaveAccessibleDescription(reason);
  openActions();
  const revision = screen.getByRole("menuitem", { name: "Request agent review" });
  expect(revision).toHaveAttribute("aria-disabled", "true");
  expect(revision).toHaveAccessibleDescription(reason);
  fireEvent.click(revision);
  expect(pane.props.onRequestRevision).not.toHaveBeenCalled();
});

it("requests a review from the menu once while pending and preserves the editor", async () => {
  let finishRequest!: () => void;
  const onRequestRevision = vi.fn(
    () =>
      new Promise<void>((resolve) => {
        finishRequest = resolve;
      }),
  );
  renderPane({ onRequestRevision });
  const frame = screen.getByTitle("Google Docs editor");
  const inlineRevision = button("Request agent review");
  openActions();
  fireEvent.click(screen.getByRole("menuitem", { name: "Request agent review" }));
  expect(inlineRevision).toBeDisabled();
  openActions();
  const revision = screen.getByRole("menuitem", { name: "Request agent review" });
  expect(revision).toHaveAttribute("aria-disabled", "true");
  fireEvent.click(revision);
  expect(onRequestRevision).toHaveBeenCalledExactlyOnceWith(FIRST_URL);
  await act(async () => finishRequest());
  expect(revision).not.toHaveAttribute("aria-disabled");
  expect(inlineRevision).not.toBeDisabled();
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);
});

it("disables changing, reloading, unlinking and review during a save in both variants", async () => {
  let finishSave!: () => void;
  const pane = renderPane({
    onSave: vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finishSave = resolve;
        }),
    ),
  });
  vi.spyOn(window, "confirm").mockReturnValue(true);
  const frame = screen.getByTitle("Google Docs editor");
  const inlineActions = ["Change document", "Reload", "Request agent review"].map(button);
  fireEvent.click(button("Change document"));
  fireEvent.change(screen.getByRole("textbox", { name: "Google Docs URL" }), {
    target: { value: SECOND_URL },
  });
  fireEvent.click(button("Open document"));
  for (const action of inlineActions) expect(action).toBeDisabled();
  openActions();
  for (const name of ["Change document", "Reload", "Unlink document", "Request agent review"]) {
    const action = screen.getByRole("menuitem", { name });
    expect(action).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(action);
  }
  expect(pane.props.onSave).toHaveBeenCalledExactlyOnceWith(SECOND_URL);
  expect(pane.props.onRequestRevision).not.toHaveBeenCalled();
  expect(screen.getByTitle("Google Docs editor")).toBe(frame);
  await act(async () => finishSave());
});
