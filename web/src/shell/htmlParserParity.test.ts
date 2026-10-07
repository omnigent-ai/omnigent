// Differential checks: our raw-text-aware scanner vs jsdom's DOMParser (HTML
// tokenizer). Catches tag-boundary bugs that unit cases for one path miss.

import { describe, expect, it } from "vitest";
import { injectDesignDoc, SLIDES_MSG_SOURCE } from "./codeViewerHelpers";
import {
  appendCommentBridge,
  topLevelSectionRanges,
  wireframeScreenIdForSourceOffset,
} from "./htmlCommentBridge";

const NONCE = "parity-nonce";
const DESIGN_MARK = "data-omnigent-parity-inject";

/** Tricky documents: label + HTML source. */
const TRICKY_DOCS: { label: string; html: string }[] = [
  {
    label: "quoted > and /> in section/script/style/body attributes",
    html:
      '<html><body data-x=">" data-y="/>">\n' +
      '<section data-x=">"><h1>One gt</h1></section>\n' +
      '<section data-y="/>"><h1>Two slash</h1></section>\n' +
      '<script data-x="/>">var t = "<section><h1>nope</h1></section>";</script>\n' +
      '<style data-y=">">.x{content:"<section>"}</style>\n' +
      "<section><h1>Three after raw</h1></section>\n" +
      "</body></html>",
  },
  {
    label: "self-closing script open still opens raw text",
    html:
      "<html><body>\n" +
      "<section><h1>Before</h1></section>\n" +
      '<script/>var fake = "<section><h1>inside script</h1></section>";</script>\n' +
      "<section><h1>After</h1></section>\n" +
      "</body></html>",
  },
  {
    label: "raw-text tag names inside script/style/textarea/title/comment",
    html: `<html><head><title>x &lt;section&gt;</title></head><body>
<!-- <section><h1>comment</h1></section> -->
<script>var a = "<section><h1>scr</h1></section>";</script>
<style>.x{content:"<section>"}</style>
<textarea>&lt;section&gt;<h1>ta</h1>&lt;/section&gt;</textarea>
<section><h1>Real</h1></section>
</body></html>`,
  },
  {
    label: "uppercase and mixed-case tags",
    html: `<HTML><BODY>
<SECTION><H1>One</H1></SECTION>
<Section data-screen="Home"><H1>Two</H1></Section>
</BODY></HTML>`,
  },
  {
    label: "nested sections",
    html: `<html><body>
<section><h1>Outer</h1><section><h1>Inner</h1></section></section>
<section><h1>Sibling</h1></section>
</body></html>`,
  },
  {
    label: "unclosed script at EOF",
    html: `<html><body>
<section><h1>Only</h1></section>
<script>var x = "<section><h1>eof</h1></section>";`,
  },
  {
    label: "unquoted attribute values",
    html: `<html><body>
<section data-screen=home class=slide><h1>Home</h1></section>
<section data-screen=settings><h1>Settings</h1></section>
</body></html>`,
  },
  {
    label: "scriptx and style-guide lookalikes",
    html: `<html><body>
<scriptx>not raw <section><h1>Inside scriptx</h1></section></scriptx>
<style-guide>not raw <section><h1>Inside style-guide</h1></section></style-guide>
<section><h1>Real</h1></section>
</body></html>`,
  },
  {
    label: "no head or body wrappers",
    html: `<section><h1>Frag one</h1></section><section><h1>Frag two</h1></section>`,
  },
  {
    label: "trailing fake </body> in script after real close",
    html: `<html><body>
<section><h1>One</h1></section>
<section><h1>Two</h1></section>
</body><script>var end = "</body>";</script></html>`,
  },
  {
    label: "fake <body> in head script",
    html: `<html><head><script>var start = "<body>";</script></head><body>
<section><h1>One</h1></section>
<section><h1>Two</h1></section>
</body></html>`,
  },
  {
    label: "section with quoted > truncating open tag",
    html: `<html><body>
<section data-note="a > b"><h1>Keep whole</h1><p>still in slide</p></section>
<section><h1>Next</h1></section>
</body></html>`,
  },
  {
    label: "wireframe screens with quoted /> in data-screen attrs nearby",
    html:
      "<html><body>\n" +
      '<section data-screen="home" data-title="A > B"><h1>Home</h1></section>\n' +
      '<section data-screen="settings" data-x="/>"><h1>Settings</h1></section>\n' +
      '<script data-x="/>">var s = "<section data-screen=fake>";</script>\n' +
      "</body></html>",
  },
];

