import { afterEach, describe, expect, it } from "vitest";
import {
  HOST_RETURN_COALESCE_MS,
  HOST_RETURN_WINDOW_MS,
  isHostReturnRecent,
  observeHostOnline,
  observeSandboxResumes,
  resetHostReturns,
  useHostReturnStore,
} from "@/store/hostReturns";

const online = (entries: Record<string, boolean | null>) => new Map(Object.entries(entries));
const latest = (sessionId: string) => useHostReturnStore.getState().bySession[sessionId];

afterEach(() => resetHostReturns());

describe("host returns", () => {
  it("isn't a return when a host first reads online, is newly watched, or is reported again", () => {
    expect(observeHostOnline(online({ s1: true }), 0)).toEqual([]);
    expect(observeHostOnline(online({ s1: true, s2: true }), 1)).toEqual([]);
    // A remounted observer reports the same readings again.
    expect(observeHostOnline(online({ s1: true, s2: true }), 2)).toEqual([]);
    expect(observeHostOnline(online({ s3: null }), 3)).toEqual([]);
    expect(observeHostOnline(online({ s3: true }), 4)).toEqual([]);
    expect(useHostReturnStore.getState().bySession).toEqual({});
  });

  it("is a return when a host this app saw offline reads online", () => {
    observeHostOnline(online({ s1: false }), 0);
    expect(observeHostOnline(online({ s1: true }), 1_000)).toEqual(["s1"]);
    expect(latest("s1")).toMatchObject({ at: 1_000, sawOnline: true, sawResume: false });
  });

  it("is a return when this app's resume brings a host up, even unseen offline", () => {
    expect(observeSandboxResumes({ s1: 500 }, 500)).toEqual(["s1"]);
    expect(observeSandboxResumes({ s1: 500 }, 600)).toEqual([]);
  });

  it("counts one resume reported by both signals once", () => {
    observeHostOnline(online({ s1: false }), 0);
    expect(observeHostOnline(online({ s1: true }), 1_000)).toEqual(["s1"]);
    const first = latest("s1");
    expect(observeSandboxResumes({ s1: 1_200 }, 1_200)).toEqual([]);
    expect(latest("s1")).toMatchObject({ id: first.id, at: 1_000, sawResume: true });
  });

  it("counts a second offline → online cycle, or a second resume, as a new return in the window", () => {
    observeHostOnline(online({ s1: false }), 0);
    observeHostOnline(online({ s1: true }), 1_000);
    const first = latest("s1");
    observeHostOnline(online({ s1: false }), 3_000);
    expect(observeHostOnline(online({ s1: true }), 5_000)).toEqual(["s1"]);
    expect(latest("s1").id).not.toBe(first.id);
    expect(latest("s1").at).toBe(5_000);

    observeSandboxResumes({ s2: 100 }, 100);
    const resumed = latest("s2");
    expect(observeSandboxResumes({ s2: 2_000 }, 2_000)).toEqual(["s2"]);
    expect(latest("s2").id).not.toBe(resumed.id);
  });

  it("counts an offline → online cycle after a resume as a new return, even inside the window", () => {
    observeHostOnline(online({ s1: true }), 0);
    expect(observeSandboxResumes({ s1: 1_000 }, 1_000)).toEqual(["s1"]);
    const resumed = latest("s1");
    observeHostOnline(online({ s1: false }), 3_000);
    expect(observeHostOnline(online({ s1: true }), 5_000)).toEqual(["s1"]);
    expect(latest("s1").id).not.toBe(resumed.id);
    expect(latest("s1").at).toBe(5_000);
  });

  it("counts a resume after an offline reading that followed an online return as new", () => {
    observeHostOnline(online({ s1: false }), 0);
    expect(observeHostOnline(online({ s1: true }), 1_000)).toEqual(["s1"]);
    observeHostOnline(online({ s1: false }), 2_000);
    expect(observeSandboxResumes({ s1: 3_000 }, 3_000)).toEqual(["s1"]);
  });

  it("doesn't join signals that arrive far apart", () => {
    observeSandboxResumes({ s1: 0 }, 0);
    observeHostOnline(online({ s1: false }), 1);
    expect(observeHostOnline(online({ s1: true }), HOST_RETURN_COALESCE_MS + 1)).toEqual(["s1"]);
  });

  it("stays recent for the window after the return", () => {
    observeSandboxResumes({ s1: 0 }, 0);
    expect(isHostReturnRecent(latest("s1"), HOST_RETURN_WINDOW_MS - 1)).toBe(true);
    expect(isHostReturnRecent(latest("s1"), HOST_RETURN_WINDOW_MS)).toBe(false);
    expect(isHostReturnRecent(undefined, 0)).toBe(false);
  });
});
