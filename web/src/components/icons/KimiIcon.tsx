// Import the Mono glyph directly instead of the package index. The Kimi index
// barrel pulls in a `Color` component whose transitive `@lobehub/fluent-emoji`
// dependency uses an ESM directory import that vitest can't resolve (it breaks
// AgentCard.test collection). `Mono` is the monochrome `currentColor` glyph the
// other harness icons (Cursor/Claude/…) render anyway, and it only depends on
// React. See node_modules/@lobehub/icons/es/Kimi/index.d.ts.
import Kimi from "@lobehub/icons/es/Kimi/components/Mono";

export const KimiIcon = Kimi;
