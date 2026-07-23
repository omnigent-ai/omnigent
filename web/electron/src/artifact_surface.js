"use strict";

const { randomUUID } = require("node:crypto");

const ARTIFACT_INSPECTOR_SCRIPT = `(() => {
  window.__omnigentCancelArtifactInspector?.();
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.setAttribute("data-omnigent-artifact-inspector", "");
    Object.assign(overlay.style, {
      position: "fixed",
      zIndex: "2147483647",
      pointerEvents: "none",
      border: "2px solid #2563eb",
      background: "rgba(37, 99, 235, 0.12)",
      boxSizing: "border-box",
      display: "none",
    });
    document.documentElement.appendChild(overlay);

    const cleanup = (result) => {
      document.removeEventListener("mousemove", onMove, true);
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKeyDown, true);
      overlay.remove();
      delete window.__omnigentCancelArtifactInspector;
      resolve(result);
    };
    const onMove = (event) => {
      const target = document.elementFromPoint(event.clientX, event.clientY);
      if (!target || target === overlay) return;
      const rect = target.getBoundingClientRect();
      Object.assign(overlay.style, {
        display: "block",
        left: rect.left + "px",
        top: rect.top + "px",
        width: rect.width + "px",
        height: rect.height + "px",
      });
    };
    const onClick = (event) => {
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      cleanup({ x: Math.round(event.clientX), y: Math.round(event.clientY) });
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") cleanup(null);
    };

    window.__omnigentCancelArtifactInspector = () => cleanup(null);
    document.addEventListener("mousemove", onMove, true);
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKeyDown, true);
  });
})()`;

function parseArtifactPreviewUrl(value) {
  const url = new URL(value);
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    throw new Error("artifact previews require an HTTP(S) URL");
  }
  const match = url.pathname.match(/^\/p\/([^/]+)\//);
  if (!match) throw new Error("artifact previews require a capability-scoped URL");
  return {
    url,
    origin: url.origin,
    capabilityPrefix: `/p/${match[1]}/`,
  };
}

function navigationAllowed(value, policy) {
  try {
    const url = new URL(value);
    return url.origin === policy.origin && url.pathname.startsWith(policy.capabilityPrefix);
  } catch {
    return false;
  }
}

function normalizeArtifactBounds(bounds, contentBounds) {
  const contentWidth = Math.max(0, Math.round(Number(contentBounds?.width) || 0));
  const contentHeight = Math.max(0, Math.round(Number(contentBounds?.height) || 0));
  const x = Math.min(contentWidth, Math.max(0, Math.round(Number(bounds?.x) || 0)));
  const y = Math.min(contentHeight, Math.max(0, Math.round(Number(bounds?.y) || 0)));
  const requestedWidth = Math.max(0, Math.round(Number(bounds?.width) || 0));
  const requestedHeight = Math.max(0, Math.round(Number(bounds?.height) || 0));
  return {
    x,
    y,
    width: Math.min(requestedWidth, contentWidth - x),
    height: Math.min(requestedHeight, contentHeight - y),
  };
}

function denyPermissions(ses) {
  if (!ses) return;
  ses.setPermissionCheckHandler?.(() => false);
  ses.setPermissionRequestHandler?.((_webContents, _permission, callback) => callback(false));
}

class ArtifactSurfaceManager {
  constructor({ createView, configureSession = denyPermissions }) {
    this.createView = createView;
    this.configureSession = configureSession;
    this.surfaces = new WeakMap();
  }

  async sync(win, params) {
    if (!params || typeof params.id !== "string" || params.id.length === 0) {
      throw new Error("artifact surface id is required");
    }
    const policy = parseArtifactPreviewUrl(params.url);
    let surface = this.surfaces.get(win);
    const policyChanged =
      surface &&
      (surface.policy.origin !== policy.origin ||
        surface.policy.capabilityPrefix !== policy.capabilityPrefix);
    if (surface && (surface.id !== params.id || policyChanged)) {
      this.destroy(win, surface.id);
      surface = undefined;
    }

    if (!surface) {
      const view = this.createView({
        webPreferences: {
          contextIsolation: true,
          nodeIntegration: false,
          sandbox: true,
          webSecurity: true,
          partition: `omnigent-artifact-preview-${win.id}-${randomUUID()}`,
        },
      });
      this.configureSession(view.webContents.session);
      view.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      const blockUnsafeNavigation = (event, url) => {
        if (!navigationAllowed(url, policy)) event.preventDefault();
      };
      view.webContents.on("will-navigate", blockUnsafeNavigation);
      view.webContents.on("will-redirect", blockUnsafeNavigation);
      win.contentView.addChildView(view);
      surface = { id: params.id, view, policy, url: null };
      this.surfaces.set(win, surface);
    }

    const bounds = normalizeArtifactBounds(params.bounds, win.getContentBounds());
    surface.view.setBounds(bounds);
    surface.view.setVisible(Boolean(params.visible && bounds.width > 0 && bounds.height > 0));
    if (surface.url !== policy.url.href) {
      surface.url = policy.url.href;
      await surface.view.webContents.loadURL(surface.url);
    }
    return true;
  }

  destroy(win, id) {
    const surface = this.surfaces.get(win);
    if (!surface || surface.id !== id) return;
    this.surfaces.delete(win);
    win.contentView.removeChildView(surface.view);
    if (!surface.view.webContents.isDestroyed?.()) surface.view.webContents.close();
  }

  async inspect(win, id) {
    const surface = this.surfaces.get(win);
    if (!surface || surface.id !== id) return false;
    const point = await surface.view.webContents.executeJavaScript(ARTIFACT_INSPECTOR_SCRIPT, true);
    if (!point) return false;
    surface.view.webContents.inspectElement(point.x, point.y);
    return true;
  }

  destroyWindow(win) {
    const surface = this.surfaces.get(win);
    if (surface) this.destroy(win, surface.id);
  }
}

module.exports = {
  ArtifactSurfaceManager,
  ARTIFACT_INSPECTOR_SCRIPT,
  denyPermissions,
  navigationAllowed,
  normalizeArtifactBounds,
  parseArtifactPreviewUrl,
};
