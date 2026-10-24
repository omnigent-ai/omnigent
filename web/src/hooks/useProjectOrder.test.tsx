import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { saveProjectOrder } from "@/lib/projectsApi";
import { resolveOrCreateProjectId } from "./useConversations";
import { showToast } from "@/components/ui/toast";
import { useSaveProjectOrder } from "./useProjectOrder";

vi.mock("@/lib/projectsApi", () => ({ saveProjectOrder: vi.fn(), getProjectOrder: vi.fn() }));
vi.mock("./useConversations", () => ({ resolveOrCreateProjectId: vi.fn() }));
vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));

const projects = [
  { id: "a", name: "A" },
  { id: "b", name: "B" },
];
function setup() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  client.setQueryData(["projects"], projects);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  return { client, ...renderHook(() => useSaveProjectOrder(), { wrapper }) };
}

beforeEach(() => vi.resetAllMocks());

describe("saving project order", () => {
  it("previews a move immediately and restores confirmed order after a failed save", async () => {
    let reject!: (reason: Error) => void;
    vi.mocked(saveProjectOrder).mockReturnValue(
      new Promise((_resolve, rejectPromise) => {
        reject = rejectPromise;
      }),
    );
    const { result, client } = setup();
    act(() => result.current.mutate([...projects].reverse()));
    await waitFor(() => expect(saveProjectOrder).toHaveBeenCalledWith(["b", "a"]));
    expect(client.getQueryData(["projects"])).toEqual([...projects].reverse());
    act(() => reject(new Error("offline")));
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(client.getQueryData(["projects"])).toEqual(projects);
    expect(showToast).toHaveBeenCalledWith("Couldn't save project order", { duration: 0 });
  });

  it.each([false, true])(
    "queues newer intent and rolls back to the last confirmed order (first fails: %s)",
    async (firstFails) => {
      const writes: {
        resolve: (value: Awaited<ReturnType<typeof saveProjectOrder>>) => void;
        reject: (error: Error) => void;
      }[] = [];
      vi.mocked(saveProjectOrder).mockImplementation(
        () =>
          new Promise((resolve, reject) => {
            writes.push({ resolve, reject });
          }),
      );
      const { result, client } = setup();
      client.setQueryData(["project-order"], {
        sort_mode: "alphabetical",
        ordered_project_ids: null,
      });
      act(() => result.current.mutate([...projects].reverse()));
      await waitFor(() => expect(writes).toHaveLength(1));
      act(() => result.current.mutate(null));
      await waitFor(() => expect(client.getQueryData(["projects"])).toEqual(projects));
      expect(writes).toHaveLength(1);
      const renamed = { ...projects[0], name: "Renamed", icon: "🔥" };
      client.setQueryData(["projects"], [renamed, projects[1], { id: "c", name: "C" }]);
      act(() =>
        firstFails
          ? writes[0].reject(new Error("offline"))
          : writes[0].resolve({ sort_mode: "manual", ordered_project_ids: ["b", "a"] }),
      );
      await waitFor(() => expect(writes).toHaveLength(2));
      expect(client.getQueryData(["projects"])).toEqual([
        renamed,
        projects[1],
        { id: "c", name: "C" },
      ]);
      expect(showToast).not.toHaveBeenCalled();
      act(() => writes[1].reject(new Error("offline")));
      await waitFor(() => expect(result.current.isError).toBe(true));
      expect(client.getQueryData(["projects"])).toEqual(
        firstFails
          ? [projects[1], { id: "c", name: "C" }, renamed]
          : [projects[1], renamed, { id: "c", name: "C" }],
      );
      expect(client.getQueryData(["project-order"])).toEqual(
        firstFails
          ? { sort_mode: "alphabetical", ordered_project_ids: null }
          : { sort_mode: "manual", ordered_project_ids: ["b", "a"] },
      );
    },
  );

  it.each([undefined, "Deleting", "Creating", "Saving"] as const)(
    "promotes only idle legacy folders (pending: %s)",
    async (pending) => {
      vi.mocked(resolveOrCreateProjectId).mockResolvedValue("legacy-id");
      vi.mocked(saveProjectOrder).mockResolvedValue({
        ordered_project_ids: ["legacy-id", "a"],
        sort_mode: "manual",
      });
      const { result, client } = setup();
      const order = [{ id: null, name: "Legacy" }, projects[0]];
      client.setQueryData(
        ["projects"],
        [
          {
            ...order[0],
            pending,
            ...(pending === "Saving" ? { name: "Renamed", previousName: "Legacy" } : {}),
          },
          projects[0],
        ],
      );
      act(() => result.current.mutate(order));
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      if (pending) expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
      else expect(resolveOrCreateProjectId).toHaveBeenCalledWith("Legacy");
      expect(saveProjectOrder).toHaveBeenCalledWith(pending ? ["a"] : ["legacy-id", "a"]);
    },
  );

  it("deduplicates queued aliases when a deleted legacy name is reused", async () => {
    const { result, client } = setup();
    const queued = [{ id: null, name: "Legacy" }, projects[0]];
    client.setQueryData(["projects"], [{ ...projects[0], name: "Legacy", pending: "Saving" }]);
    vi.mocked(saveProjectOrder).mockResolvedValue({
      sort_mode: "manual",
      ordered_project_ids: ["a"],
    });
    act(() => result.current.mutate(queued));
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
    expect(saveProjectOrder).toHaveBeenCalledExactlyOnceWith(["a"]);
  });

  it.each([false, true])(
    "rechecks queued projects before promotion (deletion finished: %s)",
    async (deleted) => {
      let finish!: (value: Awaited<ReturnType<typeof saveProjectOrder>>) => void;
      vi.mocked(saveProjectOrder)
        .mockReturnValueOnce(
          new Promise((resolve) => {
            finish = resolve;
          }),
        )
        .mockResolvedValueOnce({ sort_mode: "manual", ordered_project_ids: ["b", "a"] });
      const { result, client } = setup();
      const legacy = { id: null, name: "Legacy" };
      client.setQueryData(["projects"], [...projects, legacy]);
      act(() => result.current.mutate(null));
      await waitFor(() => expect(saveProjectOrder).toHaveBeenCalledOnce());
      act(() => result.current.mutate([legacy, ...[...projects].reverse()]));
      await waitFor(() =>
        expect(client.getQueryData(["projects"])).toEqual([legacy, ...[...projects].reverse()]),
      );
      client.setQueryData(
        ["projects"],
        [
          { id: "replacement", name: "A" },
          { ...projects[0], name: "Renamed" },
          projects[1],
          ...(deleted ? [] : [{ ...legacy, pending: "Deleting" }]),
        ],
      );
      act(() => finish({ sort_mode: "alphabetical", ordered_project_ids: null }));
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
      expect(saveProjectOrder).toHaveBeenLastCalledWith(["b", "a"]);
    },
  );

  it.each(["alphabetical", "manual"] as const)(
    "restores %s after an uncached save fails",
    async (sort_mode) => {
      let fail!: (error: Error) => void;
      vi.mocked(saveProjectOrder).mockReturnValueOnce(
        new Promise((_resolve, reject) => {
          fail = reject;
        }),
      );
      const { result, client } = setup();
      client.removeQueries({ queryKey: ["projects"] });
      const preference = { sort_mode, ordered_project_ids: ["b", "a"] };
      client.setQueryData(["project-order"], preference);
      act(() => result.current.mutate(projects));
      await waitFor(() => expect(saveProjectOrder).toHaveBeenCalledOnce());
      client.setQueryData(["projects"], [...projects, { id: "c", name: "C" }].reverse());
      act(() => fail(new Error("offline")));
      await waitFor(() => expect(result.current.isError).toBe(true));
      expect(client.getQueryData(["projects"])).toEqual([
        ...(sort_mode === "manual" ? [...projects].reverse() : projects),
        { id: "c", name: "C" },
      ]);
      expect(client.getQueryData(["project-order"])).toEqual(preference);
    },
  );

  it.each([false, true])(
    "resets without promoting folders (projects uncached: %s)",
    async (uncached) => {
      vi.mocked(saveProjectOrder).mockResolvedValue({
        ordered_project_ids: ["b", "a"],
        sort_mode: "alphabetical",
      });
      const { result, client } = setup();
      if (uncached) client.removeQueries({ queryKey: ["projects"] });
      client.setQueryData(["project-order"], { ordered_project_ids: ["b", "a"] });
      act(() => result.current.mutate(null));
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      expect(saveProjectOrder).toHaveBeenCalledWith(null);
      expect(resolveOrCreateProjectId).not.toHaveBeenCalled();
      if (uncached) expect(client.getQueryData(["projects"])).toBeUndefined();
      else expect(client.getQueryState(["projects"])?.isInvalidated).toBe(true);
      expect(client.getQueryState(["project-order"])?.isInvalidated).toBe(true);
    },
  );
});
