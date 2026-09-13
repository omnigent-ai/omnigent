// Interactive 3D preview for STL / 3MF / OBJ files.
//
// Mirrors the ImageViewer pattern in CodeViewer: it takes the already-fetched
// FileContentResponse, decodes it via the shared `fileContentToBlob`, and
// renders it. three.js is imported at this module's top level so the lazy
// dynamic import in CodeViewer keeps the whole 3D stack out of the main bundle
// (same strategy as Monaco).
//
// The whole three.js scene is torn down on unmount (and on any init failure) —
// geometry, materials, renderer, the animation frame, and the WebGL context are
// all released so repeatedly opening model files can't leak GPU memory.

import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { STLLoader } from "three/examples/jsm/loaders/STLLoader.js";
import { ThreeMFLoader } from "three/examples/jsm/loaders/3MFLoader.js";
import { OBJLoader } from "three/examples/jsm/loaders/OBJLoader.js";
import { useResolvedThemeMode } from "@/components/theme/useResolvedThemeMode";
import { type FileContentResponse, fileContentToBlob } from "@/hooks/useFileContent";
import {
  type ModelFormat,
  type ModelViewerTheme,
  getModelFormat,
  modelViewerTheme,
} from "./codeViewerHelpers";
import { TruncatedBanner } from "./TruncatedBanner";

// What parseModel produces: the object to add to the scene, plus a handle to
// the STL default material when we created one. The material handle lets the
// theme-update effect recolor STL surfaces live; 3MF/OBJ carry their own
// materials, so we never touch those.
interface ParsedModel {
  object: THREE.Object3D;
  stlMaterial: THREE.MeshStandardMaterial | null;
}

/**
 * Parse raw model bytes into a three.js Object3D for the given format.
 *
 * The `format` comes from the shared `getModelFormat` resolver — the same one
 * that decides dispatch — so the parser only ever sees a format the viewer was
 * routed for. STL and 3MF are parsed from an ArrayBuffer; OBJ is text. Each
 * loader's `parse` is synchronous and self-contained (no network), so a
 * truncated or malformed file throws here and the caller surfaces the error
 * state.
 *
 * STL carries no material, so we give it a theme-derived surface (`theme`);
 * the returned `stlMaterial` handle lets the caller recolor it on a theme
 * toggle without reparsing.
 */
function parseModel(
  format: ModelFormat,
  buffer: ArrayBuffer,
  theme: ModelViewerTheme,
): ParsedModel {
  if (format === "stl") {
    const geometry = new STLLoader().parse(buffer);
    const material = new THREE.MeshStandardMaterial({
      color: theme.stlMaterial,
      metalness: 0.1,
      roughness: 0.6,
    });
    return { object: new THREE.Mesh(geometry, material), stlMaterial: material };
  }
  if (format === "3mf") {
    return { object: new ThreeMFLoader().parse(buffer), stlMaterial: null };
  }
  // OBJ is ASCII text.
  const text = new TextDecoder().decode(buffer);
  return { object: new OBJLoader().parse(text), stlMaterial: null };
}

/**
 * Frame `object` in `camera`: center it at the origin and pull the camera back
 * far enough that the whole bounding sphere is visible, then aim OrbitControls
 * at the center.
 *
 * The caller has already validated that the object's bounding box is non-empty
 * and finite, so `box` here is always usable.
 */
function fitToObject(
  object: THREE.Object3D,
  box: THREE.Box3,
  camera: THREE.PerspectiveCamera,
  controls: OrbitControls,
): void {
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());

  // Recenter the model on the origin so orbiting rotates around its middle.
  object.position.sub(center);

  const maxDim = Math.max(size.x, size.y, size.z) || 1;
  const fov = (camera.fov * Math.PI) / 180;
  // Distance so the bounding sphere fits the vertical FOV, with a little margin.
  const distance = (maxDim / 2 / Math.tan(fov / 2)) * 1.6;

  camera.position.set(distance, distance * 0.6, distance);
  camera.near = distance / 100;
  camera.far = distance * 100;
  camera.updateProjectionMatrix();

  controls.target.set(0, 0, 0);
  controls.update();
}

/**
 * Return `true` when `object` has a real, drawable shape — a non-empty,
 * finite bounding box. OBJLoader (and the others) can return an empty group for
 * empty or comment-only input, which would otherwise mount a blank canvas
 * instead of the error UI.
 */
function hasRenderableBounds(box: THREE.Box3): boolean {
  if (box.isEmpty()) return false;
  const { min, max } = box;
  return (
    Number.isFinite(min.x) &&
    Number.isFinite(min.y) &&
    Number.isFinite(min.z) &&
    Number.isFinite(max.x) &&
    Number.isFinite(max.y) &&
    Number.isFinite(max.z)
  );
}

