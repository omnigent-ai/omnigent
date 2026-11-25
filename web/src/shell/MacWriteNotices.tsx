// What a pull request control says about changes made on this Mac: which account makes
// them, and that one may have gone through before the workspace user changed.

import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useSyncExternalStore,
  type ReactNode,
} from "react";

import { Button } from "@/components/ui/button";
import { useMacWriteRoute } from "@/newPullRequestPage/hooks/usePullRequests";
import {
  dismissAbandonedMacWrite,
  draftOwner,
  type MacWriteOperation,
  useAbandonedMacWrite,
} from "@/lib/desktopPullRequestWrites";

/** A GitHub account a page shows by name, such as its toolbar's "This Mac · @login". */
export interface VisibleMacAccount {
  host: string;
  login: string;
}

/** The accounts the controls of one page show right now; a page's own registry sees its parent's too. */
class VisibleMacAccounts {
  private readonly shown = new Map<symbol, VisibleMacAccount>();
  private readonly listeners = new Set<() => void>();
  private version = 0;

  private readonly parent: VisibleMacAccounts | null;

  constructor(parent: VisibleMacAccounts | null) {
    this.parent = parent;
  }

  show(account: VisibleMacAccount): () => void {
    const id = Symbol("shown account");
    this.shown.set(id, account);
    this.changed();
    return () => {
      this.shown.delete(id);
      this.changed();
    };
  }

  shows(host: string, login: string): boolean {
    const sameAccount = (account: VisibleMacAccount) =>
      account.host.toLowerCase() === host.toLowerCase() &&
      account.login.toLowerCase() === login.toLowerCase();
    return [...this.shown.values()].some(sameAccount) || !!this.parent?.shows(host, login);
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    const stopParent = this.parent?.subscribe(listener);
    return () => {
      this.listeners.delete(listener);
      stopParent?.();
    };
  };

  snapshot = (): string => `${this.parent?.snapshot() ?? ""}/${this.version}`;

  private changed(): void {
    this.version += 1;
    for (const listener of this.listeners) listener();
  }
}

const VisibleMacAccountsContext = createContext<VisibleMacAccounts | null>(null);

/** The page whose controls register the accounts they show, for its pickers' notices. */
export function VisibleMacAccountsProvider({ children }: { children: ReactNode }) {
  const parent = useContext(VisibleMacAccountsContext);
  const registry = useMemo(() => new VisibleMacAccounts(parent), [parent]);
  return (
    <VisibleMacAccountsContext.Provider value={registry}>
      {children}
    </VisibleMacAccountsContext.Provider>
  );
}

/**
 * Say this page shows `account` by name, while it's rendered and not null. Only a control
 * the user actually sees calls this, never a hidden copy made to measure it.
 */
export function useShowsMacAccount(account: VisibleMacAccount | null): void {
  const registry = useContext(VisibleMacAccountsContext);
  const host = account?.host ?? null;
  const login = account?.login ?? null;
  useEffect(() => {
    if (!registry || !host || !login) return;
    return registry.show({ host, login });
  }, [registry, host, login]);
}

/** Whether this page shows `login` on `host` by name somewhere already. */
function useShowsAccount(host: string | null, login: string | null): boolean {
  const registry = useContext(VisibleMacAccountsContext);
  useSyncExternalStore(
    registry?.subscribe ?? noSubscription,
    registry?.snapshot ?? noSnapshot,
    noSnapshot,
  );
  return !!registry && !!host && !!login && registry.shows(host, login);
}

const noSubscription = () => () => {};
const noSnapshot = () => "";

/**
 * "Changes are made as @login" in a picker, where its changes go to this Mac. Left out where the
 * page already shows that same account by name, as the Pull Requests page's toolbar does.
 */
export function MacWriteActingAccount({ prUrl }: { prUrl: string }) {
  const route = useMacWriteRoute(prUrl);
  const shown = useShowsAccount(route?.target.host ?? null, route?.actor.login ?? null);
  if (!route || shown) return null;
  return (
    <p className="truncate border-t border-border px-3 py-1.5 text-xs text-muted-foreground">
      Changes are made as @{route.actor.login}
    </p>
  );
}

/**
 * For the user who made it only: a change of `subject` (e.g. "labels") that was sent on
 * this Mac before they switched accounts may or may not have gone through.
 */
export function MacWriteAbandonedNotice({
  prUrl,
  operation,
  subject,
}: {
  prUrl: string;
  operation: MacWriteOperation;
  subject: string;
}) {
  const abandoned = useAbandonedMacWrite(prUrl, operation);
  if (!abandoned) return null;
  return (
    <div role="status" className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ui">
      <p className="min-w-0 text-muted-foreground">
        A change to {subject} may have been made before you switched accounts. Check the pull
        request on GitHub.
      </p>
      <Button asChild variant="link" size="sm" className="h-auto p-0">
        <a href={prUrl} target="_blank" rel="noreferrer">
          Open on GitHub
        </a>
      </Button>
      <Button
        type="button"
        variant="link"
        size="sm"
        className="h-auto p-0 text-muted-foreground"
        onClick={() => dismissAbandonedMacWrite(draftOwner(), prUrl, operation)}
      >
        Dismiss
      </Button>
    </div>
  );
}
