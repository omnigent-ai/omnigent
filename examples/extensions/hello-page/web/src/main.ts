import { defineExtension, type Disposable, type ExtensionContext } from "@omnigent/extension-sdk";
import "./style.css";

let themeSubscription: Disposable | null = null;

function render(context: ExtensionContext, visits: number): void {
  const root = document.getElementById("root");
  if (!root) throw new Error("Extension root is missing");
  root.innerHTML = `
    <main class="page">
      <section class="card">
        <span class="eyebrow">Omnigent extension</span>
        <h1>Hello from an isolated page</h1>
        <p>This UI runs in an opaque-origin iframe and uses only the permission-checked host API.</p>
        <p class="visits">Opened <strong>${visits}</strong> time${visits === 1 ? "" : "s"} in this browser.</p>
        <div class="actions">
          <button id="new-session" type="button">New session</button>
          <button id="self" type="button">Open this page</button>
        </div>
        <output id="status" aria-live="polite"></output>
      </section>
    </main>`;
  const status = root.querySelector<HTMLOutputElement>("#status")!;
  root.querySelector<HTMLButtonElement>("#new-session")!.addEventListener("click", () => {
    void context.navigation.openNewSession().catch((error: unknown) => {
      status.textContent = error instanceof Error ? error.message : "Navigation failed";
    });
  });
  root.querySelector<HTMLButtonElement>("#self")!.addEventListener("click", () => {
    void context.navigation.openPage("omnigent.hello-page.home", { source: "button" });
  });
}

defineExtension({
  async activate(context) {
    const previous = await context.storage.user.get<number>("visits");
    const visits = (previous ?? 0) + 1;
    await context.storage.user.set("visits", visits);
    render(context, visits);
    const theme = await context.theme.getCurrent();
    document.documentElement.dataset.theme = theme.theme;
    themeSubscription = await context.theme.subscribe((next) => {
      document.documentElement.dataset.theme = next.theme;
    });
  },
  deactivate() {
    themeSubscription?.dispose();
    themeSubscription = null;
  },
});
