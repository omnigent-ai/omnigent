// Browser-side OpenTelemetry tracing for the Omnigent web UI.
//
// Initializes a WebTracerProvider with fetch + XHR instrumentation so a
// trace BEGINS in the browser (at the user's click) and continues into the
// server: the instrumentation injects a W3C `traceparent` header on every
// request, which the FastAPI-instrumented server extracts. The browser
// exports spans over OTLP/HTTP to a collector.
//
// Opt-in by configuration, mirroring the server: tracing only activates when
// `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` is set (the base URL of an OTLP/HTTP
// collector, e.g. `http://localhost:4318`). With no endpoint configured this
// is a no-op — no provider, no patched fetch, zero overhead — so production
// builds without a collector are unaffected.

import { ZoneContextManager } from "@opentelemetry/context-zone";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-http";
import { registerInstrumentations } from "@opentelemetry/instrumentation";
import { FetchInstrumentation } from "@opentelemetry/instrumentation-fetch";
import { XMLHttpRequestInstrumentation } from "@opentelemetry/instrumentation-xml-http-request";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { BatchSpanProcessor, WebTracerProvider } from "@opentelemetry/sdk-trace-web";
import { ATTR_SERVICE_NAME } from "@opentelemetry/semantic-conventions";

let initialized = false;

/**
 * Initialize browser tracing if a collector endpoint is configured.
 *
 * Idempotent and safe to call once at app startup (before any request is
 * made, so fetch/XHR are patched in time). No-op when
 * `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` is unset.
 */
export function initBrowserTelemetry(): void {
  if (initialized) return;

  const endpoint = import.meta.env.VITE_OTEL_EXPORTER_OTLP_ENDPOINT?.trim();
  if (!endpoint) return;
  initialized = true;

  const serviceName = import.meta.env.VITE_OTEL_SERVICE_NAME?.trim() || "omni-web";

  const exporter = new OTLPTraceExporter({
    // OTLP/HTTP traces are posted to the collector's `/v1/traces` path.
    url: `${endpoint.replace(/\/$/, "")}/v1/traces`,
  });

  const provider = new WebTracerProvider({
    resource: resourceFromAttributes({ [ATTR_SERVICE_NAME]: serviceName }),
    spanProcessors: [new BatchSpanProcessor(exporter)],
  });

  // ZoneContextManager keeps the active span across async boundaries
  // (promises, timers) so child spans nest correctly in the browser.
  provider.register({ contextManager: new ZoneContextManager() });

  // Propagate `traceparent` to same-origin API calls (the default) and,
  // explicitly, to this app's own origin — scoped so the header is never
  // attached to unrelated third-party requests (analytics, CDNs).
  const propagateTraceHeaderCorsUrls = [new RegExp(`^${escapeRegExp(window.location.origin)}`)];

  registerInstrumentations({
    instrumentations: [
      new FetchInstrumentation({
        propagateTraceHeaderCorsUrls,
        clearTimingResources: true,
      }),
      new XMLHttpRequestInstrumentation({ propagateTraceHeaderCorsUrls }),
    ],
  });
}

/**
 * Escape a string for safe use inside a RegExp.
 *
 * @param value Raw string, e.g. an origin like `https://app.example.com`.
 * @returns The string with regex metacharacters escaped.
 */
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
