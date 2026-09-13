import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { FileContentResponse } from "@/hooks/useFileContent";

// ── Mocks ───────────────────────────────────────────────────────────────────
//
// jsdom has no WebGL, so three.js and its loaders are stubbed. The stubs are
// configurable per test via module-level state so we can drive: which loader
// parses (asserting MIME-selected loader), an empty/degenerate result, a parse
// throw, a post-renderer failure, and a rejected blob read. The renderer stub
// records forceContextLoss/dispose so teardown can be asserted.

interface LoaderBehavior {
  // What the active loader's parse should do.
  mode: "valid" | "empty" | "nan" | "throw";
  // If set, the OrbitControls constructor throws AFTER the renderer is created,
  // exercising the partial-init failure/teardown path.
  orbitThrows: boolean;
  // If set, the parsed object carries a mesh whose material references textures
  // (as a textured 3MF does), so teardown's texture disposal can be asserted.
  texturedMaterial: boolean;
}

const behavior: LoaderBehavior = {
  mode: "valid",
  orbitThrows: false,
  texturedMaterial: false,
};

// Textures the textured-material mesh references; disposed flags are asserted
// by the teardown test to prove the material's textures are freed, not leaked.
interface TextureRecord {
  isTexture: true;
  disposed: boolean;
  dispose: () => void;
}
function makeTextureRecord(): TextureRecord {
  const rec: TextureRecord = { isTexture: true, disposed: false, dispose: () => {} };
  rec.dispose = () => {
    rec.disposed = true;
  };
  return rec;
}
const materialTextures: { map: TextureRecord; normalMap: TextureRecord } = {
  map: makeTextureRecord(),
  normalMap: makeTextureRecord(),
};

// Records so tests can assert which loader ran and that teardown happened.
const parseCalls: string[] = [];
interface RendererRecord {
  disposed: boolean;
  contextLost: boolean;
  clearColor?: number;
}
let lastRenderer: RendererRecord | null = null;

// The mesh's material, exposed so theme tests can assert its color. Set when
// the STL loader runs (the only format that builds its own material).
let lastMaterial: { color: number } | null = null;

function makeParsedObject() {
  // A textured mesh mirrors what a 3MF loader yields: a material whose slots
  // (map, normalMap) hold Texture instances. disposeObject must free those, so
  // dispose() flips the shared records the teardown test asserts.
  const child = behavior.texturedMaterial
    ? {
        geometry: { dispose: () => {} },
        material: {
          map: materialTextures.map,
          normalMap: materialTextures.normalMap,
          dispose: () => {},
        },
      }
    : { geometry: null, material: null };
  // Object3D stand-in. Box3.setFromObject reads boxSpec to decide bounds.
  return {
    boxSpec:
      behavior.mode === "empty"
        ? { empty: true, nan: false }
        : behavior.mode === "nan"
          ? { empty: false, nan: true }
          : { empty: false, nan: false },
    position: { sub: () => {} },
    traverse: (cb: (child: unknown) => void) => cb(child),
  };
}

function loaderStub(name: string) {
  return class {
    parse() {
      parseCalls.push(name);
      if (behavior.mode === "throw") throw new Error("malformed model");
      return makeParsedObject();
    }
  };
}

vi.mock("three/examples/jsm/loaders/STLLoader.js", () => ({ STLLoader: loaderStub("stl") }));
vi.mock("three/examples/jsm/loaders/3MFLoader.js", () => ({ ThreeMFLoader: loaderStub("3mf") }));
vi.mock("three/examples/jsm/loaders/OBJLoader.js", () => ({ OBJLoader: loaderStub("obj") }));

vi.mock("three/examples/jsm/controls/OrbitControls.js", () => ({
  OrbitControls: class {
    constructor() {
      if (behavior.orbitThrows) throw new Error("orbit init failed");
    }
    enableDamping = false;
    target = { set: () => {} };
    update() {}
    dispose() {}
  },
}));

