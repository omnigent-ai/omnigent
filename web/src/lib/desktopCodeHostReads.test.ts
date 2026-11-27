// One code-host read asks the shell what it offers once, for both its operation and its priority.

import { afterEach, expect, it, vi } from "vitest";

import { queryCodeHost } from "./desktopCodeHostReads";

afterEach(() => {
  delete (window as unknown as { omnigentDesktop?: unknown }).omnigentDesktop;
});

function installShell({ readPriority }: { readPriority?: number } = {}) {
  const getDesktopFeatures = vi.fn(async () => ({
    codeHosts: [{ id: "github", operations: ["pull_request_view"], readPriority }],
  }));
  const codeHostQuery = vi.fn(async (_request: unknown) => ({ ok: true, data: {} }));
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
