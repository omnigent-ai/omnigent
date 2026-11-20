// The snapshot script the web tab and the desktop share (electron/src/browserActionScript.mjs).
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import { buildBrowserActionScript } from "../../electron/src/browserActionScript.mjs";

beforeAll(() => {
  // jsdom lays nothing out: every element gets a box, as rendered ones have, except a
  // display: contents one, which has none of its own (a 0x0 rect, as in a browser). A
  // test places one with data-x / data-y (viewport coordinates).
  Element.prototype.getBoundingClientRect = function box(this: Element) {
    const size = getComputedStyle(this).display === "contents" ? 0 : 10;
    const x = Number(this.getAttribute("data-x") ?? 0);
    const y = Number(this.getAttribute("data-y") ?? 0);
    return {
      x,
      y,
      width: size,
      height: size,
      top: y,
      left: x,
      right: x + size,
      bottom: y + size,
    } as DOMRect;
  };
  // Nor does it render text: innerText is what textContent says here.
  Object.defineProperty(HTMLElement.prototype, "innerText", {
    configurable: true,
    get(this: HTMLElement) {
      return this.textContent ?? "";
    },
  });
});

afterEach(() => {
  document.body.innerHTML = "";
});

/** The whole tree, its viewport line first. */
function fullTree(): string {
  const raw = new Function(`return ${buildBrowserActionScript("snapshot")}`)() as string;
  return (JSON.parse(raw) as { tree: string }).tree;
}

/** The tree's element lines (after its viewport line). */
function snapshot(): string {
  const [viewport, ...lines] = fullTree().split("\n");
  expect(viewport).toMatch(/^viewport: \d+–\d+ of \d+px$/);
  return lines.join("\n");
}

