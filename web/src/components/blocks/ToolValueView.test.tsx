import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { absoluteTime } from "@/lib/relativeTime";
import { ToolValueView } from "./ToolValueView";

afterEach(cleanup);

function renderValue(value: unknown) {
  // Markdown links look up workspace files through React Query, as in the app.
  return render(
    <QueryClientProvider
      client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}
    >
      <ToolValueView value={value} />
    </QueryClientProvider>,
  ).container;
}

describe("ToolValueView", () => {
  it("shows nesting past three levels as compact JSON", () => {
    const container = renderValue({ a: { b: { c: { d: 1 } } } });
    expect(container.textContent).toContain('{"d":1}');
  });

  it("frames the outermost fields as a table and keeps nested fields plain", () => {
    // WHY: the expanded view should read as a table without boxing every nested value.
    const container = renderValue({ id: "OMNI-1", owner: { name: "Ada", team: "web" } });
    const [outer, nested] = Array.from(container.querySelectorAll("dl"));
    expect(outer).toHaveClass("rounded-md", "border");
    expect(nested).not.toHaveClass("border");
    // Each outer row carries its own divider, under the label and the value alike.
    const rows = Array.from(outer.children);
    expect(rows).toHaveLength(2);
    expect(rows[1]).toHaveClass("border-t");
    expect(rows[1].querySelector("dt")?.textContent).toBe("Owner");

    cleanup();
    const list = renderValue([{ title: "Plan" }, { title: "Ship" }]).querySelector("ol");
    expect(list).toHaveClass("rounded-md", "border", "divide-y");
  });

  it("caps long field lists and record lists with a count of the rest", () => {
    // WHY: a huge tool payload must not mount one DOM row per entry.
    const fields = Object.fromEntries(
      Array.from({ length: 60 }, (_, index) => [`k${index}`, index]),
    );
    renderValue(fields);
    expect(screen.getByText("and 10 more")).toBeInTheDocument();
    cleanup();

    const records = Array.from({ length: 55 }, (_, index) => ({ title: `Issue ${index}` }));
    renderValue({ issues: records });
    expect(screen.getByText("and 5 more")).toBeInTheDocument();
  });

  it("renders a URL as an external link and an ISO time in local time", () => {
    renderValue({ url: "https://example.com/a", updatedAt: "2026-10-26T09:30:00Z" });
    const link = screen.getByRole("link", { name: "https://example.com/a" });
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    const time = screen.getByText(absoluteTime(Date.parse("2026-10-26T09:30:00Z")));
    expect(time).toHaveAttribute("datetime", "2026-10-26T09:30:00Z");
  });

  it("shows short value lists as chips", () => {
    renderValue({ labels: ["web", "ux"] });
    expect(screen.getByText("web")).toHaveAttribute("data-slot", "badge");
  });

  it("keeps a record's text id and collapses its long extra values to one line", () => {
    const description = "first line\nsecond line";
    const container = renderValue([
      { id: "OMNI-12243", title: "Tool cards", notes: description, status: "In Review" },
    ]);
    expect(container.textContent).toContain("ID OMNI-12243");
    expect(container.textContent).toContain("Notes first line second line");
    expect(container.querySelector("pre")).toBeNull();
  });

  it("renders a long description as markdown in the interface font", async () => {
    // Link syntax in a description renders as a link, not raw `[text](url)`.
    const description =
      "Follow-up to [#11607](<https://github.com/omnigent-ai/omnigent/pull/11607>).\n\n1. Show counts";
    const container = renderValue({ identifier: "OMNI-13632", description });
    const prose = screen.getByTestId("tool-prose-value");
    const link = await screen.findByRole("link", { name: "#11607" });
    expect(prose.contains(link)).toBe(true);
    expect(link).toHaveAttribute("href", "https://github.com/omnigent-ai/omnigent/pull/11607");
    expect(prose.textContent).not.toContain("](");
    expect(prose.querySelector(".font-mono, pre, code")).toBeNull();
    expect(container.querySelector("pre")).toBeNull();
  });

  it("keeps file content and other code in the code face", () => {
    const container = renderValue({ path: "notes.md", content: "# Notes\n- [x](y)\n" });
    expect(screen.queryByTestId("tool-prose-value")).toBeNull();
    expect(container.querySelector("pre")).toHaveClass("font-mono");
    expect(container.querySelector("pre")?.textContent).toBe("# Notes\n- [x](y)\n");
  });

  it("shows finished prose as sent, without closing unmatched markdown", async () => {
    const description = "Search with the glob **/*.ts\nthen [open the PR](https://x/pull/1)";
    renderValue({ description });
    const prose = screen.getByTestId("tool-prose-value");
    await screen.findByRole("link", { name: "open the PR" });
    expect(prose.textContent).toContain("**/*.ts");
    expect(prose.querySelector("strong")).toBeNull();
  });

  it("shows raw HTML in prose as written, so it can't hide text or run", async () => {
    // WHY: markup, comments or an unclosed tag in a tool's text must not hide part of it.
    renderValue({
      description:
        "<!-- Describe your change -->\n## Summary\n\nSee [docs](https://x/docs) or " +
        '[this](javascript:alert(1))\nLook <img src="x" onerror="alert(1)"> here\n\n' +
        "<details><summary>Logs</summary>\n\nAll green\n</details>",
    });
    const prose = await screen.findByTestId("tool-prose-value");
    await screen.findByRole("link", { name: "docs" });
    const hrefs = [...prose.querySelectorAll("a[href]")].map((link) => link.getAttribute("href"));
    expect(hrefs).toEqual(["https://x/docs"]);
    expect(prose.querySelector("img, details, [onerror]")).toBeNull();
    expect(prose.textContent).toContain("<!-- Describe your change -->");
    expect(prose.textContent).toContain('<img src="x" onerror="alert(1)">');
    cleanup();
    renderValue({ description: "Summary\n<pre\nStill visible\n\nmore text" });
    const unclosed = await screen.findByTestId("tool-prose-value");
    expect(unclosed.textContent).toContain("Still visible");
    expect(unclosed.textContent).toContain("more text");
  });

  it("keeps file-edit text literal, alone or in a batch of edits", () => {
    const oldText = "## Setup\n\n- Run `just dev`\n";
    const newText = "## Setup\n\n- Run **the app**\n";
    const container = renderValue({ path: "README.md", oldText, newText });
    expect(screen.queryByTestId("tool-prose-value")).toBeNull();
    const blocks = [...container.querySelectorAll("pre")].map((pre) => pre.textContent);
    expect(blocks).toEqual([oldText, newText]);
    cleanup();
    const batch = renderValue({ path: "README.md", edits: [{ oldText, newText }] });
    expect(screen.queryByTestId("tool-prose-value")).toBeNull();
    expect(batch.textContent).toContain("Run **the app**");
  });

  it("judges list items by the list's key, not their index", async () => {
    const script = "# set up the environment\npip install -r requirements.txt\n- **note**: slow\n";
    const container = renderValue({ commands: [script, script] });
    expect(screen.queryByTestId("tool-prose-value")).toBeNull();
    expect([...container.querySelectorAll("pre")].map((pre) => pre.textContent)).toEqual([
      script,
      script,
    ]);
    cleanup();
    // Long enough to skip the chip list, so each item goes through the prose check.
    const review =
      "Looks good to me overall.\n\n- [see the plan](https://x/a)\n- ship after CI passes";
    renderValue({ comments: [review, review] });
    expect(await screen.findAllByTestId("tool-prose-value")).toHaveLength(2);
    expect(screen.getAllByRole("link", { name: "see the plan" })).toHaveLength(2);
  });

  it("labels inherited object names as plain words", () => {
    renderValue({ constructor: "x" });
    expect(screen.getByText("Constructor")).toBeInTheDocument();
  });
});
