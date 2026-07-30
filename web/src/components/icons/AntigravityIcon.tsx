// Import the Mono glyph directly, not the package index. The barrel default
// attaches `.Color`/`.Avatar`/`.Combine` statics that pull in antd; we only
// render the monochrome `currentColor` glyph, so the subpath keeps antd out of
// the bundle. Same rationale as KimiIcon.
import Antigravity from "@lobehub/icons/es/Antigravity/components/Mono";

export const AntigravityIcon = Antigravity;
