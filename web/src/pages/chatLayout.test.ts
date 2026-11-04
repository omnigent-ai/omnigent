import { afterEach, describe, expect, it } from "vitest";

import { COMPOSER_POPOVER_HEADER_INSET, measureComposerPopoverHeaderInset } from "./chatLayout";

function addHeader(bottom: number) {
  const header = document.createElement("header");
  header.className = "chat-header";
  header.getBoundingClientRect = () => ({ bottom }) as DOMRect;
  document.body.append(header);
}

afterEach(() => {
  document.body.replaceChildren();
});

describe("measureComposerPopoverHeaderInset", () => {
  it("keeps the header-height floor for a header at the top of the viewport", () => {
    addHeader(48);
    expect(measureComposerPopoverHeaderInset()).toEqual(COMPOSER_POPOVER_HEADER_INSET);
  });

  it("keeps the floor when no header is rendered", () => {
    expect(measureComposerPopoverHeaderInset()).toEqual(COMPOSER_POPOVER_HEADER_INSET);
  });

  it("clears the lowest header pushed down by a native safe area", () => {
    addHeader(48);
    addHeader(87.5);
    expect(measureComposerPopoverHeaderInset()).toEqual({
      ...COMPOSER_POPOVER_HEADER_INSET,
      top: 96,
    });
  });
});
