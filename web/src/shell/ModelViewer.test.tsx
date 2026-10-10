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
  // If set, the TrackballControls constructor throws AFTER the renderer is
  // created, exercising the partial-init failure/teardown path.
  trackballThrows: boolean;
  // If set, the parsed object carries a mesh whose material references textures
  // (as a textured 3MF does), so teardown's texture disposal can be asserted.
  texturedMaterial: boolean;
}

const behavior: LoaderBehavior = {
  mode: "valid",
  trackballThrows: false,
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

// The parsed root, its rotation when its bounds were measured, and how many
// times that rotation was written: print formats must be stood up exactly once,
// before the bounds that drive camera fitting.
let lastParsedObject: { rotation: { x: number } } | null = null;
let rotationAtBounds: number | null = null;
let rotationWrites = 0;

function makeRotation() {
  let x = 0;
  return Object.defineProperty({}, "x", {
    get: () => x,
    set: (value: number) => {
      x = value;
      rotationWrites += 1;
    },
  }) as { x: number };
}

// Scene-graph records for the controls and headlight tests.
interface ControlsRecord {
  kind: "orbit" | "trackball";
  instance: { rotateSpeed: number };
  camera: unknown;
  cameraPositionOnConstruction: { x: number; y: number; z: number };
  handleResizeCalls: number;
  updateCalls: number;
  disposeCalls: number;
}
let lastControls: ControlsRecord | null = null;
let lastCamera: {
  children: unknown[];
  position: { x: number; y: number; z: number };
} | null = null;
let lastScene: { children: unknown[] } | null = null;
let lastKeyTarget: { parent: unknown; position: { x: number; y: number; z: number } } | null = null;
let resizeControls: (() => void) | null = null;

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
    rotation: makeRotation(),
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

// Both controls modules are stubbed so a test can tell which one the viewer
// built; the record also captures the camera position at construction, since
// TrackballControls snapshot it for reset().
function controlsStub(kind: "orbit" | "trackball") {
  return class {
    rotateSpeed = 1;
    constructor(camera: unknown) {
      if (kind === "trackball" && behavior.trackballThrows) {
        throw new Error("controls init failed");
      }
      const position = (camera as { position: { x: number; y: number; z: number } }).position;
      lastControls = {
        kind,
        instance: this,
        camera,
        cameraPositionOnConstruction: { x: position.x, y: position.y, z: position.z },
        handleResizeCalls: 0,
        updateCalls: 0,
        disposeCalls: 0,
      };
    }
    handleResize() {
      if (lastControls) lastControls.handleResizeCalls += 1;
    }
    update() {
      if (lastControls) lastControls.updateCalls += 1;
    }
    dispose() {
      if (lastControls) lastControls.disposeCalls += 1;
    }
  };
}

vi.mock("three/examples/jsm/controls/OrbitControls.js", () => ({
  OrbitControls: controlsStub("orbit"),
}));
vi.mock("three/examples/jsm/controls/TrackballControls.js", () => ({
  TrackballControls: controlsStub("trackball"),
}));

vi.mock("three", () => {
  class Vector3 {
    x = 0;
    y = 0;
    z = 0;
    set(x: number, y: number, z: number) {
      this.x = x;
      this.y = y;
      this.z = z;
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
    setFromObject(obj: { boxSpec?: { empty: boolean; nan: boolean }; rotation?: { x: number } }) {
      lastParsedObject = obj as { rotation: { x: number } };
      rotationAtBounds = obj.rotation?.x ?? 0;
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
    isPerspectiveCamera = true;
    fov = 45;
    aspect = 1;
    near = 0.1;
    far = 1000;
    position = new Vector3();
    children: unknown[] = [];
    constructor() {
      lastCamera = { children: this.children, position: this.position };
    }
    add(...objects: { parent?: unknown }[]) {
      for (const object of objects) {
        object.parent = this;
        this.children.push(object);
      }
    }
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
    children: unknown[] = [];
    constructor() {
      lastScene = { children: this.children };
    }
    add(...objects: unknown[]) {
      this.children.push(...objects);
    }
    remove() {}
  }
  class Mesh {
    geometry = null;
    material = null;
    position = new Vector3();
    rotation = makeRotation();
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
      isDirectionalLight = true;
      position = new Vector3();
      intensity: number;
      parent: unknown = null;
      target = { parent: null as unknown, position: new Vector3() };
      constructor(_color?: number, intensity = 1) {
        this.intensity = intensity;
        lastKeyTarget = this.target;
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
  behavior.trackballThrows = false;
  behavior.texturedMaterial = false;
  materialTextures.map = makeTextureRecord();
  materialTextures.normalMap = makeTextureRecord();
  parseCalls.length = 0;
  lastRenderer = null;
  lastMaterial = null;
  lastParsedObject = null;
  rotationAtBounds = null;
  rotationWrites = 0;
  lastControls = null;
  lastCamera = null;
  lastScene = null;
  lastKeyTarget = null;
  resizeControls = null;
  themeState.resolvedTheme = "light";
  vi.stubGlobal(
    "requestAnimationFrame",
    vi.fn(() => 1),
  );
  vi.stubGlobal("cancelAnimationFrame", vi.fn());
  vi.stubGlobal(
    "ResizeObserver",
    class {
      constructor(callback: ResizeObserverCallback) {
        resizeControls = () => callback([], this as unknown as ResizeObserver);
      }
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

describe("ModelViewer print orientation", () => {
  // STL and 3MF loaders return the slicer's Z-up frame; the viewer stands the
  // part up once, before measuring the bounds that frame the camera.
  it("rotates STL once before computing bounds", async () => {
    render(<ModelViewer data={makeData({ path: "part.stl" })} path="part.stl" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    expect(parseCalls).toEqual(["stl"]);
    expect(lastParsedObject?.rotation.x).toBe(-Math.PI / 2);
    expect(rotationAtBounds).toBe(-Math.PI / 2);
    expect(rotationWrites).toBe(1);
  });

  it("rotates 3MF once before computing bounds", async () => {
    render(<ModelViewer data={makeData({ path: "part.3mf" })} path="part.3mf" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    expect(parseCalls).toEqual(["3mf"]);
    expect(lastParsedObject?.rotation.x).toBe(-Math.PI / 2);
    expect(rotationAtBounds).toBe(-Math.PI / 2);
    expect(rotationWrites).toBe(1);
  });

  it("leaves OBJ, which is already Y-up, unrotated", async () => {
    render(<ModelViewer data={makeData({ path: "mesh.obj" })} path="mesh.obj" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    expect(parseCalls).toEqual(["obj"]);
    expect(lastParsedObject?.rotation.x).toBe(0);
    expect(rotationAtBounds).toBe(0);
    expect(rotationWrites).toBe(0);
  });
});

describe("ModelViewer trackball controls and headlight", () => {
  it("fits the camera before building trackball controls and resizes them with the pane", async () => {
    render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastControls).not.toBeNull());
    const controls = lastControls;
    if (!controls) throw new Error("TrackballControls were not initialized");

    expect(controls.kind).toBe("trackball");
    expect((controls.camera as { children: unknown[] }).children).toBe(lastCamera?.children);
    // The controls snapshot the camera for reset(), so it must already be at
    // its fitted position when they are built.
    expect(controls.cameraPositionOnConstruction).toEqual(lastCamera?.position);
    expect(controls.cameraPositionOnConstruction).not.toEqual({ x: 0, y: 0, z: 0 });
    expect(controls.updateCalls).toBeGreaterThanOrEqual(1);

    resizeControls?.();
    expect(controls.handleResizeCalls).toBe(1);
  });

  it("turns the model about once per canvas width of drag", async () => {
    render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastControls).not.toBeNull());
    // TrackballControls rotate rotateSpeed radians per half canvas width.
    expect(lastControls?.instance.rotateSpeed).toBe(Math.PI);
  });

  it("parents the key light and its target to the camera, which joins the scene", async () => {
    render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastRenderer).not.toBeNull());
    const camera = lastScene?.children.find(
      (child) => (child as { isPerspectiveCamera?: boolean }).isPerspectiveCamera,
    );
    expect(camera).toBeDefined();
    const carried = lastCamera?.children ?? [];
    expect(
      carried.some((child) => (child as { isDirectionalLight?: boolean }).isDirectionalLight),
    ).toBe(true);
    expect(carried).toContain(lastKeyTarget);
    expect(lastKeyTarget?.parent).toBe(camera);
    // Aimed straight down the view axis, so camera-facing surfaces stay lit.
    expect(lastKeyTarget?.position).toMatchObject({ x: 0, y: 0, z: -1 });
  });

  it("disposes the controls on unmount", async () => {
    const { unmount } = render(<ModelViewer data={makeData()} path="part.stl" />);
    await waitFor(() => expect(lastControls).not.toBeNull());
    unmount();
    expect(lastControls?.disposeCalls).toBe(1);
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
    // Renderer is created, then TrackballControls throws — the failure path
    // must tear down the already-created renderer (dispose + forceContextLoss)
    // rather than leaking the context until unmount.
    behavior.trackballThrows = true;
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
