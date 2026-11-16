// Ownership wrappers: a single-setting widget, a list of user data whose rows are not settings,
// the overlays a setting opens, and the owner a shared component's controls take from the
// Settings site that renders it.

import { createContext, type ReactElement, type ReactNode, useContext, useMemo } from "react";

import { type SettingsEntry, settingsEntryMember } from "./catalog/types";
import {
  type SettingsAlternativeName,
  settingsControlId,
  settingsControlProps,
  settingsDescriptionId,
  settingsLabelId,
  type SettingsRuntimeName,
  settingsTemplatedName,
} from "./settingsControl";

/**
 * A single-setting widget as `entry`'s control, whose id is the control's. Its portaled content,
 * such as a picker's popover, reads it through `useSettingsWidgetPortal`.
 */
export function SettingsControl({
  entry,
  part,
  children,
}: {
  entry: SettingsEntry;
  part?: string;
  children: ReactElement;
}) {
  const id = useMemo(() => settingsControlId(entry, part), [entry, part]);
  return (
    <div className="contents" data-settings-widget={id}>
      <SettingsWidgetContext.Provider value={id}>{children}</SettingsWidgetContext.Provider>
    </div>
  );
}

const SettingsWidgetContext = createContext<string | null>(null);

export interface SettingsWidgetPortalProps {
  "data-settings-overlay-owner"?: string;
  "data-settings-widget"?: string;
}

/** Props for a widget's portaled content, placed under the widget that opens it. */
export function useSettingsWidgetPortal(): SettingsWidgetPortalProps {
  const id = useContext(SettingsWidgetContext);
  return id ? { "data-settings-overlay-owner": id, "data-settings-widget": id } : {};
}

/** A list of user data inside `entry`'s row, as `entry.regions[region]` declares it. */
export function SettingsDataRegion({
  entry,
  region,
  className,
  children,
}: {
  entry: SettingsEntry;
  /** One of `entry.regions`, which declares its row actions and fields. */
  region: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={className} data-settings-region={`${entry.id}:${region}`}>
      {children}
    </div>
  );
}

/**
 * Text a row shows that isn't Settings, such as a document, as `entry.contentAreas` declares
 * it. Its only controls are links in that text.
 */
export function SettingsContent({
  entry,
  area,
  className,
  children,
}: {
  entry: SettingsEntry;
  /** One of `entry.contentAreas`. */
  area: string;
  className?: string;
  children: ReactNode;
}) {
  return (
    <div className={className} data-settings-content={`${entry.id}:${area}`}>
      {children}
    </div>
  );
}

/**
 * A `title` for an owned control that adds no tooltip. The wrapper otherwise sets the tooltip to
 * the control's name, which would show Settings a hover its shared control never had.
 */
export const SETTINGS_NO_TOOLTIP = { title: "" } as const;

/** Props for a dialog's or popover's content, linking it to the part that opens it. */
export function settingsOverlayProps(
  entry: SettingsEntry,
  part?: string,
): { "data-settings-overlay-owner": string } {
  return { "data-settings-overlay-owner": settingsControlId(entry, part) };
}

/**
 * Props for the dialog a row of `entry`'s `region` opens through `action`, as
 * `entry.regions[region].rowOverlays[action]` declares it.
 */
export function settingsRowOverlayProps(
  entry: SettingsEntry,
  region: string,
  action: string,
): { "data-settings-row-overlay": string } {
  return { "data-settings-row-overlay": `${entry.id}:${region}:${action}` };
}

/**
 * Props for an option of `entry`'s `part` overlay that's named from runtime data, such as an
 * installed app.
 */
export function settingsRuntimeChoiceProps(
  entry: SettingsEntry,
  part: string,
): { "data-settings-runtime-choice": string } {
  return { "data-settings-runtime-choice": settingsControlId(entry, part) };
}

interface SettingsOwnerValue {
  entry: SettingsEntry;
  /** The entry's part the shared component's control is; its main control when absent. */
  part?: string;
}

const SettingsOwnerContext = createContext<SettingsOwnerValue | null>(null);

/**
 * At a Settings site, makes `entry` (or one of its parts) the owner of the shared component
 * rendered inside, so its control is named and found as that entry. Elsewhere the same
 * component renders without one and keeps its own names.
 */
export function SettingsOwner({
  entry,
  part,
  children,
}: {
  entry: SettingsEntry;
  part?: string;
  children: ReactNode;
}) {
  const value = useMemo(() => ({ entry, part }), [entry, part]);
  return <SettingsOwnerContext.Provider value={value}>{children}</SettingsOwnerContext.Provider>;
}

/** The names a shared control shows outside any owner, as it did before it was shared. */
export interface SettingsOutsideNames {
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
  title?: string;
}

export interface SettingsOwnership {
  /** The owner's entry, inside a `SettingsOwner`. */
  entry?: SettingsEntry;
  /**
   * Props naming the shared component's control: the owner's, inside an owner, with a
   * templated part's runtime value; `outside` everywhere else. `title` is the tooltip,
   * which defaults to the name.
   */
  control: (options?: {
    name?: SettingsRuntimeName;
    title?: string;
    outside?: SettingsOutsideNames;
  }) => SettingsOutsideNames & { "data-settings-control"?: string };
  /** The ids its label and description take: the owner's row's inside one, `outside` elsewhere. */
  labelIds: (outside: { labelId: string; descriptionId: string }) => {
    labelId: string;
    descriptionId: string;
  };
  /** Props for the overlay content its control opens; none outside an owner. */
  overlay: () => { "data-settings-overlay-owner"?: string };
}

