# Web actions and keybindings

Omnigent-owned commands use this directory as their single execution and keyboard-routing path. Browser, operating-system, and widget-library behavior stays with its owner.

## Add an action

1. Add a stable dotted ID and typed arguments in `types.ts`.
2. Add user-facing metadata in `catalog.ts`.
3. Register the handler with `useRegisterAction`. Use a global handler only for commands that are truly global; otherwise register inside an `ActionScope`.
4. If the action has a default shortcut, add it to `defaultKeybindings.ts` with the narrowest mode and context expression that describes when it is active.
5. Add focused registry, dispatcher, and component tests. If behavior is user-visible, add or update `tests/e2e_ui` coverage.

Do not persist component names or copy default bindings into preferences. User preferences store ordered overrides by stable action and rule IDs.

## Dispatch behavior

The dispatcher installs the only application shortcut listeners. Handlers return `HANDLED` or `NOT_HANDLED`; a DOM event is consumed only after a handler accepts it. Normal bubble rules yield to `defaultPrevented`, IME composition, AltGraph, and focused widget ownership. Capture rules are exceptional and must be declared in the default map.

Modes follow focused scope ancestry. Use `activation: "active"` only for open surfaces that must respond without focus. More specific scopes and contexts outrank broad ones; user rules outrank defaults.

## What remains local

Keep intrinsic interactions local and claim their events when necessary:

- text editing and browser/OS navigation;
- Radix and cmdk accessibility behavior;
- combobox, menu, slider, radiogroup, resize-handle, and form commit/cancel keys;
- Monaco commands that Omnigent has not overridden;
- ordinary xterm input and terminal-application keys;
- keybinding recording itself.

For example, Enter/Shift+Enter stepping inside a search field is widget behavior, while opening or closing that search surface is an action. Inline comment-edit save/cancel keys are local form behavior and prevent propagation so broader file actions do not also run; new-comment submission remains local and IME-aware.

## Electron

`DesktopActionSync` publishes a versioned full snapshot for the small allowlist in `desktopActionBridge.ts`. Electron validates pinned main-frame senders, keeps snapshots per window, and derives the application menu from the focused window. Native menu clicks invoke action IDs and report whether the renderer handled them; native whole-window Find is the fallback.

Electron continues to own standard roles such as undo, redo, cut, copy, paste, select all, close window, reload, zoom, and fullscreen. Printable, Shift-only, duplicate, or role-reserved renderer bindings are never promoted to app-global native accelerators.

Old shells omit the optional bridge and retain legacy menu behavior. New shells keep legacy fallbacks until a trusted renderer publishes a snapshot.

`noAdHocShortcuts.test.ts` is a deliberately conservative smoke guard, not a parser. It catches literal global listeners and common Monaco/xterm registration APIs; review still decides whether local control handlers are intrinsic behavior or application commands.
