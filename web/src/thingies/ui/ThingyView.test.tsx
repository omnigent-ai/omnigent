import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getThingy,
  getThingyContent,
  getThingyThingySessionStatus,
  listThingyComments,
} from "@/thingies/api/thingyApi";
import type { ThingyFrameProps } from "@/thingies/host/ThingyFrame";
import { THINGY_SDK_VERSION } from "@/thingies/sdk/thingyProtocol";
import { ThingyView } from "./ThingyView";
import { renderWithThingies, thingyFixture } from "./testUtils";
import type * as ThingyApiModule from "@/thingies/api/thingyApi";

vi.mock("@/thingies/api/thingyApi", async (importOriginal) => ({
  ...(await importOriginal<typeof ThingyApiModule>()),
  getThingy: vi.fn(),
  getThingyContent: vi.fn(),
  getThingyThingySessionStatus: vi.fn(),
  listThingyComments: vi.fn(),
}));
let frameProps: ThingyFrameProps | null = null;
vi.mock("@/thingies/host/ThingyFrame", () => ({
  ThingyFrame: (props: ThingyFrameProps) => {
    frameProps = props;
    return <div data-testid="thingy-frame">{props.content.html}</div>;
  },
}));

const BOARD = thingyFixture({ id: "board" });

function renderView(thingy = BOARD, onReloadThingy = vi.fn()) {
  renderWithThingies(
    <ThingyView
      thingy={thingy}
      sessionId="conv_1"
      onReloadThingy={onReloadThingy}
      toolbarTarget={null}
      renderToolbar={(commentsButton) => commentsButton}
    />,
  );
  return onReloadThingy;
}

beforeEach(() => {
  frameProps = null;
  vi.mocked(getThingyContent).mockResolvedValue({
    revision_id: "rev-1",
    html: "<p>board</p>",
    sdk_version: THINGY_SDK_VERSION,
    capabilities: ["data.read"],
  });
  vi.mocked(listThingyComments).mockResolvedValue({ items: [], canComment: true });
});
afterEach(() => vi.clearAllMocks());

describe("a thingy beside a session", () => {
  it("runs its published page, sending its work to the session beside it", async () => {
    renderView();
    expect(await screen.findByTestId("thingy-frame")).toHaveTextContent("<p>board</p>");
    expect(frameProps?.sessionId).toBe("conv_1");
    await waitFor(() => expect(frameProps?.onAddComment).toBeDefined());
  });

  it("loads the newer version as soon as the page learns one is published", async () => {
    const onReload = renderView();
    await screen.findByTestId("thingy-frame");
    act(() => frameProps?.onRevisionChanged());
    expect(onReload).toHaveBeenCalledTimes(1);
    expect(
      screen.getByText(/A newer version of this thingy was published\. Loading it…/),
    ).toBeVisible();
    // The button stays for a reload that did not bring the new version.
    fireEvent.click(screen.getByRole("button", { name: "Load new version" }));
    expect(onReload).toHaveBeenCalledTimes(2);
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

  it("explains a page written for a newer SDK", async () => {
    vi.mocked(getThingyContent).mockResolvedValue({
      revision_id: "rev-1",
      html: "",
      sdk_version: THINGY_SDK_VERSION + 1,
      capabilities: [],
    });
    renderView();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "This thingy needs a newer version of Omnigent",
    );
    expect(screen.queryByTestId("thingy-frame")).toBeNull();
  });
});

describe("a thingy with nothing published", () => {
  const draft = thingyFixture({ id: "draft", published_revision: null });

  it.each([
    ["running", "Building your thingy…"],
    ["needs_input", "Your thingy session is waiting for you."],
    ["interrupted", "Your thingy session stopped"],
    ["idle", "Tell this session what it should build."],
  ])("shows a %s thingy session's state", async (status, text) => {
    vi.mocked(getThingyThingySessionStatus).mockResolvedValue({ sessionId: "conv_1", status });
    vi.mocked(getThingy).mockResolvedValue(draft);
    renderView(draft);
    expect(await screen.findByText(text)).toBeInTheDocument();
  });

  it("tells viewers who can't edit that the owner hasn't published it", async () => {
    vi.mocked(getThingyThingySessionStatus).mockResolvedValue({ sessionId: null, status: "none" });
    const viewOnly = { ...draft, permissions: { ...draft.permissions, publish: false } };
    vi.mocked(getThingy).mockResolvedValue(viewOnly);
    renderView(viewOnly);
    expect(
      await screen.findByText("The thingy’s owner hasn’t published a version yet."),
    ).toBeInTheDocument();
  });

  it("reloads the thingy once a version is published", async () => {
    vi.mocked(getThingyThingySessionStatus).mockResolvedValue({
      sessionId: "conv_1",
      status: "running",
    });
    vi.mocked(getThingy).mockResolvedValue({ ...draft, published_revision: "rev-2" });
    const onReload = renderView(draft);
    await waitFor(() => expect(onReload).toHaveBeenCalled());
  });
});