describe("the page snapshot", () => {
  it("walks through display: contents boxes to what they hold", () => {
    document.body.innerHTML =
      '<main><div style="display: contents"><div style="display: contents">' +
      "<h2>Store</h2><button>Pizza</button></div></div></main>";
    expect(snapshot()).toBe(
      [
        '- main "StorePizza" [ref=1]',
        '  - heading "Store" [ref=2]',
        '  - button "Pizza" [ref=3]',
      ].join("\n"),
    );
  });

  it("says what controls are set to, but never a password", () => {
    document.body.innerHTML =
      '<input type="checkbox" aria-label="Agree" checked>' +
      '<input type="radio" aria-label="Small">' +
      '<div role="switch" aria-label="Dark" aria-checked="true"></div>' +
      '<input aria-label="Name" value="Ada">' +
      '<textarea aria-label="Note">Hi</textarea>' +
      '<input type="password" aria-label="Secret" value="hunter2">';
    const tree = snapshot();
    expect(tree).toContain('- checkbox "Agree" [checked] [ref=1]');
    expect(tree).toContain('- radio "Small" [unchecked] [ref=2]');
    expect(tree).toContain('- switch "Dark" [checked] [ref=3]');
    expect(tree).toContain('- textbox "Name" [value="Ada"] [ref=4]');
    expect(tree).toContain('- textbox "Note" [value="Hi"] [ref=5]');
    expect(tree).toContain('- textbox "Secret" [ref=6]');
    expect(tree).not.toContain("hunter2");
  });

  it("never gives the value of a field the page masks as a secret", () => {
    document.body.innerHTML =
      '<textarea aria-label="Masked" style="-webkit-text-security: disc">sk-live-1</textarea>' +
      '<textarea aria-label="Clear" style="color: transparent">sk-live-2</textarea>' +
      '<input aria-label="Shown password" autocomplete="current-password" value="sk-live-3">' +
      '<input aria-label="Code" autocomplete="one-time-code" value="123456">' +
      '<input aria-label="Plain" value="visible">';
    // jsdom drops -webkit-text-security; a browser's computed style keeps it.
    const masked = document.querySelector('[aria-label="Masked"]');
    const computed = window.getComputedStyle.bind(window);
    const spy = vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
      const style = computed(element);
      if (element !== masked) return style;
      return new Proxy(style, {
        get: (target, key) =>
          key === "getPropertyValue"
            ? (name: string) =>
                name === "-webkit-text-security" ? "disc" : target.getPropertyValue(name)
            : Reflect.get(target, key, target),
      });
    });
    const tree = snapshot();
    spy.mockRestore();
    expect(tree).not.toMatch(/sk-live|123456/);
    expect(tree).toContain('- textbox "Masked" [ref=1]');
    expect(tree).toContain('- textbox "Plain" [value="visible"] [ref=5]');
  });

  it("names by what a referenced label shows, and by a deep label's whole text", () => {
    document.body.innerHTML =
      '<span id="pay">Checkout <span hidden>Cancel</span></span><button aria-labelledby="pay">Checkout</button>' +
      '<img id="logo" alt="Home"><a href="/" aria-labelledby="logo"></a>' +
      "<button>Delete <div><div><div><div><div>account</div></div></div></div></div></button>";
    // jsdom renders nothing: innerText leaves out what a browser wouldn't show.
    const pay = document.getElementById("pay")!;
    Object.defineProperty(pay, "innerText", { value: "Checkout" });
    const tree = snapshot();
    expect(tree).toContain('- button "Checkout" [ref=');
    expect(tree).toContain('- link "Home" [ref=');
    expect(tree).toContain('- button "Delete account" [ref=');
  });

  it("keeps a display: contents control's role and ref", () => {
    document.body.innerHTML = '<a href="/orders" style="display: contents"><span>Orders</span></a>';
    expect(snapshot()).toBe('- link "Orders" [ref=1]');
  });

  it("gives a field's whole value, not one cut at a name's length", () => {
    const routing = "x".repeat(110);
    document.body.innerHTML = `<input aria-label="Route" value="${routing}">`;
    expect(snapshot()).toBe(`- textbox "Route" [value="${routing}"] [ref=1]`);
  });

  it("names a link by its text however deep it sits, or by aria-labelledby", () => {
    document.body.innerHTML =
      '<nav><a href="/a"><div><div><div><div><div><span>Orders</span></div></div></div></div></div></a>' +
      '<span id="lbl">Account</span><a href="/b" aria-labelledby="lbl"></a></nav>';
    const tree = snapshot();
    expect(tree).toContain('- link "Orders" [ref=2]');
    expect(tree).toContain('- link "Account" [ref=3]');
  });

  it("names by text however deep it sits, but not text hidden from reading", () => {
    const deep = (text: string) => "<div>".repeat(8) + text + "</div>".repeat(8);
    document.body.innerHTML =
      `<ul><li>${deep("Burgers")}</li></ul>` +
      `<section aria-label="Menu"><table><tr><td>${deep("Fries")}<span aria-hidden="true">★</span>` +
      "<style>td{}</style><span hidden>secret</span></td></tr></table></section>";
    const tree = snapshot();
    expect(tree).toContain('- listitem "Burgers" [ref=');
    expect(tree).toContain('- cell "Fries" [ref=');
  });

  it("gives presentation-only boxes and empty live regions no line, but walks inside them", () => {
    document.body.innerHTML =
      '<div role="presentation"><button>Slide 1 of 3</button><button>Slide 2 of 3</button></div>' +
      '<ul role="none"><li role="none"><a href="/a">Deals</a></li></ul>' +
      '<div role="status"></div><div role="status"> </div><div role="status">3 items</div>';
    expect(snapshot()).toBe(
      [
        '- button "Slide 1 of 3" [ref=1]',
        '- button "Slide 2 of 3" [ref=2]',
        '- link "Deals" [ref=3]',
        '- status "3 items" [ref=4]',
      ].join("\n"),
    );
  });

  it("leaves out an unnamed image that a named control or region around it already says", () => {
    document.body.innerHTML =
      '<button aria-label="Double cheese, 12,50 €"><img src="a.png"><span>Double cheese</span></button>' +
      '<div role="img"></div>' +
      '<section aria-label="Photos"><div><img src="b.png"></div><img src="c.png" alt="Patio"></section>' +
      '<div role="list"><div role="listitem"><img src="d.png"></div></div>';
    expect(snapshot()).toBe(
      [
        '- button "Double cheese, 12,50 €" [ref=1]',
        "- img [ref=2]",
        '- region "Photos" [ref=3]',
        '  - image "Patio" [ref=4]',
        "- list [ref=5]",
        "  - listitem [ref=6]",
        "    - image [ref=7]",
      ].join("\n"),
    );
  });

  it("says what part of the page is on screen, and which lines are off it", () => {
    Object.defineProperty(window, "scrollY", { configurable: true, value: 900 });
    Object.defineProperty(document.documentElement, "scrollHeight", {
      configurable: true,
      value: 10430,
    });
    try {
      document.body.innerHTML =
        '<button data-y="-40">Top</button><button data-y="100">Here</button>' +
        `<button data-y="${window.innerHeight}">Next</button>` +
        `<button data-x="${window.innerWidth}" data-y="100">Slide 2</button>` +
        '<button data-x="-20" data-y="100">Slide 0</button>';
      expect(fullTree()).toBe(
        [
          `viewport: 900–${900 + window.innerHeight} of 10430px`,
          '- button "Top" [above] [ref=1]',
          '- button "Here" [ref=2]',
          '- button "Next" [below] [ref=3]',
          '- button "Slide 2" [right] [ref=4]',
          '- button "Slide 0" [left] [ref=5]',
        ].join("\n"),
      );
    } finally {
      Object.defineProperty(window, "scrollY", { configurable: true, value: 0 });
      Reflect.deleteProperty(document.documentElement, "scrollHeight");
    }
  });

  it("stops at its size, showing what's on screen first, and gives refs only to lines shown", () => {
    const item = (i: number, y: number) =>
      `<li data-y="${y}"><a href="/item/${i}" data-y="${y}">Item number ${i} on a long results page</a></li>`;
    // Enough results above the screen to pass the size several times over, then the ones on it.
    const count = 600;
    const above = Array.from({ length: count }, (_, i) => item(i, -100)).join("");
    document.body.innerHTML = `<ul data-y="-100">${above}${item(count, 100)}${item(count + 1, 200)}</ul>`;
    const tree = fullTree();
    expect(tree.length).toBeLessThan(24000 + 200);
    const lines = tree.split("\n");
    expect(lines.at(-1)).toMatch(/^… \d+ more$/);
    // What's on screen is shown, under its list.
    expect(tree).toContain(`- link "Item number ${count} on a long results page" [ref=`);
    expect(tree).toContain(`- link "Item number ${count + 1} on a long results page" [ref=`);
    expect(lines[1]).toMatch(/^- list .*\[above\] \[ref=1\]$/);
    const shown = lines.filter((line) => line.includes("[ref=")).length;
    const left = Number(lines.at(-1)!.match(/^… (\d+) more$/)![1]);
    // Every element is either shown or counted, and only those shown have refs.
    expect(shown + left).toBe(1 + 2 * (count + 2));
    // Most were left out: the size held.
    expect(left).toBeGreaterThan(count);
    const refs = (window as unknown as { __omni_refs__: Map<number, unknown> }).__omni_refs__;
    expect(refs.size).toBe(shown);
    expect(Math.max(...refs.keys())).toBe(shown);
  });
});
