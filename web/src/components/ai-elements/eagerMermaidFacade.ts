// Streamdown mounts each ```mermaid fence behind `React.lazy(() =>
// import('./mermaid-<hash>.js'))`. That facade module only re-exports a
// component streamdown's static chunk already carries, yet fetching it at
// render time can fail mid-session — a tab held across a redeploy whose old
// hashed chunk is gone, or a passing network blip — and React.lazy caches the
// rejection forever, so every later diagram degrades to the
// MarkdownErrorBoundary fallback ("Could not render this markdown.") until a
// full page reload.
//
// Importing the facade eagerly folds it into the static entry graph: the
// bundler resolves streamdown's dynamic import from a chunk that is already
// loaded, so rendering a diagram never touches the network and cannot fail
// this way. The glob keys off streamdown's published dist layout, which a
// version bump may rename; eagerMermaidFacade.test.ts fails loudly when the
// pattern stops matching so the pin cannot silently vanish.
export const mermaidFacadeModules: Record<string, unknown> = import.meta.glob(
  "/node_modules/streamdown/dist/mermaid-*.js",
  { eager: true },
);
