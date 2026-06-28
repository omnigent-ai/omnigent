/* eslint-disable no-underscore-dangle -- these mirror VS Code's `vs/base/common/*`
   utilities, which use `_`-prefixed private members; kept identical so the addon
   that consumes them reads the same as upstream. */
// Minimal standalone replacements for the VS Code platform utilities that
// `terminalTypeAheadAddon.ts` (vendored from microsoft/vscode, MIT) depends on.
// Upstream imports these from `vs/base/common/*`; outside the VS Code repo we
// provide just the slices the addon uses. Kept deliberately small and free of
// any VS Code coupling.

/** Something that can be torn down. Mirrors `vs/base/common/lifecycle` IDisposable. */
export interface IDisposable {
  dispose(): void;
}

/** Wrap a teardown callback as an IDisposable. */
export function toDisposable(fn: () => void): IDisposable {
  return { dispose: fn };
}

/**
 * Base class holding child disposables, registered via {@link Disposable._register}
 * and torn down together on {@link Disposable.dispose}. A trimmed copy of
 * `vs/base/common/lifecycle` Disposable — no leak tracking, same semantics.
 */
export class Disposable implements IDisposable {
  // Upstream exposes a `DisposableStore` as `this._store`; the addon passes it
  // to `disposableTimeout` as the owning store. We model it as the same object
  // that `_register` adds to, exposing an `add` method.
  protected readonly _store: DisposableStore = new DisposableStore();

  protected _register<T extends IDisposable>(o: T): T {
    this._store.add(o);
    return o;
  }

  dispose(): void {
    this._store.dispose();
  }
}

/** Holds a set of disposables and disposes them all at once. */
export class DisposableStore implements IDisposable {
  private readonly _items = new Set<IDisposable>();
  private _isDisposed = false;

  add<T extends IDisposable>(o: T): T {
    if (this._isDisposed) {
      o.dispose();
      return o;
    }
    this._items.add(o);
    return o;
  }

  dispose(): void {
    if (this._isDisposed) {
      return;
    }
    this._isDisposed = true;
    for (const item of this._items) {
      item.dispose();
    }
    this._items.clear();
  }
}

/** Listener callback. */
export type Event<T> = (listener: (e: T) => void) => IDisposable;

/**
 * Tiny event emitter mirroring `vs/base/common/event` Emitter: `.event` is a
 * subscribe function returning an unsubscribe disposable, `.fire(value)`
 * notifies current listeners.
 */
export class Emitter<T> implements IDisposable {
  private _listeners?: Set<(e: T) => void>;

  readonly event: Event<T> = (listener: (e: T) => void): IDisposable => {
    (this._listeners ??= new Set()).add(listener);
    return toDisposable(() => this._listeners?.delete(listener));
  };

  fire(event: T): void {
    if (!this._listeners) {
      return;
    }
    // Snapshot so a listener that (un)subscribes during dispatch is safe.
    for (const listener of [...this._listeners]) {
      listener(event);
    }
  }

  dispose(): void {
    this._listeners?.clear();
    this._listeners = undefined;
  }
}

/**
 * Run `fn` after `delayMs`, returning a disposable that cancels the pending
 * timer. If `store` is provided the timeout is registered to it so it's
 * cancelled on store disposal. Mirrors `vs/base/common/async` disposableTimeout.
 */
export function disposableTimeout(
  fn: () => void,
  delayMs: number,
  store?: DisposableStore,
): IDisposable {
  const timer = setTimeout(fn, delayMs);
  const disposable = toDisposable(() => clearTimeout(timer));
  store?.add(disposable);
  return disposable;
}

/**
 * Returns a debounced wrapper of `fn`: rapid calls within `delayMs` collapse to
 * a single trailing invocation. Replaces upstream's `@debounce(ms)` *method
 * decorator* (unavailable here: ap-web's tsconfig has no `experimentalDecorators`
 * and uses `erasableSyntaxOnly`). Call sites wrap the method explicitly instead.
 */
export function debounce<A extends unknown[]>(
  fn: (...args: A) => void,
  delayMs: number,
): (...args: A) => void {
  let timer: ReturnType<typeof setTimeout> | undefined;
  return (...args: A) => {
    if (timer !== undefined) {
      clearTimeout(timer);
    }
    timer = setTimeout(() => {
      timer = undefined;
      fn(...args);
    }, delayMs);
  };
}

/** Escape a string for literal use inside a RegExp. From `vs/base/common/strings`. */
export function escapeRegExpCharacters(value: string): string {
  return value.replace(/[\\{}*+?|^$.[\]()]/g, "\\$&");
}

/** Type guard for finite/any numbers. From `vs/base/common/types`. */
export function isNumber(obj: unknown): obj is number {
  return typeof obj === "number" && !isNaN(obj);
}

/** A value that may be a single item or an array of them. From `vs/base/common/types`. */
export type SingleOrMany<T> = T | T[];

/**
 * RGBA color, 0-255 channels and 0-1 alpha. Trimmed from `vs/base/common/color`
 * — the addon only constructs one and reads `.r/.g/.b`.
 */
export class RGBA {
  readonly r: number;
  readonly g: number;
  readonly b: number;
  readonly a: number;

  constructor(r: number, g: number, b: number, a = 1) {
    this.r = r;
    this.g = g;
    this.b = b;
    this.a = a;
  }
}

/**
 * Minimal Color supporting just what the addon uses: `Color.fromHex('#rrggbb')`
 * (throws on bad input, matching upstream's try/catch contract) and `.rgba`.
 */
export class Color {
  readonly rgba: RGBA;

  constructor(rgba: RGBA) {
    this.rgba = rgba;
  }

  static fromHex(hex: string): Color {
    const match = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(hex.trim());
    if (!match) {
      throw new Error(`Invalid hex color: ${hex}`);
    }
    return new Color(
      new RGBA(parseInt(match[1]!, 16), parseInt(match[2]!, 16), parseInt(match[3]!, 16), 1),
    );
  }
}
