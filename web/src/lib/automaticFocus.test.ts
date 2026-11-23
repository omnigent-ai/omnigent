import { afterEach, describe, expect, it } from "vitest";

import { isBehindOpenLayer } from "./automaticFocus";

afterEach(() => {
  document.body.innerHTML = "";
});

function element(html: string, selector: string): HTMLElement {
  document.body.insertAdjacentHTML("beforeend", html);
  return document.querySelector<HTMLElement>(selector)!;
}

describe("isBehindOpenLayer", () => {
  it("is false with no open layer, and for an element inside the open one", () => {
    const button = element('<button id="outside">Outside</button>', "#outside");
    expect(isBehindOpenLayer(button)).toBe(false);
    const inside = element(
      '<div role="dialog" data-state="open"><input id="inside" /></div>',
      "#inside",
    );
    expect(isBehindOpenLayer(inside)).toBe(false);
    expect(isBehindOpenLayer(button)).toBe(true);
  });

  it("lets the top of two portaled dialogs take focus, and blocks the one below", () => {
    const lower = element(
      '<div role="dialog" data-state="open"><button id="in-lower">Lower</button></div>',
      "#in-lower",
    );
    const upper = element(
      '<div role="dialog" data-state="open"><button id="in-upper">Upper</button></div>',
      "#in-upper",
    );
    expect(isBehindOpenLayer(upper)).toBe(false);
    expect(isBehindOpenLayer(lower)).toBe(true);
  });

  it("ignores a layer that is closing", () => {
    const button = element('<button id="outside">Outside</button>', "#outside");
    element('<div role="menu" data-state="closed"></div>', "[role=menu]");
    expect(isBehindOpenLayer(button)).toBe(false);
  });
});
