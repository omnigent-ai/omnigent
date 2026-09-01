import { useEffect, useRef, type CSSProperties } from "react";
import "./PixelBlast.css";

const vertexSource = `#version 300 es
in vec2 position;

void main() {
  gl_Position = vec4(position, 0.0, 1.0);
}`;

const fragmentSource = `#version 300 es
precision highp float;

uniform vec3 uColor;
uniform vec2 uResolution;
uniform float uTime;
uniform float uPixelSize;
uniform float uScale;
uniform float uDensity;

out vec4 fragColor;

float bayer2(vec2 value) {
  value = floor(value);
  return fract(value.x / 2.0 + value.y * value.y * 0.75);
}

float bayer4(vec2 value) {
  return bayer2(0.5 * value) * 0.25 + bayer2(value);
}

float bayer8(vec2 value) {
  return bayer4(0.5 * value) * 0.25 + bayer2(value);
}

float hash11(float value) {
  return fract(sin(value) * 43758.5453);
}

float valueNoise(vec3 point) {
  vec3 cell = floor(point);
  vec3 local = fract(point);
  vec3 blend = local * local * local * (local * (local * 6.0 - 15.0) + 10.0);
  vec3 stepX = vec3(1.0, 0.0, 0.0);
  vec3 stepY = vec3(0.0, 1.0, 0.0);
  vec3 stepZ = vec3(0.0, 0.0, 1.0);
  vec3 seed = vec3(1.0, 57.0, 113.0);

  float n000 = hash11(dot(cell, seed));
  float n100 = hash11(dot(cell + stepX, seed));
  float n010 = hash11(dot(cell + stepY, seed));
  float n110 = hash11(dot(cell + stepX + stepY, seed));
  float n001 = hash11(dot(cell + stepZ, seed));
  float n101 = hash11(dot(cell + stepX + stepZ, seed));
  float n011 = hash11(dot(cell + stepY + stepZ, seed));
  float n111 = hash11(dot(cell + stepX + stepY + stepZ, seed));

  float x00 = mix(n000, n100, blend.x);
  float x10 = mix(n010, n110, blend.x);
  float x01 = mix(n001, n101, blend.x);
  float x11 = mix(n011, n111, blend.x);
  float y0 = mix(x00, x10, blend.y);
  float y1 = mix(x01, x11, blend.y);
  return mix(y0, y1, blend.z) * 2.0 - 1.0;
}

float fbm(vec2 uv, float time) {
  vec3 point = vec3(uv * uScale, time);
  float frequency = 1.0;
  float total = 1.0;

  for (int octave = 0; octave < 5; octave++) {
    total += valueNoise(point * frequency);
    frequency *= 1.25;
  }

  return total * 0.5 + 0.5;
}

void main() {
  vec2 centered = gl_FragCoord.xy - uResolution * 0.5;
  float aspect = uResolution.x / uResolution.y;
  vec2 pixelId = floor(centered / uPixelSize);
  float cellSize = 8.0 * uPixelSize;
  vec2 cell = floor(centered / cellSize) * cellSize;
  vec2 uv = cell / uResolution * vec2(aspect, 1.0);

  float base = fbm(uv, uTime * 0.05);
  float feed = (base * 0.5 - 0.65) + (uDensity - 0.5) * 0.3;
  float dither = bayer8(centered / uPixelSize) - 0.5;
  float coverage = step(0.5, feed + dither);

  vec3 srgb = mix(
    uColor * 12.92,
    1.055 * pow(uColor, vec3(1.0 / 2.4)) - 0.055,
    step(vec3(0.0031308), uColor)
  );

  fragColor = vec4(srgb, coverage);
}`;

export interface PixelBlastProps {
  pixelSize?: number;
  color?: string;
  patternScale?: number;
  patternDensity?: number;
  speed?: number;
  className?: string;
  style?: CSSProperties;
}

function createShader(gl: WebGL2RenderingContext, type: number, source: string) {
  const shader = gl.createShader(type);
  if (!shader) throw new Error("Unable to create WebGL shader.");

  gl.shaderSource(shader, source);
  gl.compileShader(shader);

  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    const message = gl.getShaderInfoLog(shader) || "Unknown shader compilation error.";
    gl.deleteShader(shader);
    throw new Error(message);
  }

  return shader;
}

function createProgram(gl: WebGL2RenderingContext) {
  const vertexShader = createShader(gl, gl.VERTEX_SHADER, vertexSource);
  const fragmentShader = createShader(gl, gl.FRAGMENT_SHADER, fragmentSource);
  const program = gl.createProgram();
  if (!program) throw new Error("Unable to create WebGL program.");

  gl.attachShader(program, vertexShader);
  gl.attachShader(program, fragmentShader);
  gl.linkProgram(program);
  gl.deleteShader(vertexShader);
  gl.deleteShader(fragmentShader);

  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    const message = gl.getProgramInfoLog(program) || "Unknown program link error.";
    gl.deleteProgram(program);
    throw new Error(message);
  }

  return program;
}

function hexToLinearRgb(hex: string) {
  const normalized = hex.replace("#", "");
  const expanded =
    normalized.length === 3
      ? normalized
          .split("")
          .map((channel) => channel + channel)
          .join("")
      : normalized;
  const value = Number.parseInt(expanded, 16);
  const toLinear = (channel: number) => {
    const srgb = channel / 255;
    return srgb <= 0.04045 ? srgb / 12.92 : Math.pow((srgb + 0.055) / 1.055, 2.4);
  };

  return [
    toLinear((value >> 16) & 255),
    toLinear((value >> 8) & 255),
    toLinear(value & 255),
  ] as const;
}

