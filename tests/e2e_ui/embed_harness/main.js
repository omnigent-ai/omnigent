// Minimal stand-in for the Databricks monolith's embed mount — the "managed
// omnigent" path. Renders the SCOPED embed island (`web/dist-embed`, built by
// `vite.embed.config.ts`) inside a host-owned React tree + router, exactly as
// `web/src/embed.tsx` documents for the same-root path. The host drives dark
// mode via `isDarkMode` (next-themes `forcedTheme`); the embed has no theme
// switcher of its own. No `fetcher` is injected: the harness page is served
// same-origin by the test server, so the embed's default relative `/v1/...`
// transport and window.location-derived WebSocket URLs work unchanged.
import { createElement } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { OmnigentApp } from "../dist-embed/omnigent-embed.js";
import "../dist-embed/omnigent-embed.css";

const dark = new URLSearchParams(window.location.search).get("dark") !== "0";

createRoot(document.getElementById("root")).render(
  createElement(
    BrowserRouter,
    null,
    createElement(OmnigentApp, { basename: "/embed-host", isDarkMode: dark }),
  ),
);
