import { render, screen } from "@testing-library/react";
import { createRef } from "react";
import { describe, expect, it } from "vitest";

import { InputGroup, InputGroupInput } from "./input-group";

describe("InputGroupInput", () => {
  it("hands its input to a ref, so a caller can focus the field", () => {
    const ref = createRef<HTMLInputElement>();
    render(
      <InputGroup>
        <InputGroupInput ref={ref} aria-label="Search" />
      </InputGroup>,
    );
    expect(ref.current).toBe(screen.getByRole("textbox", { name: "Search" }));
  });
});
