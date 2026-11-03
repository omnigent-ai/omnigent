import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO } from "@/lib/capabilities";
import { fetchGithubBranches, fetchGithubRepos, type GithubRepo } from "@/lib/githubIntegration";
import type { LastSandboxRepo } from "@/lib/repoPreferences";
import { SandboxRepositoryPicker } from "./SandboxRepositoryPicker";

vi.mock("@/lib/CapabilitiesContext", () => ({ useServerInfo: vi.fn() }));
vi.mock("@/lib/githubIntegration", () => ({
  fetchGithubBranches: vi.fn(),
  fetchGithubRepos: vi.fn(),
}));

const REPOSITORIES: GithubRepo[] = ["api", "web"].map((name) => ({
  full_name: `org/${name}`,
  clone_url: `https://github.com/org/${name}.git`,
  default_branch: "main",
  private: true,
  pushed_at: null,
}));

function renderPicker(initialSelections: LastSandboxRepo[] = [], maxRepos = 10) {
  const onChange = vi.fn();
  const onPendingUrlChange = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Harness({ limit }: { limit: number }) {
    const [selections, setSelections] = useState(initialSelections);
    const [pendingUrl, setPendingUrl] = useState("");
    return (
      <QueryClientProvider client={client}>
        <SandboxRepositoryPicker
          selections={selections}
          onChange={(next) => {
            setSelections(next);
            onChange(next);
          }}
          maxRepos={limit}
          pendingUrl={pendingUrl}
          onPendingUrlChange={(url) => {
            setPendingUrl(url);
            onPendingUrlChange(url);
          }}
          testIdPrefix="sandbox-repo"
        />
      </QueryClientProvider>
    );
  }
  const result = render(<Harness limit={maxRepos} />);
  return {
    ...result,
    onChange,
    onPendingUrlChange,
    setLimit: (limit: number) => result.rerender(<Harness limit={limit} />),
  };
}