/** What a shared component's controls are named and found as, from the Settings site around it. */
export function useSettingsOwnership(): SettingsOwnership {
  const owner = useContext(SettingsOwnerContext);
  return useMemo(() => {
    if (!owner) {
      return {
        control: (options) => ({ ...options?.outside }),
        labelIds: (outside) => outside,
        overlay: () => ({}),
      };
    }
    const { entry, part } = owner;
    return {
      entry,
      control: (options) => {
        // The part is one of the owner's entry's, which the type can't tie to it here.
        const props = settingsControlProps(
          entry,
          part as never,
          part === undefined ? undefined : options?.name,
        );
        const name =
          part !== undefined && options?.name
            ? settingsTemplatedName(entry, part, options.name)
            : props["aria-label"];
        return options?.title !== undefined || name !== undefined
          ? { ...props, title: options?.title ?? name }
          : props;
      },
      labelIds: () => ({
        labelId: settingsLabelId(entry),
        descriptionId: settingsDescriptionId(entry),
      }),
      overlay: () => settingsOverlayProps(entry, part),
    };
  }, [owner]);
}

interface SettingsOverlayOwnerValue {
  entry: SettingsEntry;
  /** The part, or "" for the main control, that opened the overlay this owner names. */
  opener: string;
}

const SettingsOverlayOwnerContext = createContext<SettingsOverlayOwnerValue | null>(null);

/**
 * At a Settings site, makes `entry`'s `opener` overlay the owner of the shared dialog rendered
 * inside, so each of its controls is named and found as one of that overlay's parts. Elsewhere
 * the same dialog renders without one and keeps its own names.
 */
export function SettingsOverlayOwner({
  entry,
  opener,
  children,
}: {
  entry: SettingsEntry;
  opener: string;
  children: ReactNode;
}) {
  const value = useMemo(() => ({ entry, opener }), [entry, opener]);
  return (
    <SettingsOverlayOwnerContext.Provider value={value}>
      {children}
    </SettingsOverlayOwnerContext.Provider>
  );
}

/** The parts of the overlay a shared dialog is rendered in, from the Settings site around it. */
export interface SettingsOverlayParts {
  /** The overlay's entry, inside a `SettingsOverlayOwner`. */
  entry?: SettingsEntry;
  /**
   * Props naming one of the overlay's parts: the owner's, with a declared alternative or a
   * templated part's runtime value; `outside` everywhere else. `title` defaults to the name.
   */
  control: (
    part: string,
    options?: {
      name?: SettingsRuntimeName | SettingsAlternativeName;
      title?: string;
      outside?: SettingsOutsideNames;
    },
  ) => SettingsOutsideNames & { "data-settings-control"?: string };
  /** Props for the overlay's content; none outside an owner. */
  overlay: () => { "data-settings-overlay-owner"?: string };
  /**
   * The same parts, for an owner whose entry is one of `members`' (and, given `opener`, opened
   * from it); undefined for any other Settings owner, whose dialog never shows these parts. With
   * no Settings owner at all, the unowned parts, so the dialog renders as it does elsewhere.
   */
  within: (members: readonly string[], opener?: string) => SettingsOverlayParts | undefined;
  /** Lends these parts to a shared component rendered inside, which reads them by its own hook. */
  Provider: (props: { children: ReactNode }) => ReactNode;
}

// One parts object, and so one Provider type, per owner: a narrowing that keeps the same owner
// returns the same one, so what a Provider wraps isn't remounted when its parent renders again.
const OWNED_PARTS = new WeakMap<SettingsOverlayOwnerValue, SettingsOverlayParts>();
let unownedParts: SettingsOverlayParts | undefined;

function overlayParts(owner: SettingsOverlayOwnerValue | null): SettingsOverlayParts {
  if (!owner) return (unownedParts ??= makeOverlayParts(null));
  let parts = OWNED_PARTS.get(owner);
  if (!parts) {
    parts = makeOverlayParts(owner);
    OWNED_PARTS.set(owner, parts);
  }
  return parts;
}

function makeOverlayParts(owner: SettingsOverlayOwnerValue | null): SettingsOverlayParts {
  const Provider = ({ children }: { children: ReactNode }) => (
    <SettingsOverlayOwnerContext.Provider value={owner}>
      {children}
    </SettingsOverlayOwnerContext.Provider>
  );
  if (!owner) {
    return {
      control: (_part, options) => ({ ...options?.outside }),
      overlay: () => ({}),
      // Outside Settings every part renders, unowned, with its own names.
      within: () => overlayParts(null),
      Provider,
    };
  }
  const { entry, opener } = owner;
  return {
    entry,
    control: (part, options) => {
      // The part is one of the owner's entry's, which the type can't tie to it here.
      const props = settingsControlProps(entry, part as never, options?.name as never);
      const name = props["aria-label"];
      return options?.title !== undefined || name !== undefined
        ? { ...props, title: options?.title ?? name }
        : props;
    },
    overlay: () => settingsOverlayProps(entry, opener || undefined),
    within: (members, withinOpener) => {
      const member = settingsEntryMember(entry);
      const inside =
        member !== undefined &&
        members.includes(member) &&
        (withinOpener === undefined || withinOpener === opener);
      return inside ? overlayParts(owner) : undefined;
    },
    Provider,
  };
}

/** What a shared dialog's controls are named and found as, from the Settings site around it. */
export function useSettingsOverlayParts(): SettingsOverlayParts {
  const owner = useContext(SettingsOverlayOwnerContext);
  return useMemo(() => overlayParts(owner), [owner]);
}
