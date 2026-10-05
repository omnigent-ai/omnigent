// What brands a deck: the workspace's design-system pointer when it exists,
// otherwise its design kit. Reads and the owner check are injected so this
// stays free of fetch. Never throws; problems become a notice.

import {
  DESIGN_SYSTEM_POINTER,
  isAbsoluteDesignSystemPath,
  parseDesignSystemPointer,
} from "@/lib/designSystem";
import { injectDesignSystem } from "@/lib/designSystemInjection";
import {
  kitText,
  loadDesignKit,
  type DesignKitState,
  type KitFile,
  type KitStyleOptions,
} from "./codeViewerHelpers";

export const DS_OWNER_ONLY = "Design system is only available to the session owner";
export const DS_UNREADABLE = "Design system folder is not readable from this session; import it";
export const dsNotApplied = (reason: string) => `Design system not applied: ${reason}`;
export const kitNotApplied = (reason: string) => `Design kit not applied: ${reason}`;

export interface DeckBranding {
  /** Kit style, injected after the deck's own styles. */
  kitStyle: string;
  /** Design-system style, injected before the deck's own styles. */
  systemStyle: string;
  /** The deck with `ds:` references rewritten; `null` renders it as is. */
  content: string | null;
  badge: { kind: "kit" | "system"; name: string } | null;
  notice: string | null;
}

export const NO_BRANDING: DeckBranding = {
  kitStyle: "",
  systemStyle: "",
  content: null,
  badge: null,
  notice: null,
};

export interface BrandingDeps {
  /** Workspace-relative or absolute read; resolves `null` when the file does not exist. */
  read: (path: string) => Promise<KitFile | null>;
  /** Whether the viewer owns the session (absolute reads are owner-only). */
  isOwner: () => Promise<boolean>;
  /** Called once a full design system is found, before its files load. */
  onDesignSystem?: () => void;
}

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));
export const withNotice = (notice: string): DeckBranding => ({ ...NO_BRANDING, notice });

export function brandingFromKit(kit: DesignKitState): DeckBranding {
  if (kit.status === "error") return withNotice(kitNotApplied(kit.reason));
  if (kit.status === "none") return NO_BRANDING;
  return { ...NO_BRANDING, kitStyle: kit.style, badge: { kind: "kit", name: kit.name } };
}

/** `kit` shapes only the kit style; design-system injection is the same for every kind. */
export async function loadDeckBranding(
  content: string,
  deps: BrandingDeps,
  kit: KitStyleOptions = {},
): Promise<DeckBranding> {
  let pointerFile: KitFile | null;
  try {
    pointerFile = await deps.read(DESIGN_SYSTEM_POINTER);
  } catch {
    // Same failure the kit read will name; the kit path reports it.
    pointerFile = null;
  }
  if (!pointerFile) return brandingFromKit(await loadDesignKit(deps.read, kit));

  try {
    const ref = parseDesignSystemPointer(kitText(pointerFile, "design-system.json"));
    const badge = { kind: "system" as const, name: ref.name };
    if (ref.kind === "skill") return { ...NO_BRANDING, badge };
    const absolute = isAbsoluteDesignSystemPath(ref.path);
    if (absolute && !(await deps.isOwner())) return withNotice(DS_OWNER_ONLY);
    deps.onDesignSystem?.();
    try {
      const injected = await injectDesignSystem(content, (p) => deps.read(`${ref.path}/${p}`));
      return { ...NO_BRANDING, systemStyle: injected.style, content: injected.content, badge };
    } catch (e) {
      if (absolute && message(e).startsWith("403")) return withNotice(DS_UNREADABLE);
      throw e;
    }
  } catch (e) {
    return withNotice(dsNotApplied(message(e)));
  }
}