vi.mock("three", () => {
  class Vector3 {
    x = 0;
    y = 0;
    z = 0;
    set() {
      return this;
    }
    sub() {
      return this;
    }
  }
  class Box3 {
    min = { x: 0, y: 0, z: 0 };
    max = { x: 1, y: 1, z: 1 };
    private empty = false;
    setFromObject(obj: { boxSpec?: { empty: boolean; nan: boolean } }) {
      const box = obj.boxSpec ?? { empty: false, nan: false };
      this.empty = box.empty;
      if (box.nan) this.max = { x: NaN, y: 1, z: 1 };
      return this;
    }
    isEmpty() {
      return this.empty;
    }
    getSize() {
      return { x: 1, y: 1, z: 1 };
    }
    getCenter() {
      return { x: 0, y: 0, z: 0 };
    }
  }
  class PerspectiveCamera {
    fov = 45;
    aspect = 1;
    near = 0.1;
    far = 1000;
    position = new Vector3();
    updateProjectionMatrix() {}
  }
  class WebGLRenderer {
    domElement = document.createElement("canvas");
    // A shared record so tests can assert teardown without aliasing `this`.
    record: RendererRecord = { disposed: false, contextLost: false };
    constructor() {
      lastRenderer = this.record;
    }
    setPixelRatio() {}
    setSize() {}
    setClearColor(color: number) {
      this.record.clearColor = color;
    }
    render() {}
    dispose() {
      this.record.disposed = true;
    }
    forceContextLoss() {
      this.record.contextLost = true;
    }
  }
  class Scene {
    add() {}
    remove() {}
  }
  class Mesh {
    geometry = null;
    material = null;
    position = new Vector3();
    traverse(cb: (c: unknown) => void) {
      cb(this);
    }
  }
  return {
    Vector3,
    Box3,
    PerspectiveCamera,
    WebGLRenderer,
    Scene,
    Mesh,
    MeshStandardMaterial: class {
      color: { setHex: (hex: number) => void };
      constructor(opts?: { color?: number }) {
        const rec = { hex: opts?.color ?? 0 };
        this.color = {
          setHex: (hex: number) => {
            rec.hex = hex;
            if (lastMaterial) lastMaterial.color = hex;
          },
        };
        lastMaterial = { color: rec.hex };
      }
      dispose() {}
    },
    HemisphereLight: class {
      position = new Vector3();
      intensity: number;
      constructor(_sky?: number, _ground?: number, intensity = 1) {
        this.intensity = intensity;
      }
    },
    DirectionalLight: class {
      position = new Vector3();
      intensity: number;
      constructor(_color?: number, intensity = 1) {
        this.intensity = intensity;
      }
    },
  };
});

// Theme comes from next-themes; drive it per-test via this mutable holder,
// mirroring the mock pattern in MonacoCodeEditor.test.tsx.
const themeState: { resolvedTheme: string } = { resolvedTheme: "light" };
vi.mock("next-themes", () => ({ useTheme: () => themeState }));

import { modelViewerTheme } from "./codeViewerHelpers";
import { ModelViewer } from "./ModelViewer";

// ── Helpers ───────────────────────────────────────────────────────────────────

function makeData(overrides: Partial<FileContentResponse> = {}): FileContentResponse {
  return {
    object: "session.environment.filesystem.file_content",
    path: "part.stl",
    content_type: null,
    encoding: "base64",
    content: "AAAA",
    bytes: 4,
    truncated: false,
    ...overrides,
  };
}

