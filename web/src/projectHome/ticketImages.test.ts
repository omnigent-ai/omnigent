import { describe, expect, it } from "vitest";
import { splitTicketImages, ticketImageMarkdown } from "./ticketImages";

const url = `/v1/projects/${"a".repeat(32)}/ticket-attachments/${"b".repeat(32)}/content`;

describe("splitTicketImages", () => {
  it("splits uploaded image references out in order", () => {
    expect(splitTicketImages(`Before\n${ticketImageMarkdown("shot.png", url)}\nAfter`)).toEqual([
      { kind: "markdown", text: "Before\n" },
      { kind: "image", alt: "shot.png", path: url },
      { kind: "markdown", text: "\nAfter" },
    ]);
  });

  it("leaves external images and plain text as markdown", () => {
    const body = "![x](https://example.com/x.png)";
    expect(splitTicketImages(body)).toEqual([{ kind: "markdown", text: body }]);
  });
});

describe("ticket videos", () => {
  it("marks an uploaded video's link and splits it out as a video", () => {
    const markdown = ticketImageMarkdown("clip.mp4", url, "video/mp4");
    expect(markdown).toBe(`![clip.mp4](${url}?type=video)`);
    expect(splitTicketImages(`Look:\n${markdown}`)).toEqual([
      { kind: "markdown", text: "Look:\n" },
      { kind: "video", alt: "clip.mp4", path: url },
    ]);
  });

  it("keeps images unmarked, whatever their name", () => {
    expect(ticketImageMarkdown("clip.mp4.png", url, "image/png")).toBe(`![clip.mp4.png](${url})`);
    expect(ticketImageMarkdown("shot.png", url)).toBe(`![shot.png](${url})`);
  });
});
