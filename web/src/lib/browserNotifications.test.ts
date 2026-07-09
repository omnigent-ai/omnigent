import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  getNotificationPermission,
  isNotificationSupported,
  requestNotificationPermission,
  showNotification,
} from "./browserNotifications";

// A controllable stand-in for the global Notification constructor.
// `instances` captures each created notification so tests can assert on
// title/options and fire the onclick handler.
interface FakeNotification {
  title: string;
  options?: NotificationOptions;
  onclick: (() => void) | null;
  close: () => void;
}

let instances: FakeNotification[] = [];

function installNotification(
  permission: NotificationPermission,
  requestImpl?: typeof Notification.requestPermission,
): void {
  const ctor = vi.fn(function (
    this: FakeNotification,
    title: string,
    options?: NotificationOptions,
  ) {
    this.title = title;
    this.options = options;
    this.onclick = null;
    this.close = vi.fn();
    instances.push(this);
  }) as unknown as typeof Notification;
  (ctor as unknown as { permission: NotificationPermission }).permission = permission;
  (ctor as unknown as { requestPermission: unknown }).requestPermission =
    requestImpl ?? vi.fn().mockResolvedValue(permission);
  vi.stubGlobal("Notification", ctor);
}

beforeEach(() => {
  instances = [];
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("isNotificationSupported", () => {
  it("returns true when the Notification API is present", () => {
    installNotification("default");
    expect(isNotificationSupported()).toBe(true);
  });

  it("returns false when the Notification API is absent", () => {
    vi.stubGlobal("Notification", undefined);
    expect(isNotificationSupported()).toBe(false);
  });
});

describe("getNotificationPermission", () => {
  it("returns the current permission when supported", () => {
    installNotification("granted");
    expect(getNotificationPermission()).toBe("granted");
  });

  it("returns null when unsupported", () => {
    vi.stubGlobal("Notification", undefined);
    expect(getNotificationPermission()).toBeNull();
  });
});

describe("requestNotificationPermission", () => {
  it("returns null when unsupported", async () => {
    vi.stubGlobal("Notification", undefined);
    expect(await requestNotificationPermission()).toBeNull();
  });

  it("uses the promise form when requestPermission takes no args", async () => {
    const promiseForm = vi.fn().mockResolvedValue("granted");
    installNotification("default", promiseForm as unknown as typeof Notification.requestPermission);
    expect(await requestNotificationPermission()).toBe("granted");
    expect(promiseForm).toHaveBeenCalledOnce();
  });

  it("adapts the legacy callback form (Safari) to a promise", async () => {
    // A one-arg requestPermission signals the callback form.
    const callbackForm = vi.fn((cb: (p: NotificationPermission) => void) => cb("denied"));
    installNotification(
      "default",
      callbackForm as unknown as typeof Notification.requestPermission,
    );
    expect(await requestNotificationPermission()).toBe("denied");
  });
});

describe("showNotification", () => {
  it("creates a notification with title, body, and tag when granted", () => {
    installNotification("granted");
    const result = showNotification({ title: "My Session", body: "Done", tag: "t1" });
    expect(result).not.toBeNull();
    expect(instances).toHaveLength(1);
    expect(instances[0].title).toBe("My Session");
    expect(instances[0].options).toMatchObject({ body: "Done", tag: "t1" });
  });

  it("returns null and shows nothing when permission is not granted", () => {
    installNotification("default");
    expect(showNotification({ title: "X" })).toBeNull();
    expect(instances).toHaveLength(0);
  });

  it("returns null when unsupported", () => {
    vi.stubGlobal("Notification", undefined);
    expect(showNotification({ title: "X" })).toBeNull();
  });

  it("focuses, runs onClick, and closes when clicked", () => {
    installNotification("granted");
    const focus = vi.spyOn(window, "focus").mockImplementation(() => {});
    const onClick = vi.fn();
    showNotification({ title: "X", onClick });
    instances[0].onclick?.();
    expect(focus).toHaveBeenCalledOnce();
    expect(onClick).toHaveBeenCalledOnce();
    expect(instances[0].close).toHaveBeenCalledOnce();
  });

  it("hands off to the native shell (with navigatePath) instead of a web toast", () => {
    // Under the Electron shell, showNotification routes to the OS notification
    // and forwards navigatePath (not the onClick closure, which can't cross
    // IPC) so the shell can open the conversation on click. No web toast.
    installNotification("granted");
    const electronNotify = vi.fn().mockResolvedValue(true);
    (window as unknown as Record<string, unknown>).omnigentDesktop = {
      kind: "electron",
      setBadgeCount: vi.fn(),
      notify: electronNotify,
    };
    try {
      const result = showNotification({ title: "X", body: "done", navigatePath: "/c/a" });
      expect(result).toBeNull();
      expect(instances).toHaveLength(0);
      expect(electronNotify).toHaveBeenCalledWith({
        title: "X",
        body: "done",
        navigatePath: "/c/a",
      });
    } finally {
      delete (window as unknown as Record<string, unknown>).omnigentDesktop;
    }
  });
});
