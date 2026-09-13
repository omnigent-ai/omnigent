/// <reference types="vite/client" />

interface ImportMetaEnv {
  /**
   * Base URL of an OTLP/HTTP collector for browser tracing, e.g.
   * `http://localhost:4318`. When unset, browser tracing is disabled.
   */
  readonly VITE_OTEL_EXPORTER_OTLP_ENDPOINT?: string;
  /** Service name reported for browser spans. Defaults to `omni-web`. */
  readonly VITE_OTEL_SERVICE_NAME?: string;
  /**
   * `"true"` when the dev proxy targets a Databricks workspace-hosted server
   * (behind the multi-replica sharding layer), set by `vite.config.ts` from
   * `OMNIGENT_URL`. Signals the standalone dev bundle to emit the host_id slice
   * key on host-scoped traffic; see `isDatabricksWorkspace` in `host.ts`.
   */
  readonly VITE_DATABRICKS_WORKSPACE?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