/**
 * Dispose a material and every texture it references (`map`, `normalMap`,
 * `roughnessMap`, …) so their GPU allocations are released. three.js frees
 * neither the material nor its textures automatically, so a textured 3MF would
 * otherwise leak its GPU textures on unmount. Texture slots are detected by the
 * three.js `isTexture` flag rather than `instanceof` (the loaders and the app
 * can hold different three copies).
 */
function disposeMaterial(material: THREE.Material): void {
  for (const value of Object.values(material)) {
    const texture = value as THREE.Texture | null;
    if (texture?.isTexture) texture.dispose();
  }
  material.dispose();
}

/**
 * Dispose all geometries and materials reachable from `object` so the GPU
 * buffers backing them are released. three.js does not do this automatically.
 */
function disposeObject(object: THREE.Object3D): void {
  object.traverse((child) => {
    const mesh = child as THREE.Mesh;
    mesh.geometry?.dispose();
    const material = mesh.material;
    if (Array.isArray(material)) {
      material.forEach(disposeMaterial);
    } else if (material) {
      disposeMaterial(material);
    }
  });
}

// Everything the effect creates that must be released. A single mutable bag so
// one idempotent teardown routine can clean up whether initialization finished,
// partially completed, or threw midway.
interface SceneResources {
  scene: THREE.Scene;
  renderer: THREE.WebGLRenderer | null;
  controls: OrbitControls | null;
  object: THREE.Object3D | null;
  rafId: number;
  resizeObserver: ResizeObserver | null;
  // Theme-driven handles, kept so a live theme toggle can recolor the scene
  // without reparsing the model. null until the scene is built.
  ambient: THREE.HemisphereLight | null;
  key: THREE.DirectionalLight | null;
  stlMaterial: THREE.MeshStandardMaterial | null;
}

/**
 * Push `theme` onto a built scene: canvas clear color, light intensities, and
 * the STL default material (3MF/OBJ keep their own materials). Safe to call on
 * a partially-built `res` — each field is guarded — so the theme effect can run
 * before or after the async scene build completes.
 */
function applyTheme(res: SceneResources, theme: ModelViewerTheme): void {
  res.renderer?.setClearColor(theme.background, 1);
  if (res.ambient) res.ambient.intensity = theme.ambientIntensity;
  if (res.key) res.key.intensity = theme.keyIntensity;
  res.stlMaterial?.color.setHex(theme.stlMaterial);
}

/**
 * Release every resource in `res`, in reverse order of creation. Idempotent and
 * safe to call more than once (each field is nulled/reset as it's freed), so it
 * can run from BOTH the init failure path and the effect cleanup without
 * double-disposing.
 */
function teardownScene(res: SceneResources): void {
  if (res.rafId) {
    cancelAnimationFrame(res.rafId);
    res.rafId = 0;
  }
  if (res.resizeObserver) {
    res.resizeObserver.disconnect();
    res.resizeObserver = null;
  }
  if (res.controls) {
    res.controls.dispose();
    res.controls = null;
  }
  if (res.object) {
    disposeObject(res.object);
    res.scene.remove(res.object);
    res.object = null;
  }
  if (res.renderer) {
    res.renderer.domElement.remove();
    res.renderer.dispose();
    // Force the WebGL context to be released rather than waiting on GC.
    res.renderer.forceContextLoss();
    res.renderer = null;
  }
}

