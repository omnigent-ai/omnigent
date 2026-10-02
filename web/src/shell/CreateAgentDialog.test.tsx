import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { CreateAgentDialog } from "./CreateAgentDialog";

function renderDialog(props: Partial<Parameters<typeof CreateAgentDialog>[0]> = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <CreateAgentDialog open onOpenChange={vi.fn()} onCreate={vi.fn()} {...props} />
    </QueryClientProvider>,
  );
}

const bundle = new File([new Uint8Array([0x1f, 0x8b])], "orion.tar.gz", {
  type: "application/gzip",
});

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

  it("hides Import bundle without an import handler", () => {
    renderDialog();
    expect(screen.queryByTestId("create-agent-import")).toBeNull();
  });

  it("imports a picked bundle and closes", async () => {
    const onImport = vi.fn().mockResolvedValue(undefined);
    const onOpenChange = vi.fn();
    renderDialog({ onImport, onOpenChange });

    fireEvent.change(screen.getByTestId("create-agent-import-input"), {
      target: { files: [bundle] },
    });

    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    expect(onImport).toHaveBeenCalledWith(bundle);
  });

  it("keeps the dialog open and shows the server's reason on failure", async () => {
    const onImport = vi.fn().mockRejectedValue(new Error("'polly' is a built-in agent"));
    const onOpenChange = vi.fn();
    renderDialog({ onImport, onOpenChange });

    fireEvent.change(screen.getByTestId("create-agent-import-input"), {
      target: { files: [bundle] },
    });

    expect(await screen.findByTestId("create-agent-import-error")).toHaveTextContent(
      "'polly' is a built-in agent",
    );
    expect(onOpenChange).not.toHaveBeenCalled();
    expect(screen.getByTestId("create-agent-import")).not.toBeDisabled();
  });

  it("locks Cancel and Create while an import is in flight", async () => {
    let finish: () => void = () => {};
    const onImport = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    renderDialog({ onImport });

    fireEvent.change(screen.getByTestId("create-agent-import-input"), {
      target: { files: [bundle] },
    });

    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled());
    expect(screen.getByTestId("create-agent-submit")).toBeDisabled();
    finish();
    await waitFor(() => expect(screen.getByTestId("create-agent-import")).not.toBeDisabled());
  });
});
