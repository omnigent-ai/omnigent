/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL of an OTLP/HTTP collector for browser tracing, e.g.
   * `http://localhost:4318`. When unset, browser tracing is disabled.
   */
  readonly VITE_OTEL_EXPORTER_OTLP_ENDPOINT?: string;
  /** Service name reported for browser spans. Defaults to `omni-web`. */
  readonly VITE_OTEL_SERVICE_NAME?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
