import { describe, expect, it } from "vitest";
import { ADD_COMMENT_BUTTON_CLASS_NAME } from "./addCommentButtonStyles";

describe("ADD_COMMENT_BUTTON_CLASS_NAME", () => {
  it("takes clicks even while a modal dialog disables pointer events on <body>", () => {
    expect(ADD_COMMENT_BUTTON_CLASS_NAME.split(" ")).toContain("pointer-events-auto");
  });
});
