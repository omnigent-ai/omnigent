"use strict";

// Electron draws no WebAuthn UI, so the shell shows its own notice while an
// embedded browser page waits for a security key.
const { contextBridge, ipcRenderer } = require("electron");

function observeWebAuthnRequests(report) {
  const prototype = globalThis.CredentialsContainer?.prototype;
  if (!prototype) return;
  const apply = Reflect.apply;
  const then = Promise.prototype.then;
  const describe = Object.getOwnPropertyDescriptor;
  const prototypeOf = Object.getPrototypeOf;
  const hasOwn = Object.hasOwn;
  let pending = 0;
  const safeReport = (value) => {
    try {
      report(value);
    } catch {
      // Keep the observer's own errors, e.g. during unload, out of the page.
    }
  };
  const settle = () => {
    pending -= 1;
    if (pending === 0) safeReport(false);
  };
  for (const name of ["get", "create"]) {
    const descriptor = Object.getOwnPropertyDescriptor(prototype, name);
    if (typeof descriptor?.value !== "function") continue;
    // A Proxy keeps name and length; toString stays native but drops the name.
    descriptor.value = new Proxy(descriptor.value, {
      apply(target, thisArg, args) {
        let prompted = false;
        try {
          // Dictionary members may be inherited. Page getters must run only in the native
          // call, so accessor options go unobserved.
          const member = (key) => {
            for (let source = args[0]; source != null; source = prototypeOf(source)) {
              const property = describe(source, key);
              if (!property) continue;
              if (!hasOwn(property, "value")) throw new TypeError("accessor option");
              return property.value;
            }
            return undefined;
          };
          const mediation = member("mediation");
          // Autofill (conditional), silent and immediate mediation show no browser prompt.
          prompted =
            Boolean(member("publicKey")) &&
            (mediation === undefined || mediation === "optional" || mediation === "required");
        } catch {
          // Unreadable options are left to the native call.
        }
        const result = apply(target, thisArg, args);
        if (prompted) {
          try {
            // `then` consults the page-controlled Promise species, so it may throw. Observing
            // the promise also marks a rejection handled, which only affects unhandledrejection.
            apply(then, result, [settle, settle]);
            pending += 1;
            safeReport(true);
          } catch {
            // An unobservable request must not leave the notice up.
          }
        }
        return result;
      },
    });
    Object.defineProperty(prototype, name, descriptor);
  }
}

contextBridge.executeInMainWorld({
  func: observeWebAuthnRequests,
  args: [(pending) => ipcRenderer.send("omnigent:browser-webauthn-pending", pending === true)],
});
