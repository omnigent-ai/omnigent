import { describe, expect, it } from "vitest";
import { ApiError } from "./sessionsApi";
import { describeSendFailure } from "./sendFailure";
import {
  isServerRestartError,
  SERVER_RESTART_MESSAGE,
  serverRestartMessage,
} from "./serverRestart";

describe("isServerRestartError", () => {
  it("recognizes a restarting app's gateway errors and dropped connections only", () => {
    expect(isServerRestartError(new ApiError(serverRestartMessage(502), 502, null))).toBe(true);
    expect(isServerRestartError(new TypeError("Failed to fetch"))).toBe(true);
    expect(
      isServerRestartError(new TypeError("NetworkError when attempting to fetch resource.")),
    ).toBe(true);
    // The app's own 503, other statuses and ordinary errors don't count.
    expect(
      isServerRestartError(new ApiError("Runner is starting", 503, "runner_unavailable")),
    ).toBe(false);
    expect(isServerRestartError(new ApiError("Not found", 404, "not_found"))).toBe(false);
    expect(isServerRestartError(new Error("boom"))).toBe(false);
  });

  it("describes a send that never reached the server as a restart", () => {
    expect(describeSendFailure(new TypeError("Failed to fetch"))).toEqual({
      message: SERVER_RESTART_MESSAGE,
      code: "",
    });
    expect(describeSendFailure(new Error("boom")).message).toBe("boom");
  });
});
