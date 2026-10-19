import { afterEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import {
  DBCERT_EXPIRY_COMMAND,
  DBCERT_REFRESH_COMMAND,
  parseDbcertExpiry,
  readHostDbcertExpiry,
  startHostDbcertRefresh,
} from "./hostDbcert";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

function shellResult(stdout: string, exitCode = 0): Response {
  return new Response(
    JSON.stringify({
      object: "session.environment.shell_result",
      stdout,
      stderr: "",
      exit_code: exitCode,
      timed_out: false,
    }),
    { status: 200 },
  );
}

describe("parseDbcertExpiry", () => {
  it("reads openssl's notAfter line as UTC", () => {
    expect(parseDbcertExpiry("notAfter=Oct 18 20:08:15 2026 GMT\n")).toBe(
      Date.UTC(2026, 9, 18, 20, 8, 15),
    );
    expect(parseDbcertExpiry("notAfter=Feb  3 04:05:06 2027 GMT")).toBe(
      Date.UTC(2027, 1, 3, 4, 5, 6),
    );
  });

  it("tells a host without dbcert apart from output it can't read", () => {
    expect(parseDbcertExpiry("none\n")).toBeNull();
    expect(parseDbcertExpiry("unable to load certificate")).toBeUndefined();
    expect(parseDbcertExpiry("notAfter=Foo 18 20:08:15 2026 GMT")).toBeUndefined();
  });
});

describe("host shell calls", () => {
  afterEach(() => vi.mocked(authenticatedFetch).mockReset());

  it("reads the expiry through the session's environment shell", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      shellResult("notAfter=Oct 18 20:08:15 2026 GMT\n"),
    );
    expect(await readHostDbcertExpiry("s/1")).toBe(Date.UTC(2026, 9, 18, 20, 8, 15));
    const [url, init] = vi.mocked(authenticatedFetch).mock.calls[0];
    expect(url).toBe("/v1/sessions/s%2F1/resources/environments/default/shell");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(String(init?.body))).toEqual({ command: DBCERT_EXPIRY_COMMAND, timeout: 10 });
  });

  it("treats a host it can't ask as unknown", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response("{}", { status: 409 }));
    expect(await readHostDbcertExpiry("s")).toBeUndefined();
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(shellResult("", 1));
    expect(await readHostDbcertExpiry("s")).toBeUndefined();
    vi.mocked(authenticatedFetch).mockRejectedValueOnce(new Error("offline"));
    expect(await readHostDbcertExpiry("s")).toBeUndefined();
  });

  it("starts dbcert in the background and reports whether the shell ran", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(shellResult(""));
    expect(await startHostDbcertRefresh("s")).toBe(true);
    const init = vi.mocked(authenticatedFetch).mock.calls[0][1];
    expect(JSON.parse(String(init?.body)).command).toBe(DBCERT_REFRESH_COMMAND);
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response("{}", { status: 502 }));
    expect(await startHostDbcertRefresh("s")).toBe(false);
  });
});
