import { beforeEach, describe, expect, it, vi } from "vitest";
import { createStudentSuccessAlertSeed } from "./seed";
import {
  dpiaStorageKey,
  fetchDurableDpiaCase,
  loadDurableDpiaCase,
  saveDurableDpiaCase,
} from "./dpiaApi";

const { authenticatedFetchMock } = vi.hoisted(() => ({ authenticatedFetchMock: vi.fn() }));

vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));

function response(snapshot: unknown, revision = 1): Response {
  return new Response(
    JSON.stringify({
      case_id: "student-success-alert",
      revision,
      snapshot,
      created_by: "officer@example.com",
      updated_by: "officer@example.com",
      created_at: 1,
      updated_at: 1,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } },
  );
}

beforeEach(() => {
  localStorage.clear();
  authenticatedFetchMock.mockReset();
});

describe("durable DPIA API", () => {
  it("validates a server snapshot and revision envelope", async () => {
    const snapshot = createStudentSuccessAlertSeed();
    authenticatedFetchMock.mockResolvedValueOnce(response(snapshot, 7));

    await expect(fetchDurableDpiaCase(snapshot.id)).resolves.toMatchObject({
      revision: 7,
      caseData: { id: snapshot.id },
    });
  });

  it("migrates a valid legacy snapshot once and removes it after acknowledgement", async () => {
    const snapshot = createStudentSuccessAlertSeed();
    localStorage.setItem(dpiaStorageKey(snapshot.id), JSON.stringify(snapshot));
    authenticatedFetchMock
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(response(snapshot));

    const loaded = await loadDurableDpiaCase(snapshot.id);

    expect(loaded.revision).toBe(1);
    expect(localStorage.getItem(dpiaStorageKey(snapshot.id))).toBeNull();
    expect(authenticatedFetchMock).toHaveBeenNthCalledWith(
      2,
      "/v1/dpia/cases/student-success-alert",
      expect.objectContaining({ method: "PUT" }),
    );
  });

  it("deduplicates concurrent initial migration loads", async () => {
    const snapshot = createStudentSuccessAlertSeed();
    localStorage.setItem(dpiaStorageKey(snapshot.id), JSON.stringify(snapshot));
    authenticatedFetchMock
      .mockResolvedValueOnce(new Response(null, { status: 404 }))
      .mockResolvedValueOnce(response(snapshot));

    const [first, second] = await Promise.all([
      loadDurableDpiaCase(snapshot.id),
      loadDurableDpiaCase(snapshot.id),
    ]);

    expect(first).toEqual(second);
    expect(authenticatedFetchMock).toHaveBeenCalledTimes(2);
    expect(localStorage.getItem(dpiaStorageKey(snapshot.id))).toBeNull();
  });

  it("keeps the legacy snapshot when the server rejects a write", async () => {
    const snapshot = createStudentSuccessAlertSeed();
    localStorage.setItem(dpiaStorageKey(snapshot.id), JSON.stringify(snapshot));
    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          error: { code: "conflict", message: "changed", current_revision: 4 },
        }),
        { status: 409, headers: { "Content-Type": "application/json" } },
      ),
    );

    await expect(saveDurableDpiaCase(snapshot, 3)).rejects.toEqual(
      expect.objectContaining({ currentRevision: 4 }),
    );
    expect(localStorage.getItem(dpiaStorageKey(snapshot.id))).not.toBeNull();
  });
});
