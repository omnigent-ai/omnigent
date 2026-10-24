"""Controlled Electron host bridges for browser recovery journeys; no native process starts."""

THIS_MACHINE_HOST_ID = "host_this_machine"

LOCAL_DESKTOP_BRIDGE_INIT_SCRIPT = f"""
window.__controlHostCalls = [];
window.__hostStatusListeners = [];
window.omnigentDesktop = {{
  kind: "electron",
  setBadgeCount: function () {{}},
  notify: function () {{ return Promise.resolve(false); }},
  onNotificationActivated: function () {{ return function () {{}}; }},
  getServerPicker: function () {{ return Promise.resolve(null); }},
  switchServer: function () {{ return Promise.resolve(); }},
  openServerSetup: function () {{}},
  getHostIdentity: function () {{
    return Promise.resolve({{ cliInstalled: true, hostId: {THIS_MACHINE_HOST_ID!r} }});
  }},
  onHostStatusChanged: function (callback) {{
    window.__hostStatusListeners.push(callback);
    return () => {{
      window.__hostStatusListeners = window.__hostStatusListeners.filter(cb => cb !== callback);
    }};
  }},
  controlHost: function (action) {{
    window.__controlHostCalls.push(action);
    return new Promise(resolve => {{
      window.__finishReconnect = result => {{
        resolve(result);
        window.__hostStatusListeners.forEach(callback => callback());
      }};
    }});
  }},
  getDesktopFeatures: function () {{ return Promise.resolve(null); }},
}};
"""

ARCA_DESKTOP_BRIDGE_INIT_SCRIPT = """
window.__connectArcaCalls = 0;
window.__hostStatusListeners = [];
window.omnigentDesktop = {
  kind: "electron",
  setBadgeCount: function () {},
  notify: function () { return Promise.resolve(false); },
  onNotificationActivated: function () { return function () {}; },
  getServerPicker: function () { return Promise.resolve(null); },
  switchServer: function () { return Promise.resolve(); },
  openServerSetup: function () {},
  getHostIdentity: function () {
    return Promise.resolve({ cliInstalled: true, hostId: "host_this_machine" });
  },
  onHostStatusChanged: function (callback) {
    window.__hostStatusListeners.push(callback);
    return () => {
      window.__hostStatusListeners = window.__hostStatusListeners.filter(cb => cb !== callback);
    };
  },
  connectArcaHost: function () {
    window.__connectArcaCalls += 1;
    return new Promise(resolve => {
      window.__finishArcaReconnect = result => {
        resolve(result);
        window.__hostStatusListeners.forEach(callback => callback());
      };
    });
  },
  getDesktopFeatures: function () { return Promise.resolve(null); },
};
"""
