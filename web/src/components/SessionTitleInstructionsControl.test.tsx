import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { SessionTitleInstructionsControl } from "./SessionTitleInstructionsControl";
import {
  getSessionTitleInstructions,
  saveSessionTitleInstructions,
} from "@/lib/sessionTitleInstructionsApi";

vi.mock("@/lib/sessionTitleInstructionsApi", () => ({
  getSessionTitleInstructions: vi.fn(),
  saveSessionTitleInstructions: vi.fn(),
}));
vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const getMock = vi.mocked(getSessionTitleInstructions);
const saveMock = vi.mocked(saveSessionTitleInstructions);

function renderControl() {
  return render(<SessionTitleInstructionsControl />);
}

async function loadedInput(): Promise<HTMLTextAreaElement> {
  const input = await screen.findByTestId("session-title-instructions-input");
  await waitFor(() => expect(input).toBeEnabled());
  return input as HTMLTextAreaElement;
}

beforeEach(() => {
  getMock.mockResolvedValue({ instructions: null, max_chars: 4000 });
  saveMock.mockImplementation(async (instructions) => ({
    instructions: instructions?.trim() || null,
    max_chars: 4000,
  }));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("SessionTitleInstructionsControl", () => {
  it("shows the saved instructions and their length", async () => {
    getMock.mockResolvedValue({ instructions: "Start with the ticket id.", max_chars: 4000 });
    renderControl();

    expect(await loadedInput()).toHaveValue("Start with the ticket id.");
    expect(screen.getByTestId("session-title-instructions-status")).toHaveTextContent(
      "25 / 4000 characters",
    );
    expect(screen.getByTestId("session-title-instructions-save")).toBeDisabled();
  });

  it("saves trimmed instructions and confirms", async () => {
    renderControl();
    const input = await loadedInput();

    fireEvent.change(input, { target: { value: "  Use French titles.  " } });
    fireEvent.click(screen.getByTestId("session-title-instructions-save"));

    await waitFor(() => expect(saveMock).toHaveBeenCalledWith("Use French titles."));
    await waitFor(() => expect(input).toHaveValue("Use French titles."));
    expect(toast.success).toHaveBeenCalledWith("Title instructions saved");
    expect(screen.getByTestId("session-title-instructions-save")).toBeDisabled();
  });

  it("clears saved instructions", async () => {
    getMock.mockResolvedValue({ instructions: "Use French titles.", max_chars: 4000 });
    renderControl();
    const input = await loadedInput();

    fireEvent.click(screen.getByTestId("session-title-instructions-clear"));

    await waitFor(() => expect(saveMock).toHaveBeenCalledWith(null));
    await waitFor(() => expect(input).toHaveValue(""));
    expect(toast.success).toHaveBeenCalledWith("Title instructions cleared");
    expect(screen.queryByTestId("session-title-instructions-clear")).toBeNull();
  });

  it("blocks saving past the server limit", async () => {
    getMock.mockResolvedValue({ instructions: null, max_chars: 10 });
    renderControl();
    const input = await loadedInput();

    fireEvent.change(input, { target: { value: "x".repeat(11) } });

    expect(input).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByTestId("session-title-instructions-status")).toHaveTextContent(
      "11 / 10 characters — shorten to save",
    );
    expect(screen.getByTestId("session-title-instructions-save")).toBeDisabled();
  });

  it("counts an emoji as one character, like the server", async () => {
    getMock.mockResolvedValue({ instructions: null, max_chars: 10 });
    renderControl();
    const input = await loadedInput();

    fireEvent.change(input, { target: { value: "🚀".repeat(10) } });

    expect(input).not.toHaveAttribute("aria-invalid");
    expect(screen.getByTestId("session-title-instructions-status")).toHaveTextContent(
      "10 / 10 characters",
    );
    expect(screen.getByTestId("session-title-instructions-save")).toBeEnabled();
  });

  it("keeps the draft and reports a failed save", async () => {
    saveMock.mockRejectedValue(new Error("server unavailable"));
    renderControl();
    const input = await loadedInput();

    fireEvent.change(input, { target: { value: "Use French titles." } });
    fireEvent.click(screen.getByTestId("session-title-instructions-save"));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Couldn't save title instructions: server unavailable",
      ),
    );
    expect(input).toHaveValue("Use French titles.");
    expect(screen.getByTestId("session-title-instructions-save")).toBeEnabled();
  });

  it("reports a failed load and retries", async () => {
    getMock.mockRejectedValueOnce(new Error("offline"));
    renderControl();

    expect(await screen.findByText("Couldn't load your title instructions.")).toBeVisible();
    expect(screen.getByTestId("session-title-instructions-input")).toBeDisabled();
    expect(screen.queryByTestId("session-title-instructions-save")).toBeNull();

    fireEvent.click(screen.getByTestId("session-title-instructions-retry"));

    await loadedInput();
    expect(getMock).toHaveBeenCalledTimes(2);
  });
});
