"use strict";

const { joinServerUrl, workspaceIdentityKey } = require("./url");

const MODAL_WEBAUTHN_TIMEOUT_MS = 5 * 60 * 1000;

// Arm only during fallback auth, plus the accounts-mode `/login` page.
function isWebAuthnEscapePage(pageUrl, serverUrl, authenticationNavigation = false) {
  if (!serverUrl) return false;
  try {
    const page = new URL(pageUrl);
    const accountsLoginUrl = new URL(joinServerUrl(serverUrl, "/login"));
    const pageIdentity = workspaceIdentityKey(pageUrl);
    const serverIdentity = workspaceIdentityKey(serverUrl);
    const isWebPage = page.protocol === "http:" || page.protocol === "https:";
    const accountsLogin =
      pageIdentity === serverIdentity && page.pathname === accountsLoginUrl.pathname;
    return (
      isWebPage && (accountsLogin || (authenticationNavigation && pageIdentity !== serverIdentity))
    );
  } catch {
    return false;
  }
}

// Surface an escape without ending or replacing the credential request.
function webAuthnTimeoutScript(timeoutMs) {
  const delay = Math.max(1, Number(timeoutMs) || MODAL_WEBAUTHN_TIMEOUT_MS);
  return `(() => {
    const credentials = navigator.credentials;
    if (!credentials || typeof credentials.get !== "function") return Promise.resolve(null);
    const originalGet = credentials.get.bind(credentials);
    let reportTimeout;
    const timeoutReport = new Promise((resolve) => { reportTimeout = resolve; });
    try {
      Object.defineProperty(credentials, "get", {
        configurable: true,
        writable: true,
        value: function (options) {
          const allowCredentials = options && options.publicKey
            ? options.publicKey.allowCredentials
            : null;
          const hasAllowList = Array.isArray(allowCredentials) && allowCredentials.length > 0;
          const isModalPublicKey = Boolean(
            options && options.publicKey && options.mediation !== "conditional" && !hasAllowList
          );
          if (!isModalPublicKey) return originalGet(options);
          const request = originalGet(options);
          const timer = setTimeout(() => reportTimeout({ timedOut: true }), ${delay});
          Promise.resolve(request).then(
            () => clearTimeout(timer),
            () => clearTimeout(timer)
          );
          return request;
        },
      });
    } catch {
      return Promise.resolve(null);
    }
    return timeoutReport;
  })()`;
}

function registerWebAuthnTimeout(
  webContents,
  { timeoutMs = MODAL_WEBAUTHN_TIMEOUT_MS, shouldInject = () => true, onTimeout },
) {
  webContents.on("did-finish-load", () => {
    if (!shouldInject()) return;
    return webContents
      .executeJavaScript(webAuthnTimeoutScript(timeoutMs), true)
      .then((result) => {
        if (result?.timedOut !== true) return;
        return Promise.resolve()
          .then(onTimeout)
          .catch((error) => {
            console.error("[omnigent] WebAuthn timeout handling failed", error);
          });
      })
      .catch(() => {
        // Navigation/destroyed context, or a page where injection is unavailable.
      });
  });
}

module.exports = {
  MODAL_WEBAUTHN_TIMEOUT_MS,
  isWebAuthnEscapePage,
  webAuthnTimeoutScript,
  registerWebAuthnTimeout,
};
