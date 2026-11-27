// One code-host read asks the shell what it offers once, for both its operation and its priority,
// and waits out its reader's cooldown without reaching the shell.

import { afterEach, expect, it, vi } from "vitest";

import { queryCodeHost } from "./desktopCodeHostReads";
import { githubCooldownKey, githubCooldowns } from "./githubCooldown";

afterEach(() => {
  githubCooldowns.reset();
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

function installShell({
  readPriority,
  answer = { ok: true, data: {} },
}: { readPriority?: number; answer?: unknown } = {}) {
  const getDesktopFeatures = vi.fn(async () => ({
    codeHosts: [{ id: "github", operations: ["pull_request_view"], readPriority }],
  }));
  const codeHostQuery = vi.fn(async (_request: unknown) => answer);
  (window as unknown as { omnigentDesktop: unknown }).omnigentDesktop = {
    kind: "electron",
    getDesktopFeatures,
    codeHostQuery,
    codeHostQueryCancel: vi.fn(async () => undefined),
  };
  return { getDesktopFeatures, codeHostQuery };
}

const request = (operation: string) => ({
  provider: "github" as const,
  host: "github.com",
  login: "octo",
  operation,
  params: {},
});

it("reads the shell's features once per read, with the priority it advertises", async () => {
  const shell = installShell({ readPriority: 1 });
  const result = await queryCodeHost(request("pull_request_view"), { priority: "background" });
  expect(result.ok).toBe(true);
  expect(shell.getDesktopFeatures).toHaveBeenCalledTimes(1);
  expect(shell.codeHostQuery.mock.calls[0]![0]).toMatchObject({ priority: "background" });
});

it("sends no priority to a shell that doesn't advertise one", async () => {
  const shell = installShell();
  await queryCodeHost(request("pull_request_view"));
  expect(shell.codeHostQuery.mock.calls[0]![0]).not.toHaveProperty("priority");
});

it("answers unsupported for an operation the shell doesn't offer, without asking it", async () => {
  const shell = installShell({ readPriority: 1 });
  const result = await queryCodeHost(request("pull_request_merge"));
  expect(result).toMatchObject({ ok: false, error: { code: "unsupported" } });
  expect(shell.codeHostQuery).not.toHaveBeenCalled();
});

it("refuses a read while its reader cools down, without asking the shell", async () => {
  const shell = installShell();
  githubCooldowns.rateLimited(githubCooldownKey("github.com", "octo"), Date.now() + 60_000);
  const result = await queryCodeHost(request("pull_request_view"));
  expect(result).toMatchObject({ ok: false, error: { code: "rate_limited", local: true } });
  expect(shell.codeHostQuery).not.toHaveBeenCalled();
});

it("starts the reader's cooldown from a partial answer that met a limit", async () => {
  const retryAt = Date.now() + 120_000;
  installShell({
    answer: { ok: true, data: { partial: { cause: "rate_limited", retryAt } } },
  });
  const result = await queryCodeHost(request("pull_request_view"));
  expect(result.ok).toBe(true);
  expect(githubCooldowns.heldUntil(githubCooldownKey("github.com", "octo"))).toBe(retryAt);
});