export default function PixelBlast({
  pixelSize = 1.5,
  color = "#f9a8d4",
  patternScale = 3.5,
  patternDensity = 1.3,
  speed = 0.5,
  className = "",
  style,
}: PixelBlastProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;

    const gl = canvas.getContext("webgl2", {
      alpha: true,
      // Full-screen triangle has no in-view edges to antialias; MSAA only costs a resolve.
      antialias: false,
      premultipliedAlpha: true,
      powerPreference: "high-performance",
    });

    if (!gl) {
      canvas.dataset.webglUnavailable = "true";
      return;
    }

    let program: WebGLProgram;
    try {
      program = createProgram(gl);
    } catch (error) {
      console.error("PixelBlast WebGL2 initialization failed:", error);
      canvas.dataset.webglUnavailable = "true";
      return;
    }

    const vertexArray = gl.createVertexArray();
    const positionBuffer = gl.createBuffer();
    if (!vertexArray || !positionBuffer) {
      gl.deleteProgram(program);
      return;
    }

    gl.bindVertexArray(vertexArray);
    gl.bindBuffer(gl.ARRAY_BUFFER, positionBuffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);

    const positionLocation = gl.getAttribLocation(program, "position");
    gl.enableVertexAttribArray(positionLocation);
    gl.vertexAttribPointer(positionLocation, 2, gl.FLOAT, false, 0, 0);
    gl.useProgram(program);

    const resolution = gl.getUniformLocation(program, "uResolution");
    const time = gl.getUniformLocation(program, "uTime");
    const colorUniform = gl.getUniformLocation(program, "uColor");
    const pixelSizeUniform = gl.getUniformLocation(program, "uPixelSize");
    const scale = gl.getUniformLocation(program, "uScale");
    const density = gl.getUniformLocation(program, "uDensity");
    const [red, green, blue] = hexToLinearRgb(color);

    gl.uniform3f(colorUniform, red, green, blue);
    gl.uniform1f(scale, patternScale);
    gl.uniform1f(density, patternDensity);
    gl.clearColor(0, 0, 0, 0);
    gl.enable(gl.BLEND);
    gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA);

    let pixelRatio = 1;
    const resize = () => {
      const bounds = canvas.getBoundingClientRect();
      // Decorative dithered background — render at device resolution but cap at
      // 1x. DPR 2 quadruples fragment count for no visible gain on a soft pattern.
      pixelRatio = Math.min(window.devicePixelRatio || 1, 1);
      canvas.width = Math.max(1, Math.round(bounds.width * pixelRatio));
      canvas.height = Math.max(1, Math.round(bounds.height * pixelRatio));
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.useProgram(program);
      gl.uniform2f(resolution, canvas.width, canvas.height);
      gl.uniform1f(pixelSizeUniform, pixelSize * pixelRatio);
    };

    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(canvas);
    resize();

    const startedAt = performance.now();
    const timeOffset = Math.random() * 1000;
    let animationFrame = 0;

    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let onScreen = true;

    // Animate only while visible, foregrounded, and motion is allowed —
    // a full-screen shader otherwise burns GPU when scrolled away or backgrounded.
    const shouldAnimate = () => speed > 0 && onScreen && !document.hidden && !reducedMotion.matches;

    // 30fps is plenty for this soft background; skip frames to halve GPU work
    // on 60Hz+ displays while staying rAF-driven (vsync-aligned, pauses when hidden).
    const frameInterval = 1000 / 30;
    let lastFrame = -Infinity;

    const render = (now: number) => {
      gl.clear(gl.COLOR_BUFFER_BIT);
      gl.useProgram(program);
      gl.bindVertexArray(vertexArray);
      gl.uniform1f(time, timeOffset + ((now - startedAt) / 1000) * speed);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      lastFrame = now;
    };

    const draw = (now: number) => {
      if (now - lastFrame >= frameInterval) render(now);
      if (shouldAnimate()) animationFrame = requestAnimationFrame(draw);
    };

    const start = () => {
      if (animationFrame || !shouldAnimate()) return;
      animationFrame = requestAnimationFrame(draw);
    };
    const stop = () => {
      cancelAnimationFrame(animationFrame);
      animationFrame = 0;
    };
    const sync = () => (shouldAnimate() ? start() : (stop(), render(performance.now())));

    const intersectionObserver = new IntersectionObserver(([entry]) => {
      onScreen = entry.isIntersecting;
      sync();
    });
    intersectionObserver.observe(canvas);
    document.addEventListener("visibilitychange", sync);
    reducedMotion.addEventListener("change", sync);

    draw(performance.now());

    return () => {
      stop();
      intersectionObserver.disconnect();
      document.removeEventListener("visibilitychange", sync);
      reducedMotion.removeEventListener("change", sync);
      resizeObserver.disconnect();
      gl.deleteBuffer(positionBuffer);
      gl.deleteVertexArray(vertexArray);
      gl.deleteProgram(program);
      gl.getExtension("WEBGL_lose_context")?.loseContext();
    };
  }, [color, patternDensity, patternScale, pixelSize, speed]);

  return (
    <canvas
      ref={canvasRef}
      className={`pixel-blast-canvas ${className}`}
      style={style}
      aria-hidden="true"
    />
  );
}
