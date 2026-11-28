import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  createGadgetComment,
  getGadget,
  getGadgetContent,
  getGadgetGadgetSessionStatus,
  listGadgetComments,
} from "@/gadgets/api/gadgetApi";
import type { GadgetFrameProps } from "@/gadgets/host/GadgetFrame";
import { GADGET_SDK_VERSION } from "@/gadgets/sdk/gadgetProtocol";
import { GadgetView } from "./GadgetView";
import { renderWithGadgets, gadgetFixture } from "./testUtils";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  getGadget: vi.fn(),
  getGadgetContent: vi.fn(),
  getGadgetGadgetSessionStatus: vi.fn(),
  listGadgetComments: vi.fn(),
  createGadgetComment: vi.fn(),
}));
let frameProps: GadgetFrameProps | null = null;
vi.mock("@/gadgets/host/GadgetFrame", () => ({
  GadgetFrame: (props: GadgetFrameProps) => {
    frameProps = props;
    return <div data-testid="gadget-frame">{props.content.html}</div>;
  },
}));

const BOARD = gadgetFixture({ id: "board" });

function renderView(gadget = BOARD, onReloadGadget = vi.fn()) {
  renderWithGadgets(
    <GadgetView
      gadget={gadget}
      sessionId="conv_1"
      onReloadGadget={onReloadGadget}
      toolbarTarget={null}
      renderToolbar={(commentsButton) => commentsButton}
    />,
  );
  return onReloadGadget;
}

beforeEach(() => {
  frameProps = null;
  vi.mocked(getGadgetContent).mockResolvedValue({
    revision_id: "rev-1",
    html: "<p>board</p>",
    sdk_version: GADGET_SDK_VERSION,
    capabilities: ["data.read"],
  });
  vi.mocked(listGadgetComments).mockResolvedValue({ items: [], canComment: true });
});
afterEach(() => vi.clearAllMocks());