export function ModelViewer({ data, path }: { data: FileContentResponse; path: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [errored, setErrored] = useState(false);

  // Theme comes from the app's shared next-themes source (same hook Monaco and
  // the terminal use). `mode` is a stable "light"|"dark" string, so the theme
  // effect below re-runs only on an actual toggle.
  const mode = useResolvedThemeMode();

  // The live scene, so the theme effect can recolor it without a rebuild, and
  // the latest theme, so the build effect can seed it without depending on the
  // theme (which would reparse the model on every toggle).
  const resRef = useRef<SceneResources | null>(null);
  const themeRef = useRef<ModelViewerTheme>(modelViewerTheme(mode));

  // A theme toggle while a model is open updates the live scene in place —
  // clear color, light intensities, and the STL material — with no reload.
  useEffect(() => {
    const theme = modelViewerTheme(mode);
    themeRef.current = theme;
    if (resRef.current) applyTheme(resRef.current, theme);
  }, [mode]);

  useEffect(() => {
    const container = containerRef.current;
    // The container is always mounted (even in the error state, the canvas host
    // sits under the overlay), so this ref is populated on every effect run —
    // which is what lets an invalid → valid prop change recover.
    if (!container) return;

    // A truncated model is a partial byte stream that won't parse into a valid
    // mesh — skip the scene and go straight to the error UI.
    if (data.truncated) {
      setErrored(true);
      return;
    }
    // Same resolver used for dispatch, so a MIME-matched file with an unknown
    // extension still selects the right loader here.
    const format = getModelFormat(path, data.content_type);
    if (!format) {
      setErrored(true);
      return;
    }

    setErrored(false);

    let disposed = false;
    const res: SceneResources = {
      scene: new THREE.Scene(),
      renderer: null,
      controls: null,
      object: null,
      rafId: 0,
      resizeObserver: null,
      ambient: null,
      key: null,
      stlMaterial: null,
    };
    resRef.current = res;
    // Read the current theme without re-subscribing: the theme effect keeps
    // this ref fresh, so a build triggered by a data/path change always seeds
    // the scene with the active mode.
    const theme = themeRef.current;
    const camera = new THREE.PerspectiveCamera(45, 1, 0.1, 1000);

    const fail = () => {
      // Free anything created before the failure, then show the error UI. The
      // cleanup return also calls teardownScene, which is idempotent.
      teardownScene(res);
      if (resRef.current === res) resRef.current = null;
      if (!disposed) setErrored(true);
    };

    // fileContentToBlob handles both base64 (binary STL/3MF) and utf-8 (ASCII
    // OBJ/STL); read it back as an ArrayBuffer for the loaders. This is async,
    // so guard every step against the effect having been cleaned up.
    fileContentToBlob(data)
      .arrayBuffer()
      .then((buffer) => {
        if (disposed) return;

        const { object, stlMaterial } = parseModel(format, buffer, theme);
        res.stlMaterial = stlMaterial;
        // Validate BEFORE building the scene: an empty/degenerate model (e.g. a
        // comment-only OBJ) has no finite bounds and would render a blank
        // canvas, so surface the error UI instead.
        const box = new THREE.Box3().setFromObject(object);
        if (!hasRenderableBounds(box)) {
          disposeObject(object);
          fail();
          return;
        }
        res.object = object;
        res.scene.add(object);

        // Lighting: a hemisphere fill plus a key directional light so surfaces
        // read with depth rather than as a flat silhouette. Intensities come
        // from the active theme (brighter in dark) and are kept on `res` so a
        // theme toggle can update them live.
        const ambient = new THREE.HemisphereLight(0xffffff, 0x444444, theme.ambientIntensity);
        res.ambient = ambient;
        res.scene.add(ambient);
        const key = new THREE.DirectionalLight(0xffffff, theme.keyIntensity);
        key.position.set(1, 1, 1);
        res.key = key;
        res.scene.add(key);

        const renderer = new THREE.WebGLRenderer({ antialias: true });
        res.renderer = renderer;
        // Canvas background follows the theme so it sits flush with the panel.
        renderer.setClearColor(theme.background, 1);
        renderer.setPixelRatio(window.devicePixelRatio);
        const rect = container.getBoundingClientRect();
        renderer.setSize(rect.width || 1, rect.height || 1);
        container.appendChild(renderer.domElement);

        const controls = new OrbitControls(camera, renderer.domElement);
        res.controls = controls;
        controls.enableDamping = true;

        fitToObject(object, box, camera, controls);

        const render = () => {
          res.rafId = requestAnimationFrame(render);
          controls.update();
          renderer.render(res.scene, camera);
        };
        render();

        const onResize = () => {
          const r = container.getBoundingClientRect();
          if (r.width === 0 || r.height === 0) return;
          camera.aspect = r.width / r.height;
          camera.updateProjectionMatrix();
          renderer.setSize(r.width, r.height);
        };
        res.resizeObserver = new ResizeObserver(onResize);
        res.resizeObserver.observe(container);
      })
      .catch(() => {
        fail();
      });

    return () => {
      disposed = true;
      teardownScene(res);
      if (resRef.current === res) resRef.current = null;
    };
  }, [data, path]);

  const filename = path.split("/").pop() ?? path;
  const errorMessage = data.truncated
    ? "Model is too large to preview (truncated by the server)."
    : "Unable to render 3D model.";

  const content = (
    // The container is ALWAYS mounted so its ref stays live — the error is an
    // overlay on top, never a replacement. This lets the effect recover on an
    // invalid → valid {data, path} transition (the ref would be null if we
    // swapped the element out).
    <div className="relative min-h-0 flex-1 overflow-hidden">
      <div
        ref={containerRef}
        aria-label={`3D preview of ${filename}`}
        className="absolute inset-0 cursor-grab active:cursor-grabbing"
      />
      {errored && (
        <div className="absolute inset-0 flex items-center justify-center bg-background/80 p-8 text-center text-muted-foreground text-ui">
          {errorMessage}
        </div>
      )}
    </div>
  );

  if (!data.truncated) return <div className="flex h-full flex-col">{content}</div>;
  return (
    <div className="flex h-full flex-col">
      <TruncatedBanner />
      {content}
    </div>
  );
}
