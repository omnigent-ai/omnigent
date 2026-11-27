// One code-host read asks the shell what it offers once, for both its operation and its priority,
// and waits out its reader's cooldown without reaching the shell.

import { afterEach, expect, it, vi } from "vitest";

import { queryCodeHost } from "./desktopCodeHostReads";
import {
  githubCooldownKey,
  githubCooldowns,
  resolveAutomaticGitHubReaderWith,
} from "./githubCooldown";

afterEach(() => {
  githubCooldowns.reset();
  resolveAutomaticGitHubReaderWith({ known: () => null, discover: async () => null });
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

it("holds the reader for as long as the shell's rate-limited answer says", async () => {
  const retryAt = Date.now() + 90_000;
  const shell = installShell({
    answer: { ok: false, error: { code: "rate_limited", message: "Slow down", retryAt } },
  });
  const result = await queryCodeHost(request("pull_request_view"));
  expect(result).toMatchObject({ ok: false, error: { code: "rate_limited" } });
  expect(githubCooldowns.heldUntil(githubCooldownKey("github.com", "octo"))).toBe(retryAt);
  // The next read as that reader waits it out here.
  expect(await queryCodeHost(request("pull_request_view"))).toMatchObject({
    error: { code: "rate_limited", local: true },
  });
  expect(shell.codeHostQuery).toHaveBeenCalledTimes(1);
});

it("finds out who an Automatic read reads as while another reader on its host cools down", async () => {
  const shell = installShell();
  resolveAutomaticGitHubReaderWith({ known: () => null, discover: async () => "octo" });
  githubCooldowns.rateLimited(githubCooldownKey("github.com", "other"), Date.now() + 60_000);
  const automatic = { ...request("pull_request_view"), login: undefined };
  // Not this reader's cooldown: it reads, and settles as the reader it found.
  expect((await queryCodeHost(automatic)).ok).toBe(true);
  githubCooldowns.rateLimited(githubCooldownKey("github.com", "octo"), Date.now() + 60_000);
  // Its own reader is held: refused here, without asking the shell.
  expect(await queryCodeHost(automatic)).toMatchObject({
    error: { code: "rate_limited", local: true },
  });
  expect(shell.codeHostQuery).toHaveBeenCalledTimes(1);
});
