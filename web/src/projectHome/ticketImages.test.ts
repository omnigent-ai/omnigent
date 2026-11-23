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
