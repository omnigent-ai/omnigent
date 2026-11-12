import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const supportsBrowser = vi.fn(() => true);
vi.mock("@/lib/nativeBridge", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  supportsBrowser: () => supportsBrowser(),
}));

import { backgroundSessionTitlesRequestHeaders } from "@/lib/backgroundSessionTitlesPreferences";
import { createSideChat, forkSession } from "@/lib/sessionsApi";
import {
  BrowserAgentToolsSettingsGroup,
  UPDATE_DESKTOP_NOTE,
} from "./BrowserAgentToolsSettingsGroup";
import {
  BROWSER_AGENT_TOOLS_HEADER,
  browserAgentToolsRequestHeaders,
  readBrowserAgentToolsEnabled,
  writeBrowserAgentToolsEnabled,
} from "./preferences";
import { browserAgentToolsOffError } from "./relayGate";

beforeEach(() => {
  window.localStorage.clear();
  supportsBrowser.mockReturnValue(true);
  // A desktop app with the tools: it keeps the tabs in main.
  Object.assign(window, { omnigentDesktop: { browserTabs: vi.fn(async () => ({ ok: true })) } });
});

afterEach(() => {
  delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
});

describe("browser agent tools preference", () => {
  it("is off by default, so sessions get the trusted click and type only", () => {
    expect(readBrowserAgentToolsEnabled()).toBe(false);
    expect(browserAgentToolsRequestHeaders()).toEqual({ [BROWSER_AGENT_TOOLS_HEADER]: "trusted" });
  });

  it("sends on with session-start requests once turned on", () => {
    writeBrowserAgentToolsEnabled(true);
    expect(readBrowserAgentToolsEnabled()).toBe(true);
    expect(backgroundSessionTitlesRequestHeaders()).toEqual({ [BROWSER_AGENT_TOOLS_HEADER]: "on" });
    writeBrowserAgentToolsEnabled(false);
    expect(browserAgentToolsRequestHeaders()).toEqual({ [BROWSER_AGENT_TOOLS_HEADER]: "trusted" });
  });

  it("sends nothing from a desktop app without the tools, or a plain browser", () => {
    writeBrowserAgentToolsEnabled(true);
    Object.assign(window, { omnigentDesktop: { browserPerformAction: vi.fn() } });
    expect(browserAgentToolsRequestHeaders()).toEqual({});
    delete (window as { omnigentDesktop?: unknown }).omnigentDesktop;
    expect(backgroundSessionTitlesRequestHeaders()).toEqual({});
  });
});

describe("forks", () => {
  it("send this app's switch, as a new session does, for clones and side chats", async () => {
    const fetchMock = vi.fn(
      async (url: string, _init?: RequestInit) =>
        new Response(
          JSON.stringify(
            url.endsWith("/fork")
              ? { id: "conv_fork", agent_id: "a", status: "idle", created_at: 1 }
              : {
                  id: "conv_src",
                  agent_id: "a",
                  status: "idle",
                  created_at: 1,
                  runner_online: true,
                },
          ),
          { status: url.endsWith("/fork") ? 201 : 200 },
        ),
    );
    vi.stubGlobal("fetch", fetchMock);
    try {
      const forkHeaders = () =>
        fetchMock.mock.calls
          .filter(([url]) => String(url).endsWith("/fork"))
          .map(([, init]) =>
            new Headers((init as RequestInit).headers).get(BROWSER_AGENT_TOOLS_HEADER),
          );
      writeBrowserAgentToolsEnabled(true);
      await forkSession("conv_src");
      await createSideChat("conv_src");
      writeBrowserAgentToolsEnabled(false);
      await forkSession("conv_src");
      expect(forkHeaders()).toEqual(["on", "on", "trusted"]);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});

describe("browserAgentToolsOffError", () => {
  it("refuses the advanced actions while off", () => {
    for (const action of ["go_back", "tabs", "hover", "drag", "scroll", "press_key", "check"]) {
      expect(browserAgentToolsOffError(action, {})).toContain(`browser_${action} is turned off`);
    }
    expect(browserAgentToolsOffError("click", { selector: "a", button: "right" })).toContain(
      "browser_click with button or double_click is turned off",
    );
    expect(
      browserAgentToolsOffError("click", { selector: "a", double_click: true }),
    ).not.toBeNull();
  });

  it("always allows the original five tools", () => {
    for (const action of ["navigate", "snapshot", "type", "screenshot"]) {
      expect(browserAgentToolsOffError(action, {})).toBeNull();
    }
    expect(browserAgentToolsOffError("click", { selector: "a", button: "left" })).toBeNull();
  });

  it("allows everything while on", () => {
    writeBrowserAgentToolsEnabled(true);
    expect(browserAgentToolsOffError("hover", {})).toBeNull();
    expect(browserAgentToolsOffError("click", { button: "right" })).toBeNull();
  });
});

describe("BrowserAgentToolsSettingsGroup", () => {
  it("toggles the stored preference", () => {
    render(<BrowserAgentToolsSettingsGroup />);
    const toggle = screen.getByRole("switch", { name: "Advanced browser tools for agents" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(readBrowserAgentToolsEnabled()).toBe(true);
  });

  it("renders nothing outside the desktop app", () => {
    supportsBrowser.mockReturnValue(false);
    const { container } = render(<BrowserAgentToolsSettingsGroup />);
    expect(container).toBeEmptyDOMElement();
  });

  it("can't be turned on in a desktop app without the tools, and says to update it", () => {
    writeBrowserAgentToolsEnabled(true);
    Object.assign(window, { omnigentDesktop: { browserPerformAction: vi.fn() } });
    render(<BrowserAgentToolsSettingsGroup />);
    const toggle = screen.getByRole("switch", { name: "Advanced browser tools for agents" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(toggle).toBeDisabled();
    expect(screen.getByText(UPDATE_DESKTOP_NOTE)).toBeInTheDocument();
  });

  it("gives the tabs back to each window when turned off", () => {
    const browserTabs = vi.fn(async () => ({ ok: true }));
    Object.assign(window, { omnigentDesktop: { browserTabs } });
    writeBrowserAgentToolsEnabled(true);
    render(<BrowserAgentToolsSettingsGroup />);
    fireEvent.click(screen.getByRole("switch", { name: "Advanced browser tools for agents" }));
    expect(browserTabs).toHaveBeenCalledWith({ op: "reset" });
  });
});
