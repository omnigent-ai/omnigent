// Settings → Skills against a scripted server: the installed list and its
// controls, browsing a source's catalog and installing from it, and previewing
// a repository by URL.
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsSkillsSection } from "./SettingsSkillsSection";
import { authenticatedFetch } from "@/lib/identity";
import type { InstalledSkill, SkillCatalog, SkillSource } from "@/lib/skillsApi";

const flags = { feature: true, admin: true };
vi.mock("@/lib/CapabilitiesContext", () => ({
  useServerInfo: () => ({ features: { skills_marketplace: flags.feature } }),
}));
vi.mock("@/hooks/useIsAdmin", () => ({ useIsAdmin: () => flags.admin }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked(authenticatedFetch);

const source: SkillSource = {
  id: "src1",
  url: "https://github.com/acme/skills.git",
  ref: null,
  name: "acme/skills",
  created_by: null,
  created_at: 0,
};
const review: InstalledSkill = {
  id: "sk1",
  name: "review",
  display_name: "Code Review",
  description: "Review a diff",
  source_url: "https://github.com/acme/skills.git",
  source: "acme/skills",
  ref: null,
  commit: "a".repeat(40),
  path: "skills/review",
  scope: "user",
  owner_user_id: null,
  enabled: true,
  installed_by: null,
  installed_at: 0,
  updated_at: null,
};
const catalog: SkillCatalog = {
  url: "https://github.com/acme/skills.git",
  ref: null,
  commit: "a".repeat(40),
  skills: [
    {
      name: "review",
      display_name: "Code Review",
      description: "Review a diff",
      path: "skills/review",
      plugin: null,
      installed_id: "sk1",
    },
    {
      name: "triage",
      display_name: null,
      description: "Triage issues",
      path: "skills/triage",
      plugin: "docs",
      installed_id: null,
    },
  ],
  skipped: ["plugin 'remote': source is another repository; not followed"],
};

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const calls: { method: string; url: string; body?: unknown }[] = [];

function script() {
  fetchMock.mockImplementation(async (input, init) => {
    const url = String(input);
    const method = init?.method ?? "GET";
    calls.push({ method, url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    if (url === "/v1/installed-skills" && method === "GET") return json({ data: [review] });
    if (url === "/v1/skill-sources" && method === "GET") return json({ data: [source] });
    if (url.startsWith("/v1/skill-sources/src1/catalog")) return json(catalog);
    if (url === "/v1/skill-catalog")
      return json({ ...catalog, url: "https://github.com/other/repo.git" });
    if (url === "/v1/installed-skills" && method === "POST") return json({ data: [review] }, 201);
    if (url === "/v1/installed-skills/sk1" && method === "PATCH")
      return json({ ...review, enabled: false });
    if (url === "/v1/installed-skills/sk1/content")
      return json({ markdown: "---\nname: review\n---\nReview it." });
    if (url === "/v1/installed-skills/sk1/update") return json(review);
    if (url === "/v1/installed-skills/sk1" && method === "DELETE")
      return new Response(null, { status: 204 });
    return json({ error: { message: `unexpected ${method} ${url}` } }, 500);
  });
}

// Radix activates a tab on focus, so a bare click does not switch tabs in jsdom.
function openBrowse() {
  const tab = screen.getByTestId("skills-tab-browse");
  fireEvent.focus(tab);
  fireEvent.click(tab);
}

function renderSection() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SettingsSkillsSection />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  flags.feature = true;
  flags.admin = true;
  calls.length = 0;
  fetchMock.mockReset();
  script();
});
afterEach(cleanup);

describe("SettingsSkillsSection", () => {
  it("renders nothing when the feature is off", () => {
    flags.feature = false;
    renderSection();
    expect(screen.queryByText("Skills")).toBeNull();
  });

  it("lists installed skills with their source and lets the user disable one", async () => {
    renderSection();
    const row = await screen.findByTestId("installed-skill-review");
    expect(row).toHaveTextContent("Code Review");
    expect(row).toHaveTextContent("acme/skills");
    expect(row).toHaveTextContent("aaaaaaa");

    fireEvent.click(within(row).getByTestId("installed-skill-toggle-review"));
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "PATCH",
        url: "/v1/installed-skills/sk1",
        body: { enabled: false },
      }),
    );
  });

  it("opens the stored SKILL.md from the installed list", async () => {
    renderSection();
    fireEvent.click(await screen.findByRole("button", { name: "Code Review" }));
    expect(await screen.findByTestId("skill-content")).toHaveTextContent("Review it.");
  });

  it("browses a source's catalog and installs a skill from it", async () => {
    renderSection();
    openBrowse();
    expect(await screen.findByTestId("skill-source-src1")).toHaveTextContent("acme/skills");

    const triage = await screen.findByTestId("catalog-skill-triage");
    expect(triage).toHaveTextContent("docs");
    // Already-installed skills show a badge instead of a button.
    expect(within(screen.getByTestId("catalog-skill-review")).queryByRole("button")).toBeNull();
    expect(screen.getByText("1 entries were skipped")).toBeInTheDocument();

    fireEvent.change(screen.getByTestId("install-scope"), { target: { value: "org" } });
    fireEvent.click(within(triage).getByTestId("install-skill-triage"));
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "POST",
        url: "/v1/installed-skills",
        body: {
          source: "https://github.com/acme/skills.git",
          ref: null,
          names: ["triage"],
          scope: "org",
        },
      }),
    );
  });

  it("previews a repository by URL and can install from the preview", async () => {
    renderSection();
    openBrowse();
    fireEvent.change(await screen.findByTestId("skill-url-input"), {
      target: { value: "other/repo" },
    });
    fireEvent.change(screen.getByTestId("skill-url-ref"), { target: { value: "v2" } });
    fireEvent.click(screen.getByRole("button", { name: /Preview skills/ }));

    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "POST",
        url: "/v1/skill-catalog",
        body: { source: "other/repo", ref: "v2" },
      }),
    );
    expect(await screen.findByText(/Previewing/)).toHaveTextContent(
      "https://github.com/other/repo.git",
    );
    fireEvent.click(screen.getByTestId("install-skill-triage"));
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "POST",
        url: "/v1/installed-skills",
        body: {
          source: "https://github.com/other/repo.git",
          ref: null,
          names: ["triage"],
          scope: "user",
        },
      }),
    );
  });

  it("hides source management from non-admins", async () => {
    flags.admin = false;
    renderSection();
    openBrowse();
    await screen.findByTestId("skill-source-src1");
    expect(screen.queryByTestId("skill-source-input")).toBeNull();
    expect(screen.queryByRole("button", { name: /Remove source/ })).toBeNull();
    expect(screen.getByTestId("skill-url-input")).toBeInTheDocument();
  });
});
