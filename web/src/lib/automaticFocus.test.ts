import { afterEach, describe, expect, it } from "vitest";

import { closingDialogFocus, isBehindOpenLayer } from "./automaticFocus";

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

describe("closingDialogFocus", () => {
  const PICKER = "[data-picker]";

  it("returns focus to the opener while focus is still in a closing picker, or on the page", () => {
    const opener = element('<textarea id="opener"></textarea>', "#opener");
    const query = element('<div data-picker><input id="query" /></div>', "#query");
    query.focus();
    expect(closingDialogFocus(opener, PICKER)).toEqual({ movedOn: false, restore: opener });
    query.blur();
    expect(closingDialogFocus(opener, PICKER)).toEqual({ movedOn: false, restore: opener });
  });

  it("leaves focus that something moved out of every picker where it is", () => {
    const opener = element('<textarea id="opener"></textarea>', "#opener");
    const elsewhere = element('<button id="elsewhere">Elsewhere</button>', "#elsewhere");
    elsewhere.focus();
    expect(closingDialogFocus(opener, PICKER).movedOn).toBe(true);
  });

  it("restores nothing to an opener that left the page or sits behind an open layer", () => {
    const opener = element('<textarea id="opener"></textarea>', "#opener");
    element('<div role="dialog" data-state="open"><button id="in">In</button></div>', "#in");
    expect(closingDialogFocus(opener, PICKER).restore).toBeNull();
    document.body.innerHTML = "";
    expect(closingDialogFocus(opener, PICKER).restore).toBeNull();
    expect(closingDialogFocus(null, PICKER)).toEqual({ movedOn: false, restore: null });
  });
});
