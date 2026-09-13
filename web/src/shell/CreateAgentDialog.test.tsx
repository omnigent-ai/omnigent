import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { CreateAgentDialog } from "./CreateAgentDialog";

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CreateAgentDialog open onOpenChange={vi.fn()} onCreate={vi.fn()} />
    </QueryClientProvider>,
  );
}

afterEach(cleanup);

describe("CreateAgentDialog", () => {
  it("gives the form scroll region room for the fields' focus ring", () => {
    renderDialog();

    const scrollRegion = screen
      .getByTestId("create-agent-dialog")
      .querySelector(".overflow-y-auto");
    if (!scrollRegion) throw new Error("create-agent scroll region not found");
    // overflow-y-auto also clips horizontally at the padding box, so the
    // full-width fields need horizontal padding or their 3px focus ring is
    // chopped at the container's left/right edges. -mx-1 keeps the fields
    // visually aligned with the dialog header/footer.
    expect(scrollRegion).toHaveClass("px-1", "-mx-1");
  });
});
