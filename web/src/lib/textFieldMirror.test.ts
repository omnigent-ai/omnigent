import { afterEach, describe, expect, it } from "vitest";
import { createTextFieldMirror } from "./textFieldMirror";

afterEach(() => {
  document.body.replaceChildren();
});

describe("createTextFieldMirror", () => {
  it("lays a textarea's text out in its content box with its font", () => {
    const field = document.createElement("textarea");
    field.style.font = "14px / 20px serif";
    field.style.padding = "8px 10px";
    document.body.append(field);
    Object.defineProperty(field, "clientWidth", { value: 300 });

    const mirror = createTextFieldMirror(field, getComputedStyle(field));

    expect(mirror.isConnected).toBe(false);
    expect(mirror.style.font).toBe(getComputedStyle(field).font);
    expect(mirror.style.font).not.toBe("");
    expect(mirror.style.width).toBe("280px");
    expect(mirror.style.whiteSpace).toBe("pre-wrap");
    expect(mirror.style.visibility).toBe("hidden");
  });

  it("keeps a text input's mirror on one line", () => {
    const field = document.createElement("input");
    document.body.append(field);

    expect(createTextFieldMirror(field, getComputedStyle(field)).style.whiteSpace).toBe("pre");
  });
});
