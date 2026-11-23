import { afterEach, describe, expect, it } from "vitest";

import { isFocusInFindBar } from "./findBarFocus";

afterEach(() => {
  document.body.innerHTML = "";
});

describe("isFocusInFindBar", () => {
  it("is focus in the app's find bar, its field or a button, or in the code editor's", () => {
    document.body.innerHTML = `
      <div data-find-bar><input id="field" /><button id="next">Next</button></div>
      <div class="monaco-editor"><div class="find-widget"><textarea id="monaco"></textarea></div></div>`;
    for (const id of ["field", "next", "monaco"]) {
      document.getElementById(id)!.focus();
      expect(isFocusInFindBar(), id).toBe(true);
    }
  });

  it("isn't focus anywhere else, or nowhere", () => {
    document.body.innerHTML = `<textarea id="terminal"></textarea><div class="find-widget"><input id="loose" /></div>`;
    expect(isFocusInFindBar()).toBe(false);
    document.getElementById("terminal")!.focus();
    expect(isFocusInFindBar()).toBe(false);
    // A find widget outside the code editor is someone else's.
    document.getElementById("loose")!.focus();
    expect(isFocusInFindBar()).toBe(false);
  });
});
