// Routing inversion-of-control seam.
//
// ap-web components consume routing through these abstractions instead of
// importing `react-router-dom` directly. The actual implementation comes from
// the nearest `RoutingProvider`; when none is present it falls back to
// react-router-dom. This keeps standalone (and the existing tests, which only
// wrap in `<MemoryRouter>`) working untouched, while letting an embedding host
// override individual primitives via `RoutingProvider` (see `embed.tsx`'s
// `routing` mount option).
//
// Scope: only the *consumption* primitives are abstracted (the things
// components call). Route *definition* (`Routes`/`Route`) stays on
// react-router-dom and matches relatively in both modes.
//
// Embedded mode (same-root): `react-router`/`react-router-dom` are bare
// externals resolved by the host's rspack to its own instance (see
// `vite.embed.config.ts`), and `OmnigentApp` renders WITHOUT its own `<Router>`
// (rendering one inside the
// host's router throws). ap-web's `<Routes>` become descendant routes of the
// host router. Since ap-web's `navigate()`/`<Link to>` targets are absolute,
// the host injects `basenamedRouting(basename)` via `RoutingProvider` so they
// land under the mount path instead of the host root (see `basenamedRouting`).

import { type ComponentPropsWithoutRef, type ReactNode, createContext, forwardRef, useContext } from "react";
import {
  Link as RRLink,
  type NavigateOptions,
  Outlet as RROutlet,
  type To,
  useLocation as useRRLocation,
  useNavigate as useRRNavigate,
  useParams as useRRParams,
  useSearchParams as useRRSearchParams,
} from "react-router-dom";

/**
 * The routing contract ap-web depends on. Types are taken verbatim from
 * react-router-dom so call sites are identical and any host implementation
 * must conform to the same shapes (the adapter's job).
 */
export interface RoutingApi {
  useNavigate: typeof useRRNavigate;
  useParams: typeof useRRParams;
  useSearchParams: typeof useRRSearchParams;
  useLocation: typeof useRRLocation;
  Link: typeof RRLink;
  Outlet: typeof RROutlet;
  /**
   * Rebase an app-absolute path (`/c/:id`) the same way `navigate()`/`<Link>`
   * targets are rebased. Identity in standalone; prepends `basename` in the
   * embed. Used to build absolute URLs (e.g. a shareable link) that must land
   * under the host mount path.
   */
  rebasePath: (path: string) => string;
}

/** Default implementation: plain react-router-dom. */
export const reactRouterRouting: RoutingApi = {
  useNavigate: useRRNavigate,
  useParams: useRRParams,
  useSearchParams: useRRSearchParams,
  useLocation: useRRLocation,
  Link: RRLink,
  Outlet: RROutlet,
  rebasePath: (path) => path,
};

/**
 * Prepend `basename` to an absolute path. Relative paths (no leading `/`) are
 * passed through unchanged, as are paths already under the basename.
 */
function rebasePath(path: string, basename: string): string {
  if (!path.startsWith("/")) return path;
  // Avoid double-prefixing if already under the basename.
  if (path === basename || path.startsWith(`${basename}/`)) return path;
  return `${basename}${path}`;
}

/**
 * Prepend `basename` to an absolute `to` value. Handles both forms ap-web uses:
 * a string path ("/", "/c/:id") and the object form ({ pathname, search, hash })
 * — the latter is rebased on its `pathname` while `search`/`hash` pass through.
 * Relative `to` values (no leading `/`) are left unchanged.
 */
function rebaseTo(to: To, basename: string): To {
  if (typeof to === "string") return rebasePath(to, basename);
  if (to.pathname) return { ...to, pathname: rebasePath(to.pathname, basename) };
  return to;
}

/**
 * Build a `RoutingApi` whose navigation + links are rebased under `basename`,
 * while route matching (useParams/useSearchParams/useLocation/Outlet) uses the
 * host's react-router as-is.
 *
 * This is the same-root answer to the basename problem: ap-web renders its
 * `<Routes>` as DESCENDANT routes of the host's router (no nested `<Router>`),
 * so its relative route definitions match under the host mount path — but its
 * absolute `navigate()`/`<Link to>` targets must be rebased so they land under
 * the mount instead of the host root.
 */
export function basenamedRouting(basename: string, base: RoutingApi = reactRouterRouting): RoutingApi {
  return {
    ...base,
    useNavigate: () => {
      const navigate = base.useNavigate();
      return ((to: To | number, options?: NavigateOptions) => {
        if (typeof to === "number") return navigate(to);
        return navigate(rebaseTo(to, basename), options);
      }) as ReturnType<typeof useRRNavigate>;
    },
    Link: forwardRef<HTMLAnchorElement, ComponentPropsWithoutRef<typeof RRLink>>((props, ref) => {
      const Impl = base.Link;
      return <Impl ref={ref} {...props} to={rebaseTo(props.to, basename)} />;
    }),
    rebasePath: (path) => rebasePath(path, basename),
  };
}

const RoutingContext = createContext<RoutingApi | null>(null);

export interface RoutingProviderProps {
  value: RoutingApi;
  children: ReactNode;
}

export function RoutingProvider({ value, children }: RoutingProviderProps) {
  return <RoutingContext.Provider value={value}>{children}</RoutingContext.Provider>;
}

/** The active implementation — provider value, or react-router-dom fallback. */
function useRouting(): RoutingApi {
  return useContext(RoutingContext) ?? reactRouterRouting;
}

export const useNavigate: typeof useRRNavigate = () => useRouting().useNavigate();

export function useParams<
  ParamsOrKey extends string | Record<string, string | undefined> = string,
>() {
  return useRouting().useParams<ParamsOrKey>();
}

export const useSearchParams: typeof useRRSearchParams = (defaultInit) =>
  useRouting().useSearchParams(defaultInit);

export const useLocation: typeof useRRLocation = () => useRouting().useLocation();

/**
 * The active `rebasePath` primitive. Identity standalone; prepends `basename`
 * in the embed. Use to construct absolute URLs that respect the mount path.
 */
export const useRebasePath = (): ((path: string) => string) => useRouting().rebasePath;

export const Link = forwardRef<HTMLAnchorElement, ComponentPropsWithoutRef<typeof RRLink>>(
  (props, ref) => {
    const { Link: Impl } = useRouting();
    return <Impl ref={ref} {...props} />;
  },
);
Link.displayName = "Link";

export const Outlet: typeof RROutlet = (props) => {
  const { Outlet: Impl } = useRouting();
  return <Impl {...props} />;
};