describe("a gadget beside a session", () => {
  it("runs its published page, sending its work to the session beside it", async () => {
    renderView();
    expect(await screen.findByTestId("gadget-frame")).toHaveTextContent("<p>board</p>");
    expect(frameProps?.sessionId).toBe("conv_1");
    await waitFor(() => expect(frameProps?.onAddComment).toBeDefined());
  });

  it("loads the newer version as soon as the page learns one is published", async () => {
    const onReload = renderView();
    await screen.findByTestId("gadget-frame");
    act(() => frameProps?.onRevisionChanged());
    expect(onReload).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText(/A newer version of this gadget was published\. Loading it…/),
    ).toBeVisible();
    // The button stays for a reload that did not bring the new version.
    fireEvent.click(screen.getByRole("button", { name: "Load new version" }));
    expect(onReload).toHaveBeenCalledTimes(2);
  });

  it("keeps the previous version running while a newly published one loads", async () => {
    const { rerender } = renderWithGadgets(
      <GadgetView
        gadget={BOARD}
        sessionId="conv_1"
        onReloadGadget={vi.fn()}
        toolbarTarget={null}
        renderToolbar={(commentsButton) => commentsButton}
      />,
    );
    expect(await screen.findByTestId("gadget-frame")).toHaveTextContent("<p>board</p>");
    let finish: (content: Awaited<ReturnType<typeof getGadgetContent>>) => void = () => {};
    vi.mocked(getGadgetContent).mockReturnValueOnce(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    rerender(
      <GadgetView
        gadget={{ ...BOARD, published_revision: "rev-2" }}
        sessionId="conv_1"
        onReloadGadget={vi.fn()}
        toolbarTarget={null}
        renderToolbar={(commentsButton) => commentsButton}
      />,
    );
    // No "Loading gadget…" in between: the old page stays, with a quiet note.
    expect(screen.queryByText("Loading gadget…")).toBeNull();
    expect(screen.getByTestId("gadget-frame")).toHaveTextContent("<p>board</p>");
    expect(screen.getByText("Updating to the new version…")).toBeVisible();
    await act(async () => {
      finish({
        revision_id: "rev-2",
        html: "<p>new look</p>",
        sdk_version: GADGET_SDK_VERSION,
        capabilities: ["data.read"],
      });
    });
    await waitFor(() =>
      expect(screen.getByTestId("gadget-frame")).toHaveTextContent("<p>new look</p>"),
    );
    expect(screen.queryByText("Updating to the new version…")).toBeNull();
  });

  it("waits for another gadget's own page instead of showing the previous gadget's", async () => {
    const { rerender } = renderWithGadgets(
      <GadgetView
        gadget={BOARD}
        sessionId="conv_1"
        onReloadGadget={vi.fn()}
        toolbarTarget={null}
        renderToolbar={(commentsButton) => commentsButton}
      />,
    );
    await screen.findByTestId("gadget-frame");
    vi.mocked(getGadgetContent).mockReturnValueOnce(new Promise(() => {}));
    rerender(
      <GadgetView
        gadget={gadgetFixture({ id: "other" })}
        sessionId="conv_1"
        onReloadGadget={vi.fn()}
        toolbarTarget={null}
        renderToolbar={(commentsButton) => commentsButton}
      />,
    );
    expect(screen.getByText("Loading gadget…")).toBeVisible();
    expect(screen.queryByTestId("gadget-frame")).toBeNull();
  });

  it("opens the comments beside it, starting from a selection", async () => {
    renderView();
    await waitFor(() => expect(frameProps?.onAddComment).toBeDefined());
    act(() =>
      frameProps?.onAddComment?.({
        text: "Ship it",
        occurrence: 1,
        rect: { x: 0, y: 0, width: 1, height: 1 },
      }),
    );
    expect(screen.getByRole("button", { name: "Comments" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByPlaceholderText("Add a comment…")).toBeInTheDocument();
  });

  it("keeps a comment whose save failed in the panel, with its selection, to try again", async () => {
    vi.mocked(createGadgetComment).mockRejectedValueOnce(new Error("The server is unreachable"));
    renderView();
    await waitFor(() => expect(frameProps?.onAddComment).toBeDefined());
    act(() =>
      frameProps?.onAddComment?.({
        text: "Ship it",
        occurrence: 1,
        rect: { x: 0, y: 0, width: 1, height: 1 },
      }),
    );
    const field = screen.getByPlaceholderText("Add a comment…");
    fireEvent.change(field, { target: { value: "Move it to Done" } });
    fireEvent.click(screen.getByRole("button", { name: "Add Comment" }));

    await waitFor(() => expect(createGadgetComment).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.getByText(/The server is unreachable/)).toBeInTheDocument());
    // The text and the selection it was on are still there.
    expect(screen.getByPlaceholderText("Add a comment…")).toHaveValue("Move it to Done");
    expect(screen.getByText("Ship it")).toBeInTheDocument();

    vi.mocked(createGadgetComment).mockResolvedValueOnce({
      id: "c1",
      body: "Move it to Done",
      status: "draft",
      anchor: { text: "Ship it", label: null, ref: null, occurrence: 1 },
      createdBy: BOARD.viewer_id,
      createdAt: 1,
      updatedAt: 1,
    });
    fireEvent.click(screen.getByRole("button", { name: "Add Comment" }));
    await waitFor(() => expect(createGadgetComment).toHaveBeenCalledTimes(2));
    const [first, retry] = vi.mocked(createGadgetComment).mock.calls;
    expect(retry).toEqual(first);
  });

  it.each([
    ["other text", "Launch date", 1],
    ["the same text elsewhere", "Ship it", 2],
  ])(
    "leaves a newer draft on %s alone when an earlier comment finishes saving",
    async (_case, nextText, nextOccurrence) => {
      let finish: (() => void) | undefined;
      vi.mocked(createGadgetComment).mockImplementationOnce(
        () =>
          new Promise((resolve) => {
            finish = () =>
              resolve({
                id: "c1",
                body: "Move it to Done",
                status: "draft",
                anchor: { text: "Ship it", label: null, ref: null, occurrence: 1 },
                createdBy: BOARD.viewer_id,
                createdAt: 1,
                updatedAt: 1,
              });
          }),
      );
      renderView();
      await waitFor(() => expect(frameProps?.onAddComment).toBeDefined());
      const select = (text: string, occurrence = 1) =>
        act(() =>
          frameProps?.onAddComment?.({
            text,
            occurrence,
            rect: { x: 0, y: 0, width: 1, height: 1 },
          }),
        );
      select("Ship it");
      fireEvent.change(screen.getByPlaceholderText("Add a comment…"), {
        target: { value: "Move it to Done" },
      });
      fireEvent.click(screen.getByRole("button", { name: "Add Comment" }));
      await waitFor(() => expect(createGadgetComment).toHaveBeenCalledTimes(1));

      // While it saves, the viewer selects other text and starts the next comment.
      select(nextText, nextOccurrence);
      fireEvent.change(screen.getByPlaceholderText("Add a comment…"), {
        target: { value: "Check this" },
      });
      await act(async () => finish?.());

      expect(screen.getByPlaceholderText("Add a comment…")).toHaveValue("Check this");
    },
  );

  it("keeps the draft read-only while its comment saves, so finishing can't erase newer text", async () => {
    let finish: ((saved: boolean) => void) | undefined;
    vi.mocked(createGadgetComment).mockImplementationOnce(
      () =>
        new Promise((resolve, reject) => {
          finish = (saved) =>
            saved
              ? resolve({
                  id: "c1",
                  body: "Move it to Done",
                  status: "draft",
                  anchor: { text: "Ship it", label: null, ref: null, occurrence: 1 },
                  createdBy: BOARD.viewer_id,
                  createdAt: 1,
                  updatedAt: 1,
                })
              : reject(new Error("offline"));
        }),
    );
    renderView();
    await waitFor(() => expect(frameProps?.onAddComment).toBeDefined());
    act(() =>
      frameProps?.onAddComment?.({
        text: "Ship it",
        occurrence: 1,
        rect: { x: 0, y: 0, width: 1, height: 1 },
      }),
    );
    const box = screen.getByPlaceholderText("Add a comment…");
    fireEvent.change(box, { target: { value: "Move it to Done" } });
    fireEvent.click(screen.getByRole("button", { name: "Add Comment" }));
    await waitFor(() => expect(createGadgetComment).toHaveBeenCalledTimes(1));

    // Same selection: the text can't change and a second Add can't be sent while it saves.
    expect(box).toHaveAttribute("readonly");
    expect(screen.getByRole("button", { name: "Saving…" })).toBeDisabled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(createGadgetComment).toHaveBeenCalledTimes(1);

    await act(async () => finish?.(false));
    // A failed save gives the text back to edit.
    expect(box).not.toHaveAttribute("readonly");
    expect(box).toHaveValue("Move it to Done");
  });

  it("loads the page again with what the viewer may do after their access changes", async () => {
    const reader = gadgetFixture({
      id: "board",
      permissions: { read: true, write_data: false, publish: false, manage: false },
    });
    vi.mocked(getGadgetContent)
      .mockResolvedValueOnce({
        revision_id: "rev-1",
        html: "<p>board</p>",
        sdk_version: GADGET_SDK_VERSION,
        capabilities: ["data.read"],
      })
      .mockResolvedValueOnce({
        revision_id: "rev-1",
        html: "<p>board</p>",
        sdk_version: GADGET_SDK_VERSION,
        capabilities: ["data.read", "data.write"],
      });
    const view = renderWithGadgets(
      <GadgetView
        gadget={reader}
        sessionId="conv_1"
        onReloadGadget={vi.fn()}
        toolbarTarget={null}
        renderToolbar={(commentsButton) => commentsButton}
      />,
    );
    await waitFor(() => expect(frameProps?.content.capabilities).toEqual(["data.read"]));

    view.rerender(
      <GadgetView
        gadget={{ ...reader, permissions: { ...reader.permissions, write_data: true } }}
        sessionId="conv_1"
        onReloadGadget={vi.fn()}
        toolbarTarget={null}
        renderToolbar={(commentsButton) => commentsButton}
      />,
    );
    await waitFor(() =>
      expect(frameProps?.content.capabilities).toEqual(["data.read", "data.write"]),
    );
    expect(getGadgetContent).toHaveBeenCalledTimes(2);
  });

  it("explains a page written for a newer SDK", async () => {
    vi.mocked(getGadgetContent).mockResolvedValue({
      revision_id: "rev-1",
      html: "",
      sdk_version: GADGET_SDK_VERSION + 1,
      capabilities: [],
    });
    renderView();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This gadget needs a newer version of Omnigent",
    );
    expect(screen.queryByTestId("gadget-frame")).toBeNull();
  });
});

