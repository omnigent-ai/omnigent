// The GitHub accounts a page's controls show by name, so its pickers needn't repeat them.

import { createContext, useContext, useEffect, useMemo, type ReactNode } from "react";

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
