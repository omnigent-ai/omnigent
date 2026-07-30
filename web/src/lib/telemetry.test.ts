import { afterEach, describe, expect, it, vi } from "vitest";

// Mock the OpenTelemetry SDK so the test is deterministic and free of
// zone.js / network side effects. `vi.hoisted` builds the spies before the
// hoisted `vi.mock` factories run.
const m = vi.hoisted(() => {
  const register = vi.fn();
  return {
    register,
    registerInstrumentations: vi.fn(),
    // Constructor-style mock (normal function, not arrow) so `new
    // WebTracerProvider(...)` works and exposes `.register`.
    WebTracerProvider: vi.fn(function (this: { register: typeof register }) {
      this.register = register;
    }),
    BatchSpanProcessor: vi.fn(),
    FetchInstrumentation: vi.fn(),
    XMLHttpRequestInstrumentation: vi.fn(),
    OTLPTraceExporter: vi.fn(),
    ZoneContextManager: vi.fn(),
    resourceFromAttributes: vi.fn(() => ({})),
  };
});

vi.mock("@opentelemetry/instrumentation", () => ({
  registerInstrumentations: m.registerInstrumentations,
}));
vi.mock("@opentelemetry/sdk-trace-web", () => ({
  WebTracerProvider: m.WebTracerProvider,
  BatchSpanProcessor: m.BatchSpanProcessor,
}));
vi.mock("@opentelemetry/instrumentation-fetch", () => ({
  FetchInstrumentation: m.FetchInstrumentation,
}));
vi.mock("@opentelemetry/instrumentation-xml-http-request", () => ({
  XMLHttpRequestInstrumentation: m.XMLHttpRequestInstrumentation,
}));
vi.mock("@opentelemetry/exporter-trace-otlp-http", () => ({
  OTLPTraceExporter: m.OTLPTraceExporter,
}));
vi.mock("@opentelemetry/context-zone", () => ({
  ZoneContextManager: m.ZoneContextManager,
}));
vi.mock("@opentelemetry/resources", () => ({
  resourceFromAttributes: m.resourceFromAttributes,
}));
vi.mock("@opentelemetry/semantic-conventions", () => ({
  ATTR_SERVICE_NAME: "service.name",
}));

afterEach(() => {
  vi.unstubAllEnvs();
  vi.clearAllMocks();
  vi.resetModules();
});

describe("initBrowserTelemetry", () => {
  it("no-ops when no collector endpoint is configured", async () => {
    vi.stubEnv("VITE_OTEL_EXPORTER_OTLP_ENDPOINT", "");
    const { initBrowserTelemetry } = await import("./telemetry");

    initBrowserTelemetry();

    // With no collector, nothing is registered — production builds without
    // a backend pay zero overhead and never patch fetch.
    expect(m.WebTracerProvider).not.toHaveBeenCalled();
    expect(m.registerInstrumentations).not.toHaveBeenCalled();
  });

  it("registers a provider + fetch/XHR instrumentation when an endpoint is set", async () => {
    vi.stubEnv("VITE_OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318");
    const { initBrowserTelemetry } = await import("./telemetry");

    initBrowserTelemetry();

    expect(m.WebTracerProvider).toHaveBeenCalledTimes(1);
    expect(m.register).toHaveBeenCalledTimes(1);
    // Traces post to the collector's /v1/traces path.
    expect(m.OTLPTraceExporter).toHaveBeenCalledWith({
      url: "http://localhost:4318/v1/traces",
    });
    expect(m.FetchInstrumentation).toHaveBeenCalledTimes(1);
    expect(m.XMLHttpRequestInstrumentation).toHaveBeenCalledTimes(1);
    expect(m.registerInstrumentations).toHaveBeenCalledTimes(1);
  });

  it("is idempotent — a second call does not re-register", async () => {
    vi.stubEnv("VITE_OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318");
    const { initBrowserTelemetry } = await import("./telemetry");

    initBrowserTelemetry();
    initBrowserTelemetry();

    expect(m.WebTracerProvider).toHaveBeenCalledTimes(1);
  });

  it("strips a trailing slash from the endpoint before appending /v1/traces", async () => {
    vi.stubEnv("VITE_OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318/");
    const { initBrowserTelemetry } = await import("./telemetry");

    initBrowserTelemetry();

    expect(m.OTLPTraceExporter).toHaveBeenCalledWith({
      url: "http://localhost:4318/v1/traces",
    });
  });
});
