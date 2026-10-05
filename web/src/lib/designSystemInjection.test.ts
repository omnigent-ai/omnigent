import { describe, expect, it } from "vitest";
import type { KitFile } from "@/shell/codeViewerHelpers";
import { readFixture, readFixtureFile } from "@/test/designSystemFixture";
import {
  DS_ASSET_MAX_BYTES,
  DS_DECK_MAX_BYTES,
  injectDesignSystem,
  processDesignSystemCss,
  resolveDsPath,
  rewriteDsReferences,
} from "./designSystemInjection";

const text = (content: string): KitFile => ({ encoding: "utf-8", content, bytes: content.length });
const reader =
  (files: Record<string, KitFile>) =>
  async (rel: string): Promise<KitFile | null> =>
    files[rel] ?? null;
const svgUri = (svg: string) => `data:image/svg+xml;base64,${btoa(svg)}`;

describe("resolveDsPath", () => {
  it("accepts a relative image or font path, with or without the ds: prefix", () => {
    expect(resolveDsPath("ds:assets/brand/logo-white.svg")).toBe("assets/brand/logo-white.svg");
    expect(resolveDsPath("fonts/Sans.WOFF2")).toBe("fonts/Sans.WOFF2");
  });

  it.each([
    "ds:../secret.svg",
    "ds:assets/../../x.png",
    "ds:./logo.svg",
    "ds:/etc/logo.svg",
    "ds:https://evil.example/x.svg",
    "ds:assets//logo.svg",
    "ds:",
  ])("rejects %s as outside the design system", (ref) => {
    expect(() => resolveDsPath(ref)).toThrow("must be a relative path inside the design system");
  });

  it.each(["ds:templates/deck.html", "ds:app.js", "ds:colors_and_type.css"])(
    "rejects %s as not an image or font",
    (ref) => {
      expect(() => resolveDsPath(ref)).toThrow("must be an image or font");
    },
  );
});

describe("processDesignSystemCss", () => {
  const asset = async (p: string) => `data:x/${p}`;

  it("strips @import, inlines relative urls, keeps data:, drops remote urls", async () => {
    const css = await processDesignSystemCss(
      [
        '@import url("https://x.example/a.css");',
        "@import 'b.css' screen;",
        '@font-face{font-family:A;src:url("./fonts/a.woff2") format("woff2")}',
        ".a{background:url(ds:assets/a.png)}",
        ".b{background:url('data:image/png;base64,AA==')}",
        ".c{background:url(https://tracker.example/p.gif)}",
        ".d{background:url(//cdn.example/p.gif)}",
      ].join("\n"),
      asset,
    );
    expect(css).not.toMatch(/@import/i);
    expect(css).toContain('src:url("data:x/fonts/a.woff2") format("woff2")');
    expect(css).toContain(".a{background:url(data:x/assets/a.png)}");
    expect(css).toContain("url('data:image/png;base64,AA==')");
    expect(css).toContain(".c{background:none}");
    expect(css).toContain(".d{background:none}");
  });

  it("rejects a stylesheet that closes <style>", async () => {
    await expect(processDesignSystemCss("a{}</STYLE ><script>", asset)).rejects.toThrow(
      'colors_and_type.css must not contain "</style"',
    );
  });

  it("rejects </style that only forms once @import is stripped", async () => {
    await expect(
      processDesignSystemCss("a{} </@import;style><script>alert(1)</script>", asset),
    ).rejects.toThrow('colors_and_type.css must not contain "</style"');
  });

  it.each([
    ['@\\69mport "https://evil.example/x.css";', "must not contain backslash escapes"],
    [".a{background:u\\72l(https://evil.example/a)}", "must not contain backslash escapes"],
    ['.a{background:image-set("https://evil.example/a.png" 1x)}', "must not use image-set()"],
    [
      '.a{background:-webkit-image-set("https://evil.example/a.png" 1x)}',
      "must not use image-set()",
    ],
    ["@@import;import url(https://evil.example/x.css);", "must not use @import"],
    [".a{background:url(https://evil.example/a b)}", "has a url() that is not a data: URI"],
  ])("rejects %s", async (input, reason) => {
    await expect(processDesignSystemCss(input, asset)).rejects.toThrow(
      `colors_and_type.css ${reason}`,
    );
  });

  it.each([
    '.a{background:url("https://evil.example/a)b")}',
    `.a{background:url("https://e/x'y")}`,
  ])("drops the quoted remote url in %s", async (input) => {
    expect(await processDesignSystemCss(input, asset)).toBe(".a{background:none}");
  });

  it("applies the synthetic design-system stylesheet", async () => {
    const css = await processDesignSystemCss(
      readFixtureFile("colors_and_type.css")!.content,
      asset,
    );
    expect(css).toContain('url("data:x/fonts/fixture-sans.woff2")');
    expect(css).toContain("url(data:x/assets/logo.svg)");
  });

  it("rejects a url that escapes the folder", async () => {
    await expect(processDesignSystemCss(".a{background:url(../x.png)}", asset)).rejects.toThrow(
      "../x.png must be a relative path inside the design system",
    );
  });
});

