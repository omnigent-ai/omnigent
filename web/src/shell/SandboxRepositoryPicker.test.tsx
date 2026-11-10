import { useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, sandboxRepositoryCapabilities } from "@/lib/capabilities";
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

function renderPicker(
  initialSelections: LastSandboxRepo[] = [],
  maxRepos = 10,
  options: {
    single?: boolean;
    catalog?: { name: string; url: string }[];
    urlInput?: boolean;
    branchSelection?: boolean;
  } = {},
) {
  const onChange = vi.fn();
  const onPendingUrlChange = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Harness({
    limit,
    capabilities = options,
  }: {
    limit: number;
    capabilities?: typeof options;
  }) {
    const [selections, setSelections] = useState(initialSelections);
    const [pendingUrl, setPendingUrl] = useState("");
    return (
      <QueryClientProvider client={client}>
        <SandboxRepositoryPicker
          {...capabilities}
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
    setCapabilities: (capabilities: typeof options) =>
      result.rerender(<Harness limit={maxRepos} capabilities={capabilities} />),
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
    fireEvent.click(screen.getByRole("combobox", { name: "Repository" }));
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
      "Enter a valid HTTPS or SSH repository URL without credentials.",
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
  it("single mode consumes typed URLs without Add and keeps branch errors visible", () => {
    const { onChange } = renderPicker([], 1, { single: true, branchSelection: false });
    const input = screen.getByRole("combobox", { name: "Repository URL" });
    fireEvent.change(input, { target: { value: "https://git.example.com/team/sample.git" } });
    expect(onChange).toHaveBeenLastCalledWith([
      { url: "https://git.example.com/team/sample.git", branch: "" },
    ]);
    expect(screen.queryByRole("button", { name: "Add" })).toBeNull();
    expect(screen.queryByLabelText("Branch (optional)")).toBeNull();
    fireEvent.change(input, { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("offers catalog selections without URL or branch entry for restricted providers", () => {
    const catalog = [{ name: "Sample", url: "https://git.example.com/team/sample.git" }];
    const { onChange } = renderPicker([], 10, { catalog, urlInput: false, branchSelection: false });
    expect(screen.queryByRole("textbox", { name: "Repository URL" })).toBeNull();
    fireEvent.click(screen.getByRole("combobox", { name: "Repository" }));
    fireEvent.click(screen.getByRole("option", { name: "Sample" }));
    expect(onChange).toHaveBeenLastCalledWith([{ url: catalog[0].url, branch: "" }]);
    expect(screen.queryByRole("textbox", { name: "Branch for Sample" })).toBeNull();
    expect(fetchGithubRepos).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Remove Sample" }));
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("single mode offers only catalog choices and can clear its selection", () => {
    const catalog = [{ name: "Sample", url: "https://git.example.com/team/sample.git" }];
    const { onChange } = renderPicker([], 1, {
      single: true,
      catalog,
      urlInput: false,
      branchSelection: false,
    });
    expect(screen.queryByLabelText("Repository URL")).toBeNull();
    expect(screen.queryByPlaceholderText(/paste a URL/)).toBeNull();
    const select = screen.getByRole("combobox", { name: "Repository (optional)" });
    fireEvent.change(select, { target: { value: catalog[0].url } });
    expect(onChange).toHaveBeenLastCalledWith([{ url: catalog[0].url, branch: "" }]);
    expect(screen.queryByLabelText("Branch (optional)")).toBeNull();
    fireEvent.change(select, { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("selects distinct repositories with the same display name by keyboard", async () => {
    const catalog = [
      { name: "org/api", url: "https://git.example.com/team/first.git" },
      { name: "org/api", url: "https://git.example.com/team/second.git" },
    ];
    const { onChange } = renderPicker([], 10, { catalog });
    await waitFor(() => expect(fetchGithubRepos).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("combobox", { name: "Repository" }));
    const search = screen.getByPlaceholderText("Search repositories…");
    fireEvent.change(search, { target: { value: "org/api" } });
    await waitFor(() => expect(screen.getAllByRole("option")).toHaveLength(3));
    expect(screen.getAllByRole("option", { selected: true })).toHaveLength(1);
    fireEvent.keyDown(search, { key: "ArrowDown" });
    expect(screen.getAllByRole("option", { selected: true })).toHaveLength(1);
    fireEvent.keyDown(search, { key: "Enter" });
    expect(onChange).toHaveBeenLastCalledWith([{ url: catalog[1].url, branch: "" }]);
  });

  it("retains a single selection through loading and a catalog-only target switch", () => {
    const selection = { url: "https://git.example.com/team/custom.git", branch: "release" };
    const { onChange, setCapabilities } = renderPicker([selection], 1, { single: true });
    setCapabilities({ single: true, ...sandboxRepositoryCapabilities("loading", "example") });
    expect(screen.getByRole("combobox", { name: "Repository (optional)" })).toHaveValue(
      selection.url,
    );
    const branch = screen.getByLabelText("Branch (optional)");
    expect(branch).toHaveValue(selection.branch);
    expect(branch).toHaveAttribute("aria-invalid", "true");
    expect(branch).toHaveAccessibleDescription(/Clear the branch/);
    expect(onChange).not.toHaveBeenCalled();
    setCapabilities({ single: true, urlInput: true, branchSelection: true });
    expect(screen.getByLabelText("Repository URL")).toHaveValue(selection.url);
    expect(screen.getByLabelText("Branch (optional)")).toHaveValue(selection.branch);
    expect(screen.queryByRole("alert")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
    const catalog = [{ name: "Sample", url: "https://git.example.com/team/sample.git" }];
    setCapabilities({ single: true, catalog, urlInput: false, branchSelection: false });
    fireEvent.change(screen.getByLabelText("Branch (optional)"), { target: { value: "" } });
    const select = screen.getByRole("combobox", { name: "Repository (optional)" });
    expect(select).toHaveValue(selection.url);
    expect(select).toHaveAccessibleDescription(/unavailable on this target/);
    fireEvent.change(select, { target: { value: catalog[0].url } });
    expect(onChange).toHaveBeenLastCalledWith([{ url: catalog[0].url, branch: "" }]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("retains unsupported selections and branches until explicitly corrected", () => {
    renderPicker([{ url: "https://git.example.com/team/custom.git", branch: "release" }], 10, {
      catalog: [{ name: "Sample", url: "https://git.example.com/team/sample.git" }],
      urlInput: false,
      branchSelection: false,
    });
    const branch = screen.getByRole("textbox", { name: "Branch for custom" });
    expect(branch).toHaveValue("release");
    expect(branch).toHaveAccessibleDescription(/Clear the branch/);
    fireEvent.change(branch, { target: { value: "" } });
    expect(screen.getByText(/This repository is unavailable on this target/)).toBeVisible();
    expect(screen.queryByRole("textbox", { name: "Branch for custom" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Remove custom" }));
    expect(screen.queryByText(/This repository is unavailable on this target/)).toBeNull();
  });

  it("deduplicates catalog and connected suggestions in multi mode", async () => {
    renderPicker([], 10, { catalog: [{ name: "API", url: "git@github.com:org/api" }] });
    await waitFor(() => expect(fetchGithubRepos).toHaveBeenCalledOnce());
    fireEvent.click(screen.getByRole("combobox", { name: "Repository" }));
    await screen.findByRole("option", { name: /org\/web/ });
    expect(screen.getAllByRole("option")).toHaveLength(2);
    const connectedSuggestion = screen.getByRole("option", { name: /org\/api/ });
    expect(connectedSuggestion).toBeVisible();
    expect(within(connectedSuggestion).getByLabelText("Private repository")).toBeVisible();
  });

  it("single mode deduplicates catalog and connected suggestions and retains unsupported branches", async () => {
    renderPicker([{ url: REPOSITORIES[0].clone_url!, branch: "release" }], 1, {
      single: true,
      branchSelection: false,
      catalog: [{ name: "API", url: "git@github.com:org/api" }],
    });
    await waitFor(() => expect(document.querySelectorAll("datalist option")).toHaveLength(2));
    expect(document.querySelector("datalist")).toHaveTextContent("org/api");
    const branch = screen.getByLabelText("Branch (optional)");
    expect(branch).toHaveValue("release");
    expect(branch).toHaveAttribute("aria-invalid", "true");
    expect(branch).toHaveAccessibleDescription(/Clear the branch/);
    expect(screen.getByLabelText("Repository URL")).not.toHaveAttribute("aria-describedby");
    expect(screen.getByRole("alert")).toHaveTextContent("Clear the branch");
    fireEvent.change(screen.getByLabelText("Branch (optional)"), { target: { value: "" } });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("single mode describes an invalid branch on its branch field", () => {
    const { onChange } = renderPicker(
      [{ url: "https://git.example.com/team/sample.git", branch: "bad branch" }],
      1,
      {
        single: true,
      },
    );
    const branch = screen.getByLabelText("Branch (optional)");
    expect(branch).toHaveAttribute("aria-invalid", "true");
    expect(branch).toHaveAccessibleDescription(
      "Enter a valid branch name, or leave blank for the default branch.",
    );
    const url = screen.getByLabelText("Repository URL");
    expect(url).toHaveAttribute("aria-invalid", "false");
    expect(url).not.toHaveAttribute("aria-describedby");
    fireEvent.change(branch, { target: { value: "main" } });
    fireEvent.change(url, { target: { value: "invalid" } });
    expect(url).toHaveAttribute("aria-invalid", "true");
    expect(url).toHaveAccessibleDescription(/valid HTTPS or SSH repository URL/);
    expect(branch).not.toHaveAttribute("aria-describedby");
    fireEvent.change(url, { target: { value: "" } });
    expect(url).toHaveAccessibleDescription("Choose a repository for this branch.");
    expect(branch).not.toHaveAttribute("aria-describedby");
    expect(onChange).toHaveBeenLastCalledWith([{ url: "", branch: "main" }]);
    fireEvent.change(branch, { target: { value: "" } });
    expect(onChange).toHaveBeenLastCalledWith([]);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("leaves URL entry usable if listing fails and rejects unavailable catalog-only URLs", async () => {
    vi.mocked(fetchGithubRepos).mockRejectedValue(new Error("unavailable"));
    const view = renderPicker([], 1, { single: true });
    expect(
      await screen.findByText("Couldn't load repositories. You can still paste a URL."),
    ).toBeInTheDocument();
    fireEvent.change(screen.getByRole("combobox", { name: "Repository URL" }), {
      target: { value: "https://git.example.com/team/sample.git" },
    });
    expect(screen.queryByRole("alert")).toBeNull();
    view.unmount();
    renderPicker([{ url: "https://git.example.com/team/sample.git", branch: "" }], 1, {
      single: true,
      urlInput: false,
    });
    expect(screen.getByRole("alert")).toHaveTextContent("unavailable on this target");
  });
});
