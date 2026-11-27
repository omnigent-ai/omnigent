// The repository choices' policy loads apart from the read path: a load that fails is tried
// again at the next read, without an unhandled rejection.

import { expect, it, vi } from "vitest";

import { chooseReadSource, loadRepoReaderPolicyWithForTests } from "./desktopReadPath";

const PR = "https://github.com/example-org/example-repo/pull/1";

it("tries a failed load of the repository policy again at the next read, and only until it loads", async () => {
  const load = vi
    .fn<() => Promise<unknown>>()
    .mockRejectedValueOnce(new Error("chunk missing"))
    .mockResolvedValue({});
  loadRepoReaderPolicyWithForTests(load);

  chooseReadSource(PR, "session");
  expect(load).toHaveBeenCalledTimes(1);
  // Still loading: the next read waits on the same load.
  chooseReadSource(PR, "mac");
  expect(load).toHaveBeenCalledTimes(1);
  await vi.waitFor(async () => {
    chooseReadSource(PR, "session");
    expect(load).toHaveBeenCalledTimes(2);
  });
  await Promise.resolve();
  chooseReadSource(PR, "mac");
  expect(load).toHaveBeenCalledTimes(2);
});
