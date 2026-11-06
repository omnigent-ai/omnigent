import { useEffect } from "react";
import { act, cleanup, render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const fetchMock = vi.fn();
const mountApp = vi.fn();
const reload = vi.fn();
const cookieName = "omnigent_admin_mode_test";
const identityResponse = {
  user_id: "admin@example.test",
  is_admin: false,
  can_enable_admin_mode: true,
  admin_mode_enabled: false,
  admin_mode_cookie_name: cookieName,
};
const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status });

function AppContent() {
  useEffect(() => {
    mountApp();
  }, []);
  return <div>App content</div>;
}

beforeEach(() => {
  vi.resetModules();
  vi.useFakeTimers();
  fetchMock.mockReset();
  mountApp.mockReset();
  reload.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("location", { protocol: "http:", reload });
  document.cookie = `${cookieName}=; Path=/; Max-Age=0`;
  window.history.replaceState({}, "", "/");
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  document.cookie = `${cookieName}=; Path=/; Max-Age=0`;
  window.history.replaceState({}, "", "/");
});

async function bootstrap() {
  const mode = await import("@/lib/adminMode");
  mode.initializeAdminMode();
  const { AdminModeBoundary } = await import("./AdminModeBoundary");
  return {
    mode,
    app: (
      <AdminModeBoundary>
        <AppContent />
      </AdminModeBoundary>
    ),
  };
}

describe("admin mode bootstrap", () => {
  it("keeps app effects gated beyond the former boot timeout until the cookie arrives", async () => {
    let finish!: (response: Response) => void;
    fetchMock.mockReturnValue(
      new Promise<Response>((resolve) => {
        finish = resolve;
      }),
    );
    const { app } = await bootstrap();
    const view = render(app);

    await act(async () => vi.advanceTimersByTimeAsync(2_000));
    expect(mountApp).not.toHaveBeenCalled();
    expect(view.getByRole("status")).toHaveTextContent("Loading Omnigent");

    await act(async () => {
      document.cookie = `${cookieName}=off; Path=/`;
      finish(jsonResponse(identityResponse));
    });
    expect(mountApp).toHaveBeenCalledOnce();
    expect(view.getByText("App content")).toBeInTheDocument();
  });

  it.each(["network failure", "unreadable cookie"])(
    "retries a %s while keeping app effects unmounted",
    async (failure) => {
      if (failure === "network failure") fetchMock.mockRejectedValueOnce(new TypeError("offline"));
      else fetchMock.mockResolvedValueOnce(jsonResponse(identityResponse));
      fetchMock.mockImplementationOnce(async () => {
        document.cookie = `${cookieName}=off; Path=/`;
        return jsonResponse(identityResponse);
      });
      const { app } = await bootstrap();
      const view = render(app);
      await act(async () => {});
      expect(mountApp).not.toHaveBeenCalled();
      expect(view.getByRole("alert")).toHaveTextContent("Retrying");

      await act(async () => vi.advanceTimersByTimeAsync(1_000));
      expect(mountApp).toHaveBeenCalledOnce();
      expect(fetchMock).toHaveBeenCalledTimes(2);
    },
  );

  it("allows old servers without admin mode support to finish bootstrap", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ user_id: "admin@example.test", is_admin: true }));
    const { app } = await bootstrap();
    render(app);
    await act(async () => {});
    expect(mountApp).toHaveBeenCalledOnce();
  });

  it.each(["/login", "/register"])("allows a signed-out form at %s", async (path) => {
    window.history.replaceState({}, "", path);
    fetchMock.mockResolvedValue(jsonResponse({ user_id: null, login_url: "/login" }, 401));
    const { app } = await bootstrap();
    render(app);
    await act(async () => {});
    expect(mountApp).toHaveBeenCalledOnce();
  });

  it("keeps app effects unmounted while redirecting to sign-in", async () => {
    const originalLocation = window.location;
    Object.defineProperty(window, "location", {
      configurable: true,
      value: { pathname: "/c/demo", search: "", href: "", origin: "http://localhost" },
    });
    try {
      fetchMock.mockResolvedValue(jsonResponse({ user_id: null, login_url: "/login" }, 401));
      const { app } = await bootstrap();
      const view = render(app);
      await act(async () => {});
      expect(mountApp).not.toHaveBeenCalled();
      expect(view.getByRole("status")).toHaveTextContent("Opening sign-in");
      expect(window.location.href).toBe("/login?return_to=%2Fc%2Fdemo");
    } finally {
      Object.defineProperty(window, "location", { configurable: true, value: originalLocation });
    }
  });

  it.each(["server", "cookie path", "opt-in"])(
    "reloads cached identity before mounting after the %s changes",
    async (change) => {
      const { setOmnigentHostConfig } = await import("@/lib/host");
      setOmnigentHostConfig({
        fetcher: fetchMock,
        serverIdentity: "server-a",
        adminModeCookiePath: change === "opt-in" ? undefined : "/",
      });
      if (change !== "opt-in") document.cookie = `${cookieName}=off; Path=/`;
      fetchMock.mockResolvedValue(jsonResponse(identityResponse));
      const { app, mode } = await bootstrap();
      const first = render(app);
      const { resolveIdentity } = await import("@/lib/identity");
      await act(async () => {
        await resolveIdentity();
      });
      expect(mountApp).toHaveBeenCalledOnce();
      first.unmount();

      setOmnigentHostConfig({
        fetcher: fetchMock,
        serverIdentity: change === "server" ? "server-b" : "server-a",
        adminModeCookiePath: change === "cookie path" ? "/embed" : "/",
      });
      mode.initializeAdminMode();
      render(app);
      await act(async () => {});
      expect(mountApp).toHaveBeenCalledOnce();
      expect(reload).toHaveBeenCalledOnce();
      expect(fetchMock).toHaveBeenCalledOnce();
    },
  );

  it.each(["unchanged", "changed", "deleted"])(
    "checks a %s scoped cookie before mounting cached identity again",
    async (change) => {
      document.cookie = `${cookieName}=off; Path=/`;
      fetchMock.mockResolvedValue(jsonResponse(identityResponse));
      const { app, mode } = await bootstrap();
      const first = render(app);
      await act(async () => {});
      expect(mountApp).toHaveBeenCalledOnce();
      first.unmount();

      if (change !== "unchanged") {
        document.cookie = `${cookieName}=${change === "deleted" ? "; Max-Age=0" : "changed"}; Path=/`;
      }
      window.dispatchEvent(new Event("focus"));
      expect(reload).not.toHaveBeenCalled();

      mode.initializeAdminMode();
      render(app);
      await act(async () => {});
      expect(mountApp).toHaveBeenCalledTimes(change === "unchanged" ? 2 : 1);
      expect(reload).toHaveBeenCalledTimes(change === "unchanged" ? 0 : 1);
      expect(fetchMock).toHaveBeenCalledOnce();
    },
  );
});
