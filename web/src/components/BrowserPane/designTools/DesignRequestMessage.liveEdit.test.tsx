import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { buildLiveEditPrompt } from "./core/designPrompts";
import { DesignRequestBody } from "./DesignRequestMessage";
import { clipDesignRequest, parseDesignRequest, type DesignRequest } from "./parseDesignRequest";

const edits = (count: number, size = 1000): DesignRequest => ({
  kind: "edits",
  request: "Make these text changes",
  page: "app.example.com",
  edits: Array.from({ length: count }, (_, index) => ({
    element: `p#e${index}`,
    before: "b".repeat(size),
    after: "a".repeat(size),
  })),
});

describe("Live Edit's chat card", () => {
  it("reads back each change from the message, exactly", () => {
    const before = 'Say "hi"\nthen → go';
    const text = buildLiveEditPrompt(
      [
        { element: { tag: "h1", id: "#title" }, before, after: "Hello" },
        { element: { tag: "p" }, before: "One", after: "  " },
      ],
      "http://localhost:5173/orders/12",
    );
    expect(parseDesignRequest(text)).toEqual({
      kind: "edits",
      request: "Make these text changes in the source; I made them in the browser preview.",
      page: "localhost:5173",
      edits: [
        { element: "h1#title", before, after: "Hello" },
        { element: "p", before: "One", after: "  " },
      ],
    });
  });

  it("lists the changes before and after, without the message's fixed words", async () => {
    render(
      <DesignRequestBody
        request={{
          kind: "edits",
          request: "Make these text changes",
          page: "localhost:5173",
          edits: [{ element: "p#blurb", before: "Old", after: "New\ntwo" }],
        }}
      />,
    );
    expect(await screen.findByText("Old")).toHaveClass("line-through");
    expect(screen.getByText((_, node) => node?.textContent === "New\ntwo")).toHaveClass(
      "whitespace-pre-wrap",
    );
    expect(screen.getByText("p#blurb")).toBeInTheDocument();
    expect(screen.getByText("1 edit on localhost:5173")).toBeInTheDocument();
    expect(screen.queryByText("Make these text changes")).toBeNull();
  });

  it("keeps a collapsed card's changes bounded, still counting them all", async () => {
    const long = edits(50);
    const clipped = clipDesignRequest(long, 12_000);
    const shown = [clipped.request, ...clipped.edits!.flatMap((edit) => [edit.before, edit.after])];
    expect(shown.join("").length).toBeLessThanOrEqual(12_000);
    expect(clipped.edits!.length).toBeLessThan(50);
    render(<DesignRequestBody request={clipped} />);
    await screen.findByTestId("design-request");
    expect(screen.getAllByRole("listitem")).toHaveLength(clipped.edits!.length);
    expect(screen.getByText("50 edits on app.example.com")).toBeInTheDocument();
  });
});
