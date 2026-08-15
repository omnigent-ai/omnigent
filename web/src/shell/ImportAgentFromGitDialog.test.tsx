import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const importMock = vi.fn();
vi.mock("@/lib/agentsApi", () => ({ importAgentFromGit: (...a: unknown[]) => importMock(...a) }));

// Default: two online hosts + one offline.
const useHostsMock = vi.fn();
vi.mock("@/hooks/useHosts", () => ({ useHosts: () => useHostsMock() }));

import { ImportAgentFromGitDialog } from "./ImportAgentFromGitDialog";

const HOST_A = { host_id: "h_a", name: "Host Alpha", status: "online" as const };
const HOST_B = { host_id: "h_b", name: "Host Beta", status: "online" as const };
const HOST_OFFLINE = { host_id: "h_off", name: "Offline Host", status: "offline" as const };

function twoOnlineHosts() {
  useHostsMock.mockReturnValue({ data: [HOST_A, HOST_B, HOST_OFFLINE] });
}

function oneOnlineHost() {
  useHostsMock.mockReturnValue({ data: [HOST_A, HOST_OFFLINE] });
}

function noOnlineHosts() {
  useHostsMock.mockReturnValue({ data: [HOST_OFFLINE] });
}

beforeEach(() => {
  importMock.mockReset();
  twoOnlineHosts();
});
afterEach(cleanup);

describe("ImportAgentFromGitDialog", () => {
  it("offers an optional Agent name field, empty by default", () => {
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    const nameInput = screen.getByLabelText(/agent name/i) as HTMLInputElement;
    expect(nameInput).toBeInTheDocument();
    // Blank ⇒ the server derives the name from the repo's config.yaml.
    expect(nameInput.value).toBe("");
  });

  it("does not require a name to submit", () => {
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    fireEvent.change(screen.getByLabelText(/host/i), { target: { value: "h_a" } });
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    expect(screen.getByRole("button", { name: /import/i })).not.toBeDisabled();
  });

  it("sends a supplied name so one repo can be imported per branch", async () => {
    importMock.mockResolvedValueOnce({ id: "ag_1", name: "myagent-dev" });
    const onImported = vi.fn();
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={onImported} />);
    fireEvent.change(screen.getByLabelText(/host/i), { target: { value: "h_a" } });
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    fireEvent.change(screen.getByLabelText(/branch/i), { target: { value: "dev" } });
    fireEvent.change(screen.getByLabelText(/agent name/i), { target: { value: "myagent-dev" } });
    fireEvent.click(screen.getByRole("button", { name: /import/i }));
    await waitFor(() => expect(onImported).toHaveBeenCalled());
    expect(importMock).toHaveBeenCalledWith(
      expect.objectContaining({ name: "myagent-dev", gitRef: "dev" }),
    );
  });

  it("omits a whitespace-only name so the repo's own name is used", async () => {
    importMock.mockResolvedValueOnce({ id: "ag_1", name: "x" });
    const onImported = vi.fn();
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={onImported} />);
    fireEvent.change(screen.getByLabelText(/host/i), { target: { value: "h_a" } });
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    fireEvent.change(screen.getByLabelText(/agent name/i), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: /import/i }));
    await waitFor(() => expect(onImported).toHaveBeenCalled());
    expect(importMock).toHaveBeenCalledWith(expect.objectContaining({ name: undefined }));
  });

  it("disables submit until a repo URL is entered", () => {
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    // No host selected yet, submit should be disabled
    expect(screen.getByRole("button", { name: /import/i })).toBeDisabled();
  });

  it("submits url/branch/subpath and fires onImported", async () => {
    importMock.mockResolvedValueOnce({ id: "ag_1", name: "x" });
    const onImported = vi.fn();
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={onImported} />);
    // Select a host
    fireEvent.change(screen.getByLabelText(/host/i), { target: { value: "h_a" } });
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    fireEvent.change(screen.getByLabelText(/branch/i), { target: { value: "main" } });
    fireEvent.click(screen.getByRole("button", { name: /import/i }));
    await waitFor(() => expect(onImported).toHaveBeenCalled());
    expect(importMock).toHaveBeenCalledWith({
      gitUrl: "https://github.com/org/repo",
      gitRef: "main",
      gitSubpath: undefined,
      hostId: "h_a",
    });
  });

  it("renders the server error inline", async () => {
    importMock.mockRejectedValueOnce(new Error("Not a valid git URL."));
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    fireEvent.change(screen.getByLabelText(/host/i), { target: { value: "h_a" } });
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "file:///x" },
    });
    fireEvent.click(screen.getByRole("button", { name: /import/i }));
    await waitFor(() => expect(screen.getByText(/valid git URL/i)).toBeInTheDocument());
  });

  // ---- New host-picker tests ----

  it("renders only online hosts in the selector", () => {
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    const select = screen.getByLabelText(/host/i) as HTMLSelectElement;
    const optionValues = Array.from(select.options).map((o) => o.value);
    expect(optionValues).toContain("h_a");
    expect(optionValues).toContain("h_b");
    expect(optionValues).not.toContain("h_off");
  });

  it("submit is disabled without a host even when URL is set", () => {
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    // host still empty
    expect(screen.getByRole("button", { name: /import/i })).toBeDisabled();
  });

  it("submit is enabled once both URL and host are set", () => {
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    fireEvent.change(screen.getByLabelText(/host/i), { target: { value: "h_a" } });
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    expect(screen.getByRole("button", { name: /import/i })).not.toBeDisabled();
  });

  it("preselects the only online host when there is exactly one", () => {
    oneOnlineHost();
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    const select = screen.getByLabelText(/host/i) as HTMLSelectElement;
    expect(select.value).toBe("h_a");
  });

  it("sends hostId in the importAgentFromGit call", async () => {
    oneOnlineHost();
    importMock.mockResolvedValueOnce({ id: "ag_2", name: "y" });
    const onImported = vi.fn();
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={onImported} />);
    // preselected to h_a
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    fireEvent.click(screen.getByRole("button", { name: /import/i }));
    await waitFor(() => expect(importMock).toHaveBeenCalled());
    expect(importMock).toHaveBeenCalledWith(expect.objectContaining({ hostId: "h_a" }));
  });

  it("shows connect-a-host message and disables submit when no online hosts", () => {
    noOnlineHosts();
    render(<ImportAgentFromGitDialog open onOpenChange={() => {}} onImported={() => {}} />);
    expect(screen.getByText(/connect a host to import from git/i)).toBeInTheDocument();
    expect(screen.queryByLabelText(/host/i)).toBeNull(); // select not shown
    fireEvent.change(screen.getByLabelText(/repo(sitory)? url/i), {
      target: { value: "https://github.com/org/repo" },
    });
    expect(screen.getByRole("button", { name: /import/i })).toBeDisabled();
  });
});
