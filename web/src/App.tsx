import { lazy, Suspense } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { ChatPage } from "@/pages/ChatPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import { AppShell } from "@/shell/AppShell";

// Lazy-load the accounts pages so the bundle a header / OIDC
// deploy ships (where accounts is off) doesn't include them in the
// main entry chunk. They're separate chunks that only download
// when the user actually navigates to /login or /register — which
// never happens in non-accounts deploys because the route table
// below doesn't register them. (Members / Policies are lazy-loaded
// too, but inside SettingsPage now that they're settings
// sub-categories rather than standalone routes.)
const LoginPage = lazy(() => import("@/pages/LoginPage").then((m) => ({ default: m.LoginPage })));
const RegisterPage = lazy(() =>
  import("@/pages/RegisterPage").then((m) => ({ default: m.RegisterPage })),
);
const SetupPage = lazy(() => import("@/pages/SetupPage").then((m) => ({ default: m.SetupPage })));
const ApprovePage = lazy(() =>
  import("@/pages/ApprovePage").then((m) => ({ default: m.ApprovePage })),
);
const InboxPage = lazy(() => import("@/pages/InboxPage").then((m) => ({ default: m.InboxPage })));
const SettingsPage = lazy(() =>
  import("@/pages/SettingsPage").then((m) => ({ default: m.SettingsPage })),
);

interface AppProps {
  /**
   * Mount prefix when embedded (e.g. `/ml/omnigent-embed`). The route table
   * matches the FULL pathname, so paths are declared as `${basename}/c/:id`.
   *
   * Why not rely on descendant-route prefix stripping? When embedded, web's
   * `<Routes>` uses the host's externalized react-router instance, but the host
   * mounts via `@databricks/web-shared/routing`, whose internal react-router
   * may be a DIFFERENT physical module — so the parent route-match context that
   * normally rebases descendant routes isn't reliably visible here. Matching
   * the absolute pathname removes that dependency. Standalone passes no
   * basename (BrowserRouter handles the root) and matches relatively (`prefix`
   * is empty, so every path is unchanged from the standalone route table).
   */
  basename?: string;
}

/**
 * The route table. AppShell is the parent layout-route — it renders
 * the sidebar + a main `<Outlet />` and every child route's content
 * lands in that outlet.
 *
 * Both chat routes (index and `c/:conversationId`) render the same
 * `<ChatPage />` directly. ChatPage stays mounted across the
 * transition between them; the chat store (zustand, module-scope)
 * holds the streaming state outside the React tree, so the in-flight
 * fetch survives URL changes. ChatPage observes `useParams` and calls
 * `chatStore.switchTo(...)` to mirror the URL into store state when
 * needed.
 *
 * **Accounts routes are CONDITIONAL** on the ``/v1/info`` probe
 * (see ``main.tsx`` + ``lib/CapabilitiesContext.tsx``). When
 * ``accounts_enabled`` is false — every header / OIDC deploy,
 * including the internal hosted product that syncs from this repo
 * — ``/login`` / ``/register`` / ``/members`` are NOT in the route
 * table at all. Navigating to any of those paths lands on
 * ``<NotFoundPage />``. The bundle still ships those components as
 * separate chunks (via ``React.lazy``) but they're never downloaded.
 *
 * ``/login`` and ``/register`` sit OUTSIDE the AppShell tree on
 * purpose — the shell loads sidebar / conversations / runner health
 * hooks which require an authed identity; mounting them on an
 * unauthed page is at best wasted fetches, at worst an infinite
 * loop with ``identity.ts``'s 401 redirect. Both pages own their
 * own minimal layout (centered card, no chrome).
 *
 * The wildcard route renders `<NotFoundPage />` for anything else. The
 * server's SPA fallback (`_SPAStaticFiles`) hands any extensionless URL
 * to the SPA, so unmatched paths land here after the bundle boots
 * rather than a server 404.
 */
function App({ basename }: AppProps = {}) {
  // Embedded: match the absolute pathname (`${basename}/...`). Standalone
  // (no basename): `prefix` is empty, so every `path` below is identical to
  // the original relative route table.
  const prefix = basename ?? "";
  const info = useServerInfo();
  // While the probe is in flight, render nothing — first paint is
  // ~30ms after boot anyway, and flashing the chrome we may
  // immediately tear down once the probe returns is worse than a
  // tiny blank moment.
  if (info === "loading") return null;

  // First-run: accounts on but no admin claimed yet. Route EVERY path to
  // the Create-admin form so the first visitor lands on it no matter how
  // they arrived (root, a bookmarked deep link, /login). The form's
  // /auth/setup is server-gated to the zero-admin state, and needs_setup
  // flips false the instant it succeeds — so this whole branch disappears
  // after the first admin exists.
  if (info.accounts_enabled && info.needs_setup) {
    return (
      <Suspense fallback={null}>
        <Routes>
          <Route path={basename ? `${prefix}/*` : "*"} element={<SetupPage />} />
        </Routes>
      </Suspense>
    );
  }

  return (
    <Suspense fallback={null}>
      <Routes>
        {info.accounts_enabled && (
          <>
            <Route path={`${prefix}/login`} element={<LoginPage />} />
            <Route path={`${prefix}/register`} element={<RegisterPage />} />
          </>
        )}
        <Route path={`${prefix}/approve/:sessionId/:elicitationId`} element={<ApprovePage />} />
        <Route element={<AppShell />}>
          <Route path={prefix || "/"} element={<ChatPage />} />
          <Route path={`${prefix}/c/:conversationId`} element={<ChatPage />} />
          <Route path={`${prefix}/inbox`} element={<InboxPage />} />
          {/* Settings renders into the chat outlet so the conversations
              sidebar stays put — entering settings only swaps the card's
              content (the section nav) and the main area. The active section
              is carried in the URL (/settings/<section>); bare /settings
              defaults to Appearance. */}
          <Route path={`${prefix}/settings`} element={<SettingsPage />} />
          <Route path={`${prefix}/settings/:section`} element={<SettingsPage />} />
          {/* Members / Policies are now settings sub-categories
              (/settings/members, /settings/policies) so entering them
              keeps the settings sidebar nav instead of dropping back to
              the conversation list. Redirect the old standalone paths so
              existing bookmarks / links still land in the right place.
              Registered in every multi-user mode (accounts AND OIDC) —
              the sections self-gate to admins. */}
          <Route
            path={`${prefix}/members`}
            element={<Navigate to={`${prefix}/settings/members`} replace />}
          />
          <Route
            path={`${prefix}/policies`}
            element={<Navigate to={`${prefix}/settings/policies`} replace />}
          />
          <Route path={basename ? `${prefix}/*` : "*"} element={<NotFoundPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}

export default App;