// Deterministic RAF: return an id and DON'T recurse, so the render loop runs
// its body exactly once instead of spinning.
beforeEach(() => {
  behavior.mode = "valid";
  behavior.orbitThrows = false;
  behavior.texturedMaterial = false;
  materialTextures.map = makeTextureRecord();
  materialTextures.normalMap = makeTextureRecord();
  parseCalls.length = 0;
  lastRenderer = null;
  lastMaterial = null;
  themeState.resolvedTheme = "light";
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn(() => 1),
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

// ── Tests ─────────────────────────────────────────────────────────────────────

describe("ModelViewer loader selection (unified with dispatch)", () => {
  it("selects the loader by MIME when the extension is unknown", async () => {
    // A file with no recognizable extension but an OBJ content type must parse
    // through the OBJ loader — proving detection and parsing use one resolver.
    render(
      <ModelViewer data={makeData({ path: "blob", content_type: "model/obj" })} path="blob" />,
    );
    await waitFor(() => expect(parseCalls).toContain("obj"));
    expect(parseCalls).not.toContain("stl");
    expect(screen.queryByText(/Unable to render/)).toBeNull();
  });

  it("selects the 3MF loader by MIME when the extension is absent", async () => {
    // A file with no recognizable extension but a 3MF content type must parse
    // through the 3MF loader — proving detection and parsing use one resolver.
    render(
      <ModelViewer
        data={makeData({ path: "download", content_type: "model/3mf" })}
        path="download"
      />,
    );
    await waitFor(() => expect(parseCalls).toContain("3mf"));
    expect(parseCalls).not.toContain("stl");
    expect(screen.queryByText(/Unable to render/)).toBeNull();
  });

  it("selects the STL loader for a binary .stl (extension fallback)", async () => {
    render(
      <ModelViewer
        data={makeData({ path: "widget.stl", content_type: "application/octet-stream" })}
        path="widget.stl"
      />,
    );
    await waitFor(() => expect(parseCalls).toContain("stl"));
  });
});

describe("ModelViewer error states", () => {
  it("shows the error overlay for a malformed model (parse throws)", async () => {
    behavior.mode = "throw";
    render(<ModelViewer data={makeData()} path="part.stl" />);
    expect(await screen.findByText(/Unable to render 3D model/)).toBeDefined();
  });

  it("shows the error overlay for an empty/degenerate model (blank scene guard)", async () => {
    // OBJLoader can return an empty group for comment-only input; the
    // bounding-box guard must route that to the error UI, not a blank canvas.
    behavior.mode = "empty";
    render(<ModelViewer data={makeData({ path: "empty.obj" })} path="empty.obj" />);
    expect(await screen.findByText(/Unable to render 3D model/)).toBeDefined();
  });

  it("shows the error overlay for a model with non-finite bounds", async () => {
    behavior.mode = "nan";
    render(<ModelViewer data={makeData({ path: "nan.obj" })} path="nan.obj" />);
    expect(await screen.findByText(/Unable to render 3D model/)).toBeDefined();
  });

  it("shows the truncated message without attempting to parse", async () => {
    render(<ModelViewer data={makeData({ truncated: true })} path="part.stl" />);
    expect(await screen.findByText(/too large to preview/)).toBeDefined();
    expect(parseCalls).toHaveLength(0);
  });
});

describe("ModelViewer error recovery (container stays mounted)", () => {
  it("recovers when props change from invalid to valid", async () => {
    // Start malformed → error overlay shown.
    behavior.mode = "throw";
    const { rerender } = render(<ModelViewer data={makeData()} path="bad.stl" />);
    expect(await screen.findByText(/Unable to render 3D model/)).toBeDefined();

    // A later valid model must render — only possible if the canvas container
    // (and its ref) stayed mounted under the overlay through the error state.
    behavior.mode = "valid";
    rerender(<ModelViewer data={makeData({ path: "good.stl" })} path="good.stl" />);
    await waitFor(() => expect(parseCalls).toContain("stl"));
    await waitFor(() => expect(screen.queryByText(/Unable to render 3D model/)).toBeNull());
    expect(lastRenderer).not.toBeNull();
  });
});

describe("ModelViewer teardown", () => {
  it("releases the renderer/WebGL context on a post-init failure (no leak)", async () => {
    // Renderer is created, then OrbitControls throws — the failure path must
    // tear down the already-created renderer (dispose + forceContextLoss)
    // rather than leaking the context until unmount.
    behavior.orbitThrows = true;
    render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    await waitFor(() => expect(lastRenderer?.contextLost).toBe(true));
    expect(lastRenderer?.disposed).toBe(true);
    // And the user sees the error UI.
    expect(await screen.findByText(/Unable to render 3D model/)).toBeDefined();
  });

  it("releases the renderer/WebGL context on unmount", async () => {
    const { unmount } = render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    expect(lastRenderer?.contextLost).toBe(false);
    unmount();
    expect(lastRenderer?.contextLost).toBe(true);
    expect(lastRenderer?.disposed).toBe(true);
  });

  it("disposes the material's textures on unmount (no GPU texture leak)", async () => {
    // A textured 3MF's material references textures (map, normalMap). Disposing
    // only the material would leak those GPU allocations, so teardown must free
    // each texture the material holds.
    behavior.texturedMaterial = true;
    const { unmount } = render(
      <ModelViewer data={makeData({ path: "part.3mf" })} path="part.3mf" />,
    );
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    expect(materialTextures.map.disposed).toBe(false);
    unmount();
    expect(materialTextures.map.disposed).toBe(true);
    expect(materialTextures.normalMap.disposed).toBe(true);
  });
});

describe("ModelViewer theme awareness", () => {
  const light = modelViewerTheme("light");
  const dark = modelViewerTheme("dark");

  it("builds the scene from the active light theme", async () => {
    themeState.resolvedTheme = "light";
    render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    // Canvas clear color and STL material track the light theme.
    expect(lastRenderer?.clearColor).toBe(light.background);
    expect(lastMaterial?.color).toBe(light.stlMaterial);
  });

  it("builds the scene from the active dark theme", async () => {
    themeState.resolvedTheme = "dark";
    render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    expect(lastRenderer?.clearColor).toBe(dark.background);
    expect(lastMaterial?.color).toBe(dark.stlMaterial);
    // Dark theme brightens the lights so the mesh stays legible.
    expect(dark.background).not.toBe(light.background);
    expect(dark.ambientIntensity).toBeGreaterThan(light.ambientIntensity);
    expect(dark.keyIntensity).toBeGreaterThan(light.keyIntensity);
  });

  it("updates the live scene when the theme toggles (no reload)", async () => {
    themeState.resolvedTheme = "light";
    // Reuse ONE data object across renders: the build effect keys on
    // [data, path], so a fresh object would rebuild and mask the in-place path.
    const stableData = makeData();
    const { rerender } = render(<ModelViewer data={stableData} path="part.stl" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    expect(lastRenderer?.clearColor).toBe(light.background);
    const rendererBefore = lastRenderer;
    const parsesBefore = parseCalls.length;

    // Toggle to dark and re-render with the SAME data/path — the scene must
    // recolor in place rather than rebuild (same renderer, no extra parse).
    themeState.resolvedTheme = "dark";
    rerender(<ModelViewer data={stableData} path="part.stl" />);
    await waitFor(() => expect(lastRenderer?.clearColor).toBe(dark.background));
    expect(lastMaterial?.color).toBe(dark.stlMaterial);
    expect(lastRenderer).toBe(rendererBefore);
    expect(parseCalls.length).toBe(parsesBefore);
  });
});
