import { beforeEach, describe, expect, it, vi } from "vitest";

import { showToast } from "@/components/ui/toast";
import { showArcaConnectOutput } from "@/lib/nativeBridge";

import { openArcaConnectOutput } from "./arcaConnectOutput";

vi.mock("@/components/ui/toast", () => ({ showToast: vi.fn() }));
vi.mock("@/lib/nativeBridge", () => ({ showArcaConnectOutput: vi.fn() }));

beforeEach(() => vi.clearAllMocks());

describe("openArcaConnectOutput", () => {
  it("opens the output without a notice when the shell shows it", async () => {
    vi.mocked(showArcaConnectOutput).mockResolvedValue(true);
    openArcaConnectOutput();
    await vi.waitFor(() => expect(showArcaConnectOutput).toHaveBeenCalledOnce());
    await Promise.resolve();
    expect(showToast).not.toHaveBeenCalled();
  });

  it("says so when the shell has no output for the connect", async () => {
    vi.mocked(showArcaConnectOutput).mockResolvedValue(false);
    openArcaConnectOutput();
    await vi.waitFor(() =>
      expect(showToast).toHaveBeenCalledWith(
        "There's no connection output to show for this Arca connect.",
      ),
    );
  });
});