describe("rewriteDsReferences", () => {
  it("rewrites ds: in src, href, and url()", async () => {
    const out = await rewriteDsReferences(
      [
        '<img src="ds:assets/a.svg">',
        "<link href='ds:fonts/b.woff2'>",
        "<img src=ds:assets/c.png alt=x>",
        '<div style="background:url(ds:assets/a.svg)"></div>',
        "<style>.x{background-image:url('ds:assets/a.svg')}</style>",
        '<a href="https://example.com/ds:x.svg">kept</a>',
        "<p>ds:assets/a.svg stays as text</p>",
      ].join("\n"),
      async (p) => `data:x/${p}`,
    );
    expect(out).toContain('<img src="data:x/assets/a.svg">');
    expect(out).toContain('<link href="data:x/fonts/b.woff2">');
    expect(out).toContain('<img src="data:x/assets/c.png" alt=x>');
    expect(out).toContain('style="background:url(data:x/assets/a.svg)"');
    expect(out).toContain("url('data:x/assets/a.svg')");
    expect(out).toContain('href="https://example.com/ds:x.svg"');
    expect(out).toContain("<p>ds:assets/a.svg stays as text</p>");
  });

  it("fails on a reference outside the design system", async () => {
    await expect(
      rewriteDsReferences('<img src="ds:../../.ssh/id.png">', async () => "data:,"),
    ).rejects.toThrow("must be a relative path inside the design system");
  });
});

describe("injectDesignSystem", () => {
  const deck = '<section><img src="ds:assets/logo.svg"></section>';

  it("returns the fixture's style and the deck with assets inlined", async () => {
    const { style, content } = await injectDesignSystem(deck, readFixture);
    const logo = svgUri(readFixtureFile("assets/logo.svg")!.content);
    const font = `data:font/woff2;base64,${readFixtureFile("fonts/fixture-sans.woff2")!.content}`;
    expect(content).toBe(`<section><img src="${logo}"></section>`);
    expect(style.startsWith("<style data-omnigent-design-system>")).toBe(true);
    expect(style).toContain("--fx-primary: #0b5fff");
    expect(style).toContain(`src: url("${font}") format("woff2")`);
    expect(style).toContain(`background-image: url(${logo})`);
    expect(style).not.toMatch(/@import|!important|fonts\.example/);
  });

  it("has no style without colors_and_type.css", async () => {
    const svg = text("<svg/>");
    const result = await injectDesignSystem(deck, reader({ "assets/logo.svg": svg }));
    expect(result).toEqual({
      style: "",
      content: `<section><img src="${svgUri("<svg/>")}"></section>`,
    });
  });

  it("errors on a missing asset", async () => {
    await expect(injectDesignSystem(deck, reader({}))).rejects.toThrow(
      "assets/logo.svg not found in the design system",
    );
  });

  it("caps each asset at 2 MB after encoding", async () => {
    const big = "x".repeat(Math.ceil((DS_ASSET_MAX_BYTES * 3) / 4));
    await expect(
      injectDesignSystem(deck, reader({ "assets/logo.svg": text(big) })),
    ).rejects.toThrow("assets/logo.svg is larger than 2 MB");
  });

  it("caps all assets for one deck at 20 MB, counting a shared asset once", async () => {
    const piece = "x".repeat(Math.floor(DS_ASSET_MAX_BYTES * 0.7));
    const files: Record<string, KitFile> = {};
    const refs: string[] = [];
    const count = Math.ceil(DS_DECK_MAX_BYTES / (piece.length * (4 / 3)));
    for (let i = 0; i < count; i++) {
      files[`assets/p${i}.svg`] = text(piece);
      refs.push(`<img src="ds:assets/p${i}.svg">`);
    }
    await expect(injectDesignSystem(refs.join(""), reader(files))).rejects.toThrow(
      "design-system assets are larger than 20 MB",
    );
    const repeated = Array(count).fill('<img src="ds:assets/p0.svg">').join("");
    await expect(injectDesignSystem(repeated, reader(files))).resolves.toBeTruthy();
  });

  it("fails on more than 200 distinct assets before reading anything", async () => {
    const reads: string[] = [];
    const deck201 = Array.from({ length: 201 }, (_, i) => `<img src="ds:a/p${i}.svg">`).join("");
    await expect(
      injectDesignSystem(deck201, async (p) => (reads.push(p), text("<svg/>"))),
    ).rejects.toThrow("references more than 200 design-system assets");
    expect(reads).toEqual([]);
  });

  it("counts stylesheet assets toward the 200 cap before reading them", async () => {
    const reads: string[] = [];
    const deck200 = Array.from({ length: 200 }, (_, i) => `<img src="ds:a/p${i}.svg">`).join("");
    const files = { "colors_and_type.css": text(".x{background:url(a/extra.svg)}") };
    await expect(
      injectDesignSystem(deck200, async (p) => (reads.push(p), reader(files)(p))),
    ).rejects.toThrow("references more than 200 design-system assets");
    expect(reads).toEqual(["colors_and_type.css"]);
  });

  it("reads assets one at a time and stops once over 20 MB", async () => {
    const piece = text("x".repeat(Math.floor(DS_ASSET_MAX_BYTES * 0.7)));
    const count = Math.ceil(DS_DECK_MAX_BYTES / (piece.content.length * (4 / 3)));
    const refs = Array.from({ length: count * 2 }, (_, i) => `<img src="ds:a/p${i}.svg">`);
    const reads: string[] = [];
    let inFlight = 0;
    let maxInFlight = 0;
    const read = async (p: string) => {
      reads.push(p);
      maxInFlight = Math.max(maxInFlight, ++inFlight);
      await Promise.resolve();
      inFlight--;
      return p === "colors_and_type.css" ? null : piece;
    };
    await expect(injectDesignSystem(refs.join(""), read)).rejects.toThrow(
      "design-system assets are larger than 20 MB",
    );
    expect(maxInFlight).toBe(1);
    expect(reads.length).toBeLessThanOrEqual(count + 1);
  });

  it("propagates a failed read", async () => {
    await expect(
      injectDesignSystem(deck, () => Promise.reject(new Error("403 Forbidden"))),
    ).rejects.toThrow("403 Forbidden");
  });
});
