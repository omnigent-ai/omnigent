import { afterEach, describe, expect, it, vi } from "vitest";

import { getSessionSlim } from "@/lib/sessionsApi";
import type { Session } from "@/lib/types";
import { sendVerdictFromServer, snapshotRefusesSend } from "./userInputDelivery";

vi.mock("@/lib/sessionsApi", () => ({ getSessionSlim: vi.fn() }));

const getSessionSlimMock = vi.mocked(getSessionSlim);

function snapshot(
  status: Session["status"],
  lastTaskError: Session["lastTaskError"] = null,
): Session {
  return { id: "conv_1", status, lastTaskError } as Session;
}

const rejection = (itemId?: string) => ({
  code: "runner_rejected_event",
  message: "The runner refused the message.",
  ...(itemId === undefined ? {} : { item_id: itemId }),
});

afterEach(() => {
  getSessionSlimMock.mockReset();
});

describe("snapshotRefusesSend", () => {
  it("applies an attributed rejection only to the send it names, whatever the status", () => {
    expect(snapshotRefusesSend(snapshot("idle", rejection("item_1")), "item_1")).toBe(true);
    expect(snapshotRefusesSend(snapshot("running", rejection("item_1")), "item_1")).toBe(true);
    expect(snapshotRefusesSend(snapshot("failed", rejection("item_2")), "item_1")).toBe(false);
  });

  it("applies an unattributed rejection to any send, but only while the session is failed", () => {
    expect(snapshotRefusesSend(snapshot("failed", rejection()), "item_1")).toBe(true);
    expect(snapshotRefusesSend(snapshot("idle", rejection()), "item_1")).toBe(false);
  });

  it("ignores other task errors and a snapshot without one", () => {
    const otherError = { code: "harness_crashed", message: "Crashed.", item_id: "item_1" };
    expect(snapshotRefusesSend(snapshot("failed", otherError), "item_1")).toBe(false);
    expect(snapshotRefusesSend(snapshot("failed", null), "item_1")).toBe(false);
    expect(snapshotRefusesSend(snapshot("failed", undefined), "item_1")).toBe(false);
  });
});

describe("sendVerdictFromServer", () => {
  it("reads the session's snapshot and reports the send refused", async () => {
    getSessionSlimMock.mockResolvedValue(snapshot("failed", rejection("item_1")));
    await expect(sendVerdictFromServer("conv_1", "item_1")).resolves.toBe("refused");
    expect(getSessionSlimMock).toHaveBeenCalledWith("conv_1");
  });

  it("reports the send delivered when the snapshot records no refusal of it", async () => {
    getSessionSlimMock.mockResolvedValue(snapshot("idle", rejection("item_other")));
    await expect(sendVerdictFromServer("conv_1", "item_1")).resolves.toBe("delivered");
  });

  it("has no verdict when the snapshot can't be read", async () => {
    getSessionSlimMock.mockRejectedValue(new Error("offline"));
    await expect(sendVerdictFromServer("conv_1", "item_1")).resolves.toBeNull();
  });
});
