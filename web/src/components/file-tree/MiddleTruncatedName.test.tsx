import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { MiddleTruncatedName } from "./MiddleTruncatedName";

describe("MiddleTruncatedName", () => {
  it("clips a long name's start from its end and its extension from its start", () => {
    const { container } = render(<MiddleTruncatedName name="DataModelHandler.scala" />);
    const start = screen.getByText("DataModelHandler.");
    expect(start).toHaveClass("truncate");
    expect(start).not.toHaveClass("[direction:rtl]");
    expect(screen.getByText("scala").parentElement).toHaveClass("truncate", "[direction:rtl]");
    expect(container).toHaveTextContent(/^DataModelHandler\.scala$/);
  });

  it("clips a name too short to split from its end", () => {
    render(<MiddleTruncatedName name="a.ts" className="text-muted-foreground" />);
    expect(screen.getByText("a.ts")).toHaveClass("truncate", "text-muted-foreground");
    expect(screen.getByText("a.ts")).not.toHaveClass("[direction:rtl]");
  });
});
