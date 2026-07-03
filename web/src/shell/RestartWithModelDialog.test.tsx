import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { TooltipProvider } from "@/components/ui/tooltip";
import { RestartWithModelDialog } from "./RestartWithModelDialog";
import { forkSession } from "@/lib/sessionsApi";

const navigateMock = vi.fn();
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigateMock }));
vi.mock("@/lib/sessionsApi", () => ({ forkSession: vi.fn() }));

const forkSessionMock = vi.mocked(forkSession);

function renderDialog(currentModel: string | null = "databricks-gpt-5-5") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        <RestartWithModelDialog
          sessionId="conv_src"
          currentModel={currentModel}
          open
          onOpenChange={() => {}}
        />
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

describe("RestartWithModelDialog", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(cleanup);

  it("forks with the chosen model_override and navigates into the clone", async () => {
    forkSessionMock.mockResolvedValue({ id: "conv_forked" } as Awaited<
      ReturnType<typeof forkSession>
    >);
    renderDialog("databricks-gpt-5-5");

    const input = screen.getByTestId("restart-model-input");
    fireEvent.change(input, { target: { value: "databricks-gpt-5-4-mini" } });
    fireEvent.click(screen.getByTestId("restart-model-submit"));

    await waitFor(() => {
      expect(forkSessionMock).toHaveBeenCalledWith(
        "conv_src",
        undefined,
        undefined,
        undefined,
        "databricks-gpt-5-4-mini",
      );
    });
    await waitFor(() => {
      expect(navigateMock).toHaveBeenCalledWith("/c/conv_forked");
    });
  });

  it("disables submit until a different, valid model is entered", () => {
    renderDialog("databricks-gpt-5-5");
    const submit = screen.getByTestId("restart-model-submit");

    // Prefilled with the current model → unchanged, so submit is disabled.
    expect(submit).toBeDisabled();

    // A flag-shaped value fails the charset guard → still disabled.
    fireEvent.change(screen.getByTestId("restart-model-input"), {
      target: { value: "--evil" },
    });
    expect(submit).toBeDisabled();

    // A different, valid id enables submit.
    fireEvent.change(screen.getByTestId("restart-model-input"), {
      target: { value: "databricks-gpt-5-4-mini" },
    });
    expect(submit).not.toBeDisabled();
  });

  it("surfaces a fork error inline without navigating", async () => {
    forkSessionMock.mockRejectedValue(new Error("harness 'codex-native' only runs GPT models"));
    renderDialog("databricks-gpt-5-5");

    fireEvent.change(screen.getByTestId("restart-model-input"), {
      target: { value: "databricks-claude-opus-4-8" },
    });
    fireEvent.click(screen.getByTestId("restart-model-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("restart-model-error")).toHaveTextContent("only runs GPT models");
    });
    expect(navigateMock).not.toHaveBeenCalled();
  });
});
