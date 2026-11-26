import { describe, expect, it } from "vitest";
import { ApiError } from "@/lib/sessionsApi";
import { provesNotDelivered } from "./sendRefusal";

describe("provesNotDelivered", () => {
  it("takes only an answer given before forwarding as proof the runner never got it", () => {
    expect(provesNotDelivered(new ApiError("Bad", 400, "invalid_request"))).toBe(true);
    expect(provesNotDelivered(new ApiError("Offline", 503, "host_offline"))).toBe(true);
    // The runner may have taken it before its answer was lost.
    expect(provesNotDelivered(new ApiError("Gone", 503, "runner_unavailable"))).toBe(false);
    expect(provesNotDelivered(new ApiError("Boom", 500, null))).toBe(false);
    expect(provesNotDelivered(new TypeError("Failed to fetch"))).toBe(false);
  });
});
