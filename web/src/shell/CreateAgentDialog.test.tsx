import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";

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

  it("accepts only names the server accepts", () => {
    const onCreate = vi.fn();
    renderDialog({ onCreate });
    fireEvent.change(screen.getByTestId("create-agent-model"), { target: { value: "m" } });

    fireEvent.change(screen.getByTestId("create-agent-name"), { target: { value: "Agent 1" } });
    expect(screen.getByTestId("create-agent-name-error")).toHaveTextContent(
      "Use only letters, numbers, hyphens, and underscores.",
    );
    expect(screen.getByTestId("create-agent-submit")).toBeDisabled();

    fireEvent.change(screen.getByTestId("create-agent-name"), { target: { value: " agent-1 " } });
    expect(screen.queryByTestId("create-agent-name-error")).toBeNull();
    fireEvent.click(screen.getByTestId("create-agent-submit"));
    expect(onCreate).toHaveBeenCalledWith(expect.objectContaining({ name: "agent-1" }));
    expect(screen.getByTestId("create-agent-name")).toHaveValue("");
    expect(screen.getByTestId("create-agent-model")).toHaveValue("");
  });

  it("only creates on an explicit click, not Enter in a field", async () => {
    const user = userEvent.setup();
    const onCreate = vi.fn();
    renderDialog({ onCreate });
    await user.type(screen.getByTestId("create-agent-name"), "agent-1");
    await user.type(screen.getByTestId("create-agent-model"), "test-model{Enter}");
    expect(onCreate).not.toHaveBeenCalled();
    await user.click(screen.getByTestId("create-agent-submit"));
    expect(onCreate).toHaveBeenCalledOnce();
  });

  it.each(["Cancel", "Close"])("resets the draft on %s without relying on unmount", (action) => {
    const onOpenChange = vi.fn();
    renderDialog({ onOpenChange });
    for (const field of ["name", "model", "description", "instructions"]) {
      fireEvent.change(screen.getByTestId(`create-agent-${field}`), {
        target: { value: "draft" },
      });
    }
    fireEvent.click(screen.getByTestId("create-agent-add-mcp"));
    fireEvent.click(screen.getByRole("button", { name: action }));
    expect(onOpenChange).toHaveBeenCalledWith(false);
    for (const field of ["name", "model", "description", "instructions"]) {
      expect(screen.getByTestId(`create-agent-${field}`)).toHaveValue("");
    }
    expect(screen.queryByTestId("create-agent-mcp-entry")).toBeNull();
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

  it("locks dismissal and Create while an import is in flight", async () => {
    let finish: () => void = () => {};
    const onImport = vi.fn(
      () =>
        new Promise<void>((resolve) => {
          finish = resolve;
        }),
    );
    const onOpenChange = vi.fn();
    renderDialog({ onImport, onOpenChange });

    fireEvent.change(screen.getByTestId("create-agent-import-input"), {
      target: { files: [bundle] },
    });

    await waitFor(() => expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled());
    expect(screen.getByTestId("create-agent-submit")).toBeDisabled();
    expect(screen.getByTestId("create-agent-name")).toBeEnabled();
    expect(screen.getByTestId("create-agent-add-mcp")).toBeEnabled();
    fireEvent.change(screen.getByTestId("create-agent-name"), { target: { value: "draft" } });
    expect(screen.getByTestId("create-agent-name")).toHaveValue("draft");
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onOpenChange).not.toHaveBeenCalled();
    finish();
    await waitFor(() => expect(onOpenChange).toHaveBeenCalledWith(false));
    await waitFor(() => expect(screen.getByTestId("create-agent-import")).not.toBeDisabled());
    expect(screen.getByTestId("create-agent-name")).toHaveValue("");
  });
});
