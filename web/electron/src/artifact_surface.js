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

const ARTIFACT_SELECTION_SCRIPT = `(() => {
  window.__omnigentCancelArtifactSelection?.();
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.setAttribute("data-omnigent-artifact-selection", "");
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

    const selectorFor = (element) => {
      if (element.id) return "#" + CSS.escape(element.id);
      const parts = [];
      let current = element;
      while (current && current.nodeType === Node.ELEMENT_NODE && current !== document.documentElement) {
        let part = current.tagName.toLowerCase();
        const classes = Array.from(current.classList).filter(Boolean).slice(0, 2);
        if (classes.length > 0) part += classes.map((name) => "." + CSS.escape(name)).join("");
        const parent = current.parentElement;
        if (parent) {
          const peers = Array.from(parent.children).filter((node) => node.tagName === current.tagName);
          if (peers.length > 1) part += ":nth-of-type(" + (peers.indexOf(current) + 1) + ")";
        }
        parts.unshift(part);
        if (document.querySelectorAll(parts.join(" > ")).length === 1) break;
        current = parent;
      }
      return parts.join(" > ");
    };
    const accessibleName = (element) => {
      const labelledBy = element.getAttribute("aria-labelledby");
      if (labelledBy) {
        const value = labelledBy
          .split(/\\s+/)
          .map((id) => document.getElementById(id)?.textContent?.trim() || "")
          .filter(Boolean)
          .join(" ");
        if (value) return value;
      }
      return (
        element.getAttribute("aria-label") ||
        element.getAttribute("alt") ||
        element.getAttribute("title") ||
        element.textContent?.trim() ||
        ""
      ).slice(0, 500);
    };
    const cleanup = (result) => {
      document.removeEventListener("mousemove", onMove, true);
      document.removeEventListener("click", onClick, true);
      document.removeEventListener("keydown", onKeyDown, true);
      overlay.remove();
      delete window.__omnigentCancelArtifactSelection;
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
      const target = document.elementFromPoint(event.clientX, event.clientY);
      if (!target || target === overlay) return cleanup(null);
      const rect = target.getBoundingClientRect();
      const style = getComputedStyle(target);
      const styleNames = [
        "display", "position", "width", "height", "color", "backgroundColor",
        "fontFamily", "fontSize", "fontWeight", "lineHeight", "letterSpacing",
        "margin", "padding", "gap", "border", "borderRadius", "boxShadow",
        "flexDirection", "alignItems", "justifyContent", "gridTemplateColumns",
      ];
      const styles = Object.fromEntries(styleNames.map((name) => [name, style[name]]));
      cleanup({
        selector: selectorFor(target),
        tagName: target.tagName.toLowerCase(),
        role: target.getAttribute("role") || null,
        accessibleName: accessibleName(target) || null,
        text: (target.textContent?.trim() || "").slice(0, 1000),
        html: target.outerHTML.slice(0, 4000),
        rect: {
          x: Math.max(0, Math.round(rect.x)),
          y: Math.max(0, Math.round(rect.y)),
          width: Math.max(1, Math.round(rect.width)),
          height: Math.max(1, Math.round(rect.height)),
        },
        viewport: {
          width: window.innerWidth,
          height: window.innerHeight,
          devicePixelRatio: window.devicePixelRatio,
        },
        styles,
      });
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") cleanup(null);
    };

    window.__omnigentCancelArtifactSelection = () => cleanup(null);
    document.addEventListener("mousemove", onMove, true);
    document.addEventListener("click", onClick, true);
    document.addEventListener("keydown", onKeyDown, true);
  });
})()`;

const ARTIFACT_REVIEW_SCRIPT = `(() => {
  const issues = [];
  const selectorFor = (element) => {
    if (element.id) return "#" + CSS.escape(element.id);
    const parent = element.parentElement;
    const tag = element.tagName.toLowerCase();
    if (!parent) return tag;
    const peers = Array.from(parent.children).filter((node) => node.tagName === element.tagName);
    return selectorFor(parent) + " > " + tag + (peers.length > 1 ? ":nth-of-type(" + (peers.indexOf(element) + 1) + ")" : "");
  };
  const nameFor = (element) =>
    element.getAttribute("aria-label") ||
    element.getAttribute("alt") ||
    element.getAttribute("title") ||
    element.textContent?.trim() ||
    "";
  const add = (severity, code, message, element) => {
    if (issues.length >= 100) return;
    issues.push({ severity, code, message, selector: element ? selectorFor(element) : null });
  };

  if (!document.title.trim()) add("warning", "missing-title", "Document has no title", null);
  if (!document.querySelector("main, [role='main']")) add("warning", "missing-main", "Document has no main landmark", null);
  document.querySelectorAll("button, a[href], [role='button'], [role='link']").forEach((element) => {
    if (!nameFor(element)) add("error", "missing-name", "Interactive element has no accessible name", element);
  });
  document.querySelectorAll("input, select, textarea").forEach((element) => {
    const id = element.id;
    const labelled = element.getAttribute("aria-label") || element.getAttribute("aria-labelledby") || (id && document.querySelector("label[for='" + CSS.escape(id) + "']"));
    if (!labelled) add("error", "missing-label", "Form control has no accessible label", element);
  });
  document.querySelectorAll("img").forEach((element) => {
    if (!element.hasAttribute("alt")) add("error", "missing-alt", "Image is missing alt text", element);
    if (element.complete && element.naturalWidth === 0) add("error", "broken-image", "Image failed to load", element);
  });
  document.querySelectorAll("link[rel='stylesheet']").forEach((element) => {
    if (!element.sheet) add("error", "broken-stylesheet", "Stylesheet failed to load", element);
  });
  const headings = Array.from(document.querySelectorAll("h1,h2,h3,h4,h5,h6"));
  let previousLevel = 0;
  for (const heading of headings) {
    const level = Number(heading.tagName.slice(1));
    if (previousLevel > 0 && level > previousLevel + 1) add("warning", "heading-jump", "Heading levels skip from h" + previousLevel + " to h" + level, heading);
    previousLevel = level;
  }
  if (document.documentElement.scrollWidth > window.innerWidth + 1) {
    add("warning", "horizontal-overflow", "Document overflows the viewport horizontally", document.documentElement);
  }

  return {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    issues,
  };
})()`;