describe("a gadget with nothing published", () => {
  const draft = gadgetFixture({ id: "draft", published_revision: null });

  it.each([
    ["running", "Building your gadget…"],
    ["needs_input", "Your gadget session is waiting for you."],
    ["unknown", /may still be working on it; not all of its sessions could be checked/],
    ["interrupted", "Your gadget session stopped"],
    ["idle", "Tell this session what it should build."],
  ])("shows a %s gadget session's state", async (status, text) => {
    vi.mocked(getGadgetGadgetSessionStatus).mockResolvedValue({ sessionId: "conv_1", status });
    vi.mocked(getGadget).mockResolvedValue(draft);
    renderView(draft);
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("tells viewers who can't edit that the owner hasn't published it", async () => {
    vi.mocked(getGadgetGadgetSessionStatus).mockResolvedValue({ sessionId: null, status: "none" });
    const viewOnly = { ...draft, permissions: { ...draft.permissions, publish: false } };
    vi.mocked(getGadget).mockResolvedValue(viewOnly);
    renderView(viewOnly);
    expect(
      await screen.findByText("The gadget’s owner hasn’t published a version yet."),
    ).toBeInTheDocument();
  });

  it("reloads the gadget once a version is published", async () => {
    vi.mocked(getGadgetGadgetSessionStatus).mockResolvedValue({
      sessionId: "conv_1",
      status: "running",
    });
    vi.mocked(getGadget).mockResolvedValue({ ...draft, published_revision: "rev-2" });
    const onReload = renderView(draft);
    await waitFor(() => expect(onReload).toHaveBeenCalled());
  });
});
