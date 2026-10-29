import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";

import { attachClone, cloneKey, detachClone, pendingClones, startClone } from "./cloneRequests";

vi.mock("sonner", () => ({
  toast: { loading: vi.fn(() => "cloning"), success: vi.fn(), error: vi.fn(), dismiss: vi.fn() },
}));

const KEY = cloneKey("conv_src");

function deferred() {
  let resolve: (forkId: string) => void = () => {};
  let reject: (error: Error) => void = () => {};
  const promise = new Promise<string>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

function listener() {
  return { onSuccess: vi.fn(), onError: vi.fn() };
}

beforeEach(() => {
  pendingClones.clear();
  vi.clearAllMocks();
});

describe("cloneRequests", () => {
  it("reports to the dialog showing the clone, without a toast", async () => {
    const run = deferred();
    const shown = listener();
    startClone(KEY, () => run.promise, shown, vi.fn());

    run.resolve("conv_fork");

    await vi.waitFor(() => expect(shown.onSuccess).toHaveBeenCalledWith("conv_fork"));
    expect(toast.loading).not.toHaveBeenCalled();
    expect(pendingClones.has(KEY)).toBe(false);
  });

  it("reports a failure after the dialog closed on a dismissible toast that stays", async () => {
    const run = deferred();
    const request = startClone(KEY, () => run.promise, listener(), vi.fn());
    detachClone(request, KEY);

    run.reject(new Error("403 forbidden"));

    await vi.waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Couldn't create the session", {
        id: "cloning",
        description: "403 forbidden",
        duration: Infinity,
        closeButton: true,
      }),
    );
  });

  it("gives the outcome back to a dialog that shows the clone again", async () => {
    const run = deferred();
    const request = startClone(KEY, () => run.promise, listener(), vi.fn());
    detachClone(request, KEY);
    const reopened = listener();
    attachClone(request, reopened);
    expect(toast.dismiss).toHaveBeenCalledWith("cloning");

    run.reject(new Error("403 forbidden"));

    await vi.waitFor(() => expect(reopened.onError).toHaveBeenCalledWith("403 forbidden"));
    expect(toast.error).not.toHaveBeenCalled();
  });

  it("forgets a clone whose loading toast is dismissed, so a retry can start", () => {
    const request = startClone(KEY, () => deferred().promise, listener(), vi.fn());
    detachClone(request, KEY);
    const options = vi.mocked(toast.loading).mock.calls[0][1];

    options?.onDismiss?.({ id: "cloning" });

    expect(pendingClones.has(KEY)).toBe(false);
  });
});