function pushCapped(items, value, limit = 50) {
  items.push(value);
  if (items.length > limit) items.splice(0, items.length - limit);
}

function consoleLevelName(level) {
  if (typeof level === "string") return level;
  return ["debug", "info", "warning", "error"][level] || "info";
}

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
    if (surface) {
      const policyChanged =
        surface.policy.origin !== policy.origin ||
        surface.policy.capabilityPrefix !== policy.capabilityPrefix;
      surface.id = params.id;
      surface.policy = policy;
      if (policyChanged) {
        surface.consoleMessages.length = 0;
        surface.loadErrors.length = 0;
      }
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
      const consoleMessages = [];
      const loadErrors = [];
      surface = { id: params.id, view, policy, url: null, consoleMessages, loadErrors };
      this.configureSession(view.webContents.session);
      view.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
      const blockUnsafeNavigation = (event, url) => {
        if (!navigationAllowed(url, surface.policy)) event.preventDefault();
      };
      view.webContents.on("will-navigate", blockUnsafeNavigation);
      view.webContents.on("will-redirect", blockUnsafeNavigation);
      view.webContents.on("console-message", (_event, levelOrDetails, message, line, source) => {
        const details =
          levelOrDetails && typeof levelOrDetails === "object"
            ? levelOrDetails
            : { level: levelOrDetails, message, lineNumber: line, sourceId: source };
        pushCapped(consoleMessages, {
          level: consoleLevelName(details.level),
          message: String(details.message || ""),
          line: Number(details.lineNumber || 0),
          source: String(details.sourceId || ""),
        });
      });
      view.webContents.on("did-fail-load", (_event, code, description, url, mainFrame) => {
        pushCapped(loadErrors, {
          code: Number(code),
          description: String(description || ""),
          url: String(url || ""),
          mainFrame: Boolean(mainFrame),
        });
      });
      win.contentView.addChildView(view);
      this.surfaces.set(win, surface);
    }

    const bounds = normalizeArtifactBounds(params.bounds, win.getContentBounds());
    surface.view.setBounds(bounds);
    const viewportWidth = Number(params.viewportWidth);
    if (
      Number.isFinite(viewportWidth) &&
      viewportWidth > 0 &&
      bounds.width > 0 &&
      bounds.height > 0
    ) {
      const width = Math.round(viewportWidth);
      const scale = Math.min(1, bounds.width / width);
      const height = Math.max(1, Math.round(bounds.height / scale));
      surface.view.webContents.enableDeviceEmulation({
        screenPosition: "desktop",
        screenSize: { width, height },
        viewPosition: { x: 0, y: 0 },
        deviceScaleFactor: 1,
        viewSize: { width, height },
        scale,
      });
    } else {
      surface.view.webContents.disableDeviceEmulation();
    }
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

  async select(win, id) {
    const surface = this.surfaces.get(win);
    if (!surface || surface.id !== id) return null;
    const selection = await surface.view.webContents.executeJavaScript(
      ARTIFACT_SELECTION_SCRIPT,
      true,
    );
    if (!selection?.rect) return null;
    const screenshot = await surface.view.webContents.capturePage(selection.rect);
    return {
      ...selection,
      screenshotDataUrl: screenshot.toDataURL(),
    };
  }

  reload(win, id) {
    const surface = this.surfaces.get(win);
    if (!surface || surface.id !== id) return false;
    surface.view.webContents.reload();
    return true;
  }

  async review(win, id) {
    const surface = this.surfaces.get(win);
    if (!surface || surface.id !== id) return null;
    const result = await surface.view.webContents.executeJavaScript(ARTIFACT_REVIEW_SCRIPT, true);
    return {
      ...result,
      consoleMessages: [...surface.consoleMessages],
      loadErrors: [...surface.loadErrors],
    };
  }

  destroyWindow(win) {
    const surface = this.surfaces.get(win);
    if (surface) this.destroy(win, surface.id);
  }
}

module.exports = {
  ArtifactSurfaceManager,
  ARTIFACT_INSPECTOR_SCRIPT,
  ARTIFACT_SELECTION_SCRIPT,
  ARTIFACT_REVIEW_SCRIPT,
  denyPermissions,
  navigationAllowed,
  normalizeArtifactBounds,
  parseArtifactPreviewUrl,
};