function parserSections(html: string): Element[] {
  return [...new DOMParser().parseFromString(html, "text/html").querySelectorAll("body > section")];
}

function parserScreens(html: string): Element[] {
  return [
    ...new DOMParser()
      .parseFromString(html, "text/html")
      .querySelectorAll("body > section[data-screen]"),
  ];
}

function sliceTextContent(html: string, start: number, end: number): string {
  return (
    new DOMParser().parseFromString(html.slice(start, end), "text/html").body.textContent ?? ""
  );
}

describe("scanner vs DOMParser parity", () => {
  it.each(TRICKY_DOCS)("topLevelSectionRanges matches DOMParser for $label", ({ html }) => {
    const expected = parserSections(html);
    const ranges = topLevelSectionRanges(html);
    expect(ranges).toHaveLength(expected.length);
    for (let i = 0; i < expected.length; i++) {
      expect(sliceTextContent(html, ranges[i].start, ranges[i].end)).toBe(
        expected[i].textContent ?? "",
      );
    }
  });

  it.each(TRICKY_DOCS.filter((d) => d.html.includes("data-screen")))(
    "wireframe data-screen ids match DOMParser for $label",
    ({ html }) => {
      const screens = parserScreens(html);
      expect(screens.length).toBeGreaterThan(0);
      for (const el of screens) {
        const id = el.getAttribute("data-screen")!.trim();
        const token = el.querySelector("h1")?.textContent ?? id;
        const at = html.indexOf(token);
        expect(at).toBeGreaterThan(-1);
        expect(wireframeScreenIdForSourceOffset(html, at)).toBe(id);
      }
    },
  );

  it.each(TRICKY_DOCS.filter((d) => /<\/body>/i.test(d.html)))(
    "appendCommentBridge injects as a direct body child for $label",
    ({ html }) => {
      const beforeScripts = [
        ...new DOMParser().parseFromString(html, "text/html").querySelectorAll("script"),
      ].map((s) => s.textContent ?? "");
      const out = appendCommentBridge(html, NONCE);
      const doc = new DOMParser().parseFromString(out, "text/html");
      const bridge = [...doc.querySelectorAll("body script")].find(
        (s) => s.getAttribute("data-omni-nonce") === NONCE,
      );
      expect(bridge).toBeTruthy();
      expect(bridge!.parentElement?.tagName.toLowerCase()).toBe("body");
      const afterDeckScripts = [...doc.querySelectorAll("script")]
        .filter((s) => s.getAttribute("data-omni-nonce") !== NONCE)
        .map((s) => s.textContent ?? "");
      expect(afterDeckScripts).toEqual(beforeScripts);
    },
  );

  it.each(TRICKY_DOCS.filter((d) => /<\/body>/i.test(d.html)))(
    "injectDesignDoc injects as a direct body child for $label",
    ({ html }) => {
      const beforeScripts = [
        ...new DOMParser().parseFromString(html, "text/html").querySelectorAll("script"),
      ].map((s) => s.textContent ?? "");
      const injection = `<style ${DESIGN_MARK}>x</style><script ${DESIGN_MARK}>var __parity="${SLIDES_MSG_SOURCE}";</script>`;
      const out = injectDesignDoc(html, "", injection);
      const doc = new DOMParser().parseFromString(out, "text/html");
      const injected = doc.querySelector(`body > script[${DESIGN_MARK}]`);
      expect(injected).toBeTruthy();
      expect(injected!.parentElement?.tagName.toLowerCase()).toBe("body");
      // Preview head may add its own script; every original deck script text must remain.
      const afterTexts = [...doc.querySelectorAll("script")].map((s) => s.textContent ?? "");
      for (const text of beforeScripts) {
        expect(afterTexts).toContain(text);
      }
    },
  );
});
