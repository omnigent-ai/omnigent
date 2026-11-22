import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  type GadgetComment,
  createGadgetComment,
  listGadgetComments,
  updateGadgetComment,
} from "@/gadgets/api/gadgetApi";
import { gadgetFixture } from "./testUtils";
import { gadgetCommentsMessage, useGadgetComments } from "./useGadgetComments";
import type * as GadgetApiModule from "@/gadgets/api/gadgetApi";
import { GADGET_SKILL_INSTRUCTION } from "@/gadgets/host/gadgetBuild";

vi.mock("@/gadgets/api/gadgetApi", async (importOriginal) => ({
  ...(await importOriginal<typeof GadgetApiModule>()),
  listGadgetComments: vi.fn(),
  createGadgetComment: vi.fn(),
  updateGadgetComment: vi.fn(),
}));

const BOARD = gadgetFixture({ id: "board", name: "Launch board" });

function comment(id: string, overrides: Partial<GadgetComment> = {}): GadgetComment {
  return {
    id,
    body: `Body of ${id}`,
    status: "draft",
    anchor: { text: "Ship it", label: "Card", ref: "card-1", occurrence: 1 },
    createdBy: "lee@example.com",
    createdAt: 1_700_000_000,
    updatedAt: 1_700_000_000,
    ...overrides,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

afterEach(() => vi.clearAllMocks());

describe("gadget comments", () => {
  it("tells the session what each comment points at", () => {
    expect(
      gadgetCommentsMessage(BOARD, [
        comment("c1"),
        comment("c2", {
          anchor: { text: "Q3", label: null, ref: null },
          createdBy: "",
          body: "a\nb",
        }),
      ]),
    ).toBe(
      [
        `${GADGET_SKILL_INSTRUCTION} Address these comments on the gadget "Launch board" (gadget_id board). Change its page with sys_gadget_revision_create and sys_gadget_publish, or its records, as each asks. They are already marked addressed; sys_gadget_comment_list has them all.`,
        "",
        '1. On "Ship it" (Card, ref card-1), lee@example.com wrote:',
        "   Body of c1",
        '2. On "Q3", someone wrote:',
        "   a",
        "   b",
      ].join("\n"),
    );
  });

  it("comments on a selection and opens the panel", async () => {
    vi.mocked(listGadgetComments).mockResolvedValue({ items: [], canComment: true });
    vi.mocked(createGadgetComment).mockResolvedValue(comment("c3"));
    const { result } = renderHook(() => useGadgetComments(BOARD, undefined), { wrapper });
    await waitFor(() => expect(result.current.canComment).toBe(true));

    act(() =>
      result.current.startComment({
        text: "Ship it",
        label: "Card",
        ref: "card-1",
        occurrence: 2,
        rect: { x: 0, y: 0, width: 1, height: 1 },
      }),
    );
    expect(result.current.open).toBe(true);
    expect(result.current.panel.activeSelection).toMatchObject({ anchor_content: "Ship it" });
    act(() => result.current.panel.onAddComment("Move it to Done"));

    await waitFor(() =>
      expect(createGadgetComment).toHaveBeenCalledWith("board", {
        body: "Move it to Done",
        anchor: { text: "Ship it", label: "Card", ref: "card-1", occurrence: 2 },
        operationId: expect.any(String),
      }),
    );
    expect(result.current.panel.canAddress).toBe(false);
  });

  it("sends the open comments to the session, then marks them addressed", async () => {
    vi.mocked(listGadgetComments).mockResolvedValue({
      items: [comment("c1"), comment("c2", { status: "addressed" })],
      canComment: true,
    });
    vi.mocked(updateGadgetComment).mockResolvedValue(comment("c1", { status: "addressed" }));
    const address = vi.fn(async (_message: string) => undefined);
    const { result } = renderHook(() => useGadgetComments(BOARD, address), { wrapper });
    await waitFor(() => expect(result.current.draftCount).toBe(1));
    expect(result.current.panel.addressedComments.map((item) => item.id)).toEqual(["c2"]);

    act(() => result.current.panel.onAddressAll());
    await waitFor(() =>
      expect(updateGadgetComment).toHaveBeenCalledWith("board", "c1", { status: "addressed" }),
    );
    expect(address).toHaveBeenCalledWith(expect.stringContaining("Body of c1"));
    expect(address.mock.calls[0][0]).not.toContain("Body of c2");
  });

  it("leaves the viewer's own comments theirs to edit or delete, as in single-user mode", async () => {
    const local = gadgetFixture({ id: "board", owner_id: "local", viewer_id: "local" });
    vi.mocked(listGadgetComments).mockResolvedValue({
      items: [comment("mine", { createdBy: "local" }), comment("theirs")],
      canComment: true,
    });
    const { result } = renderHook(() => useGadgetComments(local, undefined), { wrapper });
    await waitFor(() => expect(result.current.draftCount).toBe(2));
    // The panel reads no author as the viewer ("You"), whose comments it lets them change.
    expect(result.current.panel.comments.map((item) => item.created_by)).toEqual([
      null,
      "lee@example.com",
    ]);
    expect(result.current.panel.readOnlyMessage).toMatch(/gadget/);
  });
});
