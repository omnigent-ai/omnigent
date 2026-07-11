import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ImportAgentDialog } from "./ImportAgentDialog";
import { buildBundleFromFiles } from "@/lib/agentBundle";
import { validateAgentBundle } from "@/lib/sessionsApi";

vi.mock("@/lib/agentBundle", () => ({ buildBundleFromFiles: vi.fn() }));
vi.mock("@/lib/sessionsApi", () => ({ validateAgentBundle: vi.fn() }));

const buildBundleFromFilesMock = vi.mocked(buildBundleFromFiles);
const validateAgentBundleMock = vi.mocked(validateAgentBundle);

// A fake packed bundle the mocked builder returns; identity is asserted so
// the SAME validated bundle flows through to onImport (no re-pack).
const FAKE_BUNDLE = new File([new Uint8Array([1, 2, 3])], "agent.tar.gz", {
  type: "application/gzip",
});

function pickFile(name = "config.yaml"): void {
  const input = screen.getByTestId("import-agent-file-input") as HTMLInputElement;
  const file = new File(["spec_version: 1\nname: x\n"], name, { type: "text/yaml" });
  // jsdom's FileList: assign via defineProperty so `input.files` is readable.
  Object.defineProperty(input, "files", { value: [file], configurable: true });
  fireEvent.change(input);
}

/** A deferred promise whose resolve/reject can be triggered from a test. */
function deferred<T>(): { promise: Promise<T>; resolve: (v: T) => void } {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

beforeEach(() => {
  buildBundleFromFilesMock.mockReset();
  validateAgentBundleMock.mockReset();
  buildBundleFromFilesMock.mockResolvedValue(FAKE_BUNDLE);
});

afterEach(() => cleanup());

describe("ImportAgentDialog", () => {
  it("validates a pick and shows the 'agent config is loaded' message on success", async () => {
    validateAgentBundleMock.mockResolvedValueOnce({ agentName: "my-agent" });
    render(<ImportAgentDialog open onOpenChange={vi.fn()} onImport={vi.fn()} />);

    // Import is disabled until a pick validates.
    expect(screen.getByTestId("import-agent-submit")).toBeDisabled();

    pickFile();

    await waitFor(() => {
      expect(screen.getByTestId("import-agent-validated")).toBeInTheDocument();
    });
    expect(screen.getByTestId("import-agent-validated")).toHaveTextContent(
      /agent config is loaded/i,
    );
    expect(validateAgentBundleMock).toHaveBeenCalledWith(FAKE_BUNDLE);
    // Only now is Import enabled.
    expect(screen.getByTestId("import-agent-submit")).not.toBeDisabled();
  });

  it("shows the server error and keeps Import disabled when the config is invalid", async () => {
    validateAgentBundleMock.mockRejectedValueOnce(new Error("agent spec must include a name"));
    render(<ImportAgentDialog open onOpenChange={vi.fn()} onImport={vi.fn()} />);

    pickFile();

    await waitFor(() => {
      expect(screen.getByTestId("import-agent-error")).toHaveTextContent(
        /agent spec must include a name/,
      );
    });
    expect(screen.queryByTestId("import-agent-validated")).not.toBeInTheDocument();
    // An invalid config can never be imported.
    expect(screen.getByTestId("import-agent-submit")).toBeDisabled();
  });

  it("imports the validated bundle without re-packing on submit", async () => {
    validateAgentBundleMock.mockResolvedValueOnce({ agentName: "my-agent" });
    const onImport = vi.fn();
    const onOpenChange = vi.fn();
    render(<ImportAgentDialog open onOpenChange={onOpenChange} onImport={onImport} />);

    pickFile("my-agent.yaml");
    await waitFor(() => {
      expect(screen.getByTestId("import-agent-submit")).not.toBeDisabled();
    });

    // One pack for validation; submit reuses that same bundle.
    expect(buildBundleFromFilesMock).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("import-agent-submit"));

    expect(onImport).toHaveBeenCalledTimes(1);
    const arg = onImport.mock.calls[0]![0] as { name: string; bundle: File };
    expect(arg.bundle).toBe(FAKE_BUNDLE);
    expect(arg.name).toBe("my-agent");
    expect(buildBundleFromFilesMock).toHaveBeenCalledTimes(1);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("ignores a stale validation when a newer pick supersedes it", async () => {
    // First pick validates slowly; a second pick resolves first. The slow
    // first result must not win and re-enable Import for the wrong pick.
    const first = deferred<{ agentName: string }>();
    validateAgentBundleMock.mockReturnValueOnce(first.promise);
    validateAgentBundleMock.mockRejectedValueOnce(new Error("second pick is invalid"));
    render(<ImportAgentDialog open onOpenChange={vi.fn()} onImport={vi.fn()} />);

    pickFile("first.yaml");
    pickFile("second.yaml");

    // Second (rejected) pick settles → error shown, Import disabled.
    await waitFor(() => {
      expect(screen.getByTestId("import-agent-error")).toHaveTextContent(/second pick is invalid/);
    });

    // Now let the stale first pick resolve — it must not overwrite state.
    first.resolve({ agentName: "first" });
    await Promise.resolve();
    expect(screen.queryByTestId("import-agent-validated")).not.toBeInTheDocument();
    expect(screen.getByTestId("import-agent-submit")).toBeDisabled();
  });

  it("ignores a validation that resolves after the dialog is closed", async () => {
    const pending = deferred<{ agentName: string }>();
    validateAgentBundleMock.mockReturnValueOnce(pending.promise);
    const onImport = vi.fn();
    render(<ImportAgentDialog open onOpenChange={vi.fn()} onImport={onImport} />);

    pickFile("config.yaml");
    // Close the dialog (runs reset()) while validation is still in flight.
    fireEvent.click(screen.getByText("Cancel"));

    // The late-arriving success must not re-enable Import for the closed pick.
    pending.resolve({ agentName: "late" });
    await Promise.resolve();
    expect(screen.queryByTestId("import-agent-validated")).not.toBeInTheDocument();
  });
});
