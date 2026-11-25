import { describe, expect, it } from "vitest";
import { markdownPlainText } from "./markdownPlainText";

describe("markdownPlainText", () => {
  it("drops emphasis, code and link syntax", () => {
    expect(
      markdownPlainText(
        "**Aaron: approve it?** It lets the `Lead` ask. See [the design](https://example.com), _two_ options and ~~old~~ notes.",
      ),
    ).toBe("Aaron: approve it? It lets the Lead ask. See the design, two options and old notes.");
  });

  it("flattens headings, quotes, lists and fenced code onto one line", () => {
    expect(
      markdownPlainText("## Status\n> quoted\n- first\n- [x] done\n1. second\n```ts\nconst a = 1;\n```"),
    ).toBe("Status quoted first done second const a = 1;");
  });

  it("keeps snake_case identifiers and unpaired marks in plain text", () => {
    expect(markdownPlainText("Set `max_tokens` and keep file_name_v2 as is * 2")).toBe(
      "Set max_tokens and keep file_name_v2 as is * 2",
    );
  });

  it("removes a dangling bold marker cut off by the server's preview", () => {
    expect(markdownPlainText("**Aaron: approve Lead-to-Lead messaging")).toBe(
      "Aaron: approve Lead-to-Lead messaging",
    );
  });
});
