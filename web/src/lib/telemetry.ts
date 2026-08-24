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
// is a no-op — and because the SDK sits behind a dynamic import in
// `telemetryProvider.ts`, an unconfigured build never even downloads it.

let initialized = false;

/**
 * Initialize browser tracing if a collector endpoint is configured.
 *
 * Idempotent and safe to call once at app startup. No-op when
 * `VITE_OTEL_EXPORTER_OTLP_ENDPOINT` is unset — the endpoint check is
 * synchronous, so an unconfigured build resolves without loading the SDK.
 *
 * @returns Resolves once fetch/XHR are patched. Await it before the first
 *   request so that request is traced too.
 */
export async function initBrowserTelemetry(): Promise<void> {
  if (initialized) return;

  const endpoint = import.meta.env.VITE_OTEL_EXPORTER_OTLP_ENDPOINT?.trim();
  if (!endpoint) return;
  initialized = true;

  const serviceName = import.meta.env.VITE_OTEL_SERVICE_NAME?.trim() || "omni-web";

  const { startTracing } = await import("./telemetryProvider");
  startTracing(endpoint, serviceName);
}