describe("SandboxRepositoryPicker", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(useServerInfo).mockReturnValue({
      ...FALLBACK_SERVER_INFO,
      enabled_connections: ["github"],
    });
    vi.mocked(fetchGithubRepos).mockResolvedValue({ connected: true, repos: REPOSITORIES });
    vi.mocked(fetchGithubBranches).mockResolvedValue({
      connected: true,
      branches: ["main", "dev"],
    });
  });
  afterEach(cleanup);

  it("matches pasted clone URLs to GitHub repositories and excludes selected repositories", async () => {
    const { onChange } = renderPicker([
      { url: "git@GitHub.com:Org/API", branch: "feature/orders" },
    ]);
    await screen.findByRole("combobox", { name: "Branch for org/api" });
    fireEvent.click(screen.getByRole("combobox", { name: "GitHub repository" }));
    expect(screen.queryByRole("option", { name: /org\/api/ })).toBeNull();
    fireEvent.click(screen.getByRole("option", { name: /org\/web/ }));
    expect(onChange).toHaveBeenLastCalledWith([
      { url: "git@GitHub.com:Org/API", branch: "feature/orders" },
      { url: "https://github.com/org/web.git", branch: "" },
    ]);
    fireEvent.change(screen.getByRole("textbox", { name: "Repository URL" }), {
      target: { value: "https://GitHub.com/ORG/api.git/" },
    });
    expect(
      screen.getByText("This repository is already selected. Clear this URL to continue."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
  });

  it("allows a custom branch when GitHub cannot list branches and can restore the default", async () => {
    vi.mocked(fetchGithubBranches).mockRejectedValue(new Error("unavailable"));
    const { onChange } = renderPicker([{ url: REPOSITORIES[0].clone_url!, branch: "" }]);
    fireEvent.click(await screen.findByRole("combobox", { name: "Branch for org/api" }));
    await screen.findByText("Couldn't load branches. Enter a branch name to use it.");
    fireEvent.change(screen.getByTestId("sandbox-repo-branch-search"), {
      target: { value: "feature#invalid" },
    });
    expect(screen.queryByRole("option", { name: "Use “feature#invalid”" })).toBeNull();
    expect(
      screen.getByText("Enter a valid branch name, or leave blank for the default branch."),
    ).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
    const longBranch = "b".repeat(256);
    fireEvent.change(screen.getByTestId("sandbox-repo-branch-search"), {
      target: { value: longBranch },
    });
    expect(screen.queryByRole("option", { name: `Use “${longBranch}”` })).toBeNull();
    expect(
      screen.getByText("Repository URL and branch must be at most 256 characters together."),
    ).toBeVisible();
    fireEvent.change(screen.getByTestId("sandbox-repo-branch-search"), {
      target: { value: "release/next" },
    });
    fireEvent.click(screen.getByRole("option", { name: "Use “release/next”" }));
    expect(onChange).toHaveBeenLastCalledWith([
      { url: REPOSITORIES[0].clone_url, branch: "release/next" },
    ]);
    fireEvent.click(screen.getByRole("combobox", { name: "Branch for org/api" }));
    fireEvent.click(screen.getByRole("option", { name: "Default (main)" }));
    expect(onChange).toHaveBeenLastCalledWith([{ url: REPOSITORIES[0].clone_url, branch: "" }]);
  });

  it("distinguishes an inherited pinned default branch from following the default", async () => {
    const { onChange } = renderPicker([{ url: REPOSITORIES[0].clone_url!, branch: "main" }]);
    const branchPicker = await screen.findByRole("combobox", { name: "Branch for org/api" });
    fireEvent.click(branchPicker);
    expect(screen.getByRole("option", { name: "Default (main)" })).toHaveAttribute(
      "data-checked",
      "false",
    );
    const pinned = screen.getByRole("option", { name: "main (pinned)" });
    expect(pinned).toHaveAttribute("data-checked", "true");
    fireEvent.click(pinned);
    expect(onChange).toHaveBeenLastCalledWith([{ url: REPOSITORIES[0].clone_url, branch: "main" }]);
    fireEvent.click(branchPicker);
    fireEvent.click(screen.getByRole("option", { name: "Default (main)" }));
    expect(onChange).toHaveBeenLastCalledWith([{ url: REPOSITORIES[0].clone_url, branch: "" }]);
  });

  it("edits and removes inherited duplicate repositories independently", async () => {
    const url = "https://git.example.com/org/api";
    const { onChange } = renderPicker([
      { url, branch: "main" },
      { url, branch: "stable" },
    ]);
    const branches = screen.getAllByRole("textbox", { name: "Branch for api" });
    fireEvent.change(branches[1], { target: { value: "release" } });
    expect(branches[0]).toHaveValue("main");
    expect(onChange).toHaveBeenLastCalledWith([
      { url, branch: "main" },
      { url, branch: "release" },
    ]);
    fireEvent.click(screen.getAllByRole("button", { name: "Remove api" })[0]);
    expect(screen.getByRole("textbox", { name: "Branch for api" })).toHaveValue("release");
    expect(onChange).toHaveBeenLastCalledWith([{ url, branch: "release" }]);
    await waitFor(() => expect(fetchGithubRepos).toHaveBeenCalledOnce());
  });

  it("keeps path casing significant for repositories outside GitHub", async () => {
    const { onChange } = renderPicker([{ url: "https://git.example.com/Org/API", branch: "" }]);
    fireEvent.change(screen.getByRole("textbox", { name: "Repository URL" }), {
      target: { value: "https://git.example.com/org/api" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add" }));
    expect(onChange).toHaveBeenLastCalledWith([
      { url: "https://git.example.com/Org/API", branch: "" },
      { url: "https://git.example.com/org/api", branch: "" },
    ]);
    await waitFor(() => expect(fetchGithubRepos).toHaveBeenCalledOnce());
  });

  it("loads only the opened repository's branches and reuses them when reopened", async () => {
    renderPicker(REPOSITORIES.map((repo) => ({ url: repo.clone_url!, branch: "" })));
    const branchPicker = await screen.findByRole("combobox", { name: "Branch for org/api" });
    expect(fetchGithubBranches).not.toHaveBeenCalled();
    fireEvent.click(branchPicker);
    fireEvent.click(await screen.findByRole("option", { name: "dev" }));
    expect(fetchGithubBranches).toHaveBeenCalledExactlyOnceWith("org/api");
    fireEvent.click(branchPicker);
    expect(screen.getByRole("option", { name: "dev" })).toBeVisible();
    expect(fetchGithubBranches).toHaveBeenCalledOnce();
  });

  it("retains repository branches when switching to a provider with a smaller limit", async () => {
    const { setLimit, onChange } = renderPicker([
      { url: "https://git.example.com/org/api", branch: "release" },
      { url: "https://git.example.com/org/web", branch: "feature/ui" },
    ]);
    setLimit(1);
    expect(screen.getAllByTestId("sandbox-repo-row")).toHaveLength(2);
    expect(screen.getByRole("alert")).toHaveTextContent("at most 1 repository");
    expect(screen.getByRole("textbox", { name: "Branch for web" })).toHaveValue("feature/ui");
    expect(screen.queryByRole("textbox", { name: "Repository URL" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove api" }));
    expect(onChange).toHaveBeenLastCalledWith([
      { url: "https://git.example.com/org/web", branch: "feature/ui" },
    ]);
    expect(screen.queryByRole("alert")).toBeNull();
    await waitFor(() => expect(fetchGithubRepos).toHaveBeenCalledOnce());
  });

  it("retains an unfinished URL when the provider limit hides the repository menu", async () => {
    const { setLimit, onPendingUrlChange } = renderPicker([
      { url: REPOSITORIES[0].clone_url!, branch: "" },
    ]);
    fireEvent.change(screen.getByRole("textbox", { name: "Repository URL" }), {
      target: { value: "https://git.example.com/org/another" },
    });
    setLimit(1);
    expect(screen.getByRole("textbox", { name: "Repository URL" })).toHaveValue(
      "https://git.example.com/org/another",
    );
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    fireEvent.change(screen.getByRole("textbox", { name: "Repository URL" }), {
      target: { value: "" },
    });
    expect(onPendingUrlChange).toHaveBeenLastCalledWith("");
    expect(screen.queryByRole("textbox", { name: "Repository URL" })).toBeNull();
    await waitFor(() => expect(fetchGithubRepos).toHaveBeenCalledOnce());
  });

  it("falls back to validated URL entry when GitHub repositories fail to load", async () => {
    vi.mocked(fetchGithubRepos).mockRejectedValue(new Error("unavailable"));
    const { onChange, onPendingUrlChange } = renderPicker();
    await screen.findByText(
      "Couldn't load your GitHub repositories. Paste a repository URL below.",
    );
    const input = screen.getByRole("textbox", { name: "Repository URL" });
    fireEvent.change(input, { target: { value: "https://user:token@github.com/org/repo" } });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(input, { target: { value: "https://git.example.com/org/my+repo" } });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(input).toHaveAccessibleDescription(
      "Enter an HTTPS or SSH URL ending in a repository name.",
    );
    fireEvent.change(input, {
      target: { value: `https://git.example.com/org/${"r".repeat(256)}` },
    });
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(input).toHaveAccessibleDescription(
      "Repository URL and branch must be at most 256 characters together.",
    );
    fireEvent.change(input, { target: { value: "https://git.example.com/org/repo" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPendingUrlChange).toHaveBeenLastCalledWith("");
    const branchInput = within(screen.getByTestId("sandbox-repo-row")).getByRole("textbox");
    fireEvent.change(branchInput, { target: { value: "my branch" } });
    expect(branchInput).toHaveAttribute("aria-invalid", "true");
    expect(branchInput).toHaveAccessibleDescription(
      "Enter a valid branch name, or leave blank for the default branch.",
    );
    fireEvent.change(branchInput, { target: { value: "b".repeat(256) } });
    expect(branchInput).toHaveAttribute("aria-invalid", "true");
    expect(branchInput).toHaveAccessibleDescription(
      "Repository URL and branch must be at most 256 characters together.",
    );
    fireEvent.change(branchInput, {
      target: { value: "release" },
    });
    expect(branchInput).not.toHaveAttribute("aria-invalid");
    expect(onChange).toHaveBeenLastCalledWith([
      { url: "https://git.example.com/org/repo", branch: "release" },
    ]);
  });
});
