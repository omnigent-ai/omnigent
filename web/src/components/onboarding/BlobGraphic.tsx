// Animated blob background: floating gradient blobs on a slow elliptical orbit,
// under a grid overlay and a blurred veil. Pure CSS (see BlobGraphic.css) — no
// canvas or animation-lib dependency. Ported from the design prototype's
// `.animated-blob-graphic`.

import "./BlobGraphic.css";

// Per-blob placement/color/timing, straight from the prototype. Kept inline so
// the three blobs read as data rather than three near-identical CSS rules.
const BLOBS = [
  {
    color: "224, 68, 199", // pink
    gradientAt: "36% 32%",
    left: "34%",
    top: "68%",
    borderRadius: "58% 42% 67% 33% / 38% 58% 42% 62%",
    zIndex: 1,
    animation: "9.6s cubic-bezier(0.45, 0, 0.55, 1) -2.8s infinite blob-float-pink",
  },
  {
    color: "249, 115, 22", // orange
    gradientAt: "62% 38%",
    left: "50%",
    top: "18%",
    borderRadius: "41% 59% 35% 65% / 57% 38% 62% 43%",
    zIndex: 3,
    animation: "12.2s cubic-bezier(0.45, 0, 0.55, 1) -7.2s infinite blob-float-orange",
  },
  {
    color: "32, 158, 214", // blue
    gradientAt: "42% 64%",
    left: "66%",
    top: "68%",
    borderRadius: "64% 36% 48% 52% / 35% 61% 39% 65%",
    zIndex: 2,
    animation: "15.6s cubic-bezier(0.45, 0, 0.55, 1) -10.2s infinite blob-float-blue",
  },
];

export default function BlobGraphic() {
  return (
    <div className="blob-graphic" aria-hidden="true">
      <div className="blob-graphic__orbit">
        {BLOBS.map((blob) => (
          <span
            key={blob.color}
            className="blob-graphic__blob"
            style={{
              left: `calc(${blob.left} - 140px)`,
              top: `calc(${blob.top} - 140px)`,
              background: `radial-gradient(at ${blob.gradientAt}, rgba(${blob.color}, 0.28), rgba(${blob.color}, 0.1) 58%, rgba(${blob.color}, 0.04))`,
              borderRadius: blob.borderRadius,
              zIndex: blob.zIndex,
              animation: blob.animation,
            }}
          />
        ))}
      </div>
      <div className="blob-graphic__veil" />
      <div className="blob-graphic__grid" />
    </div>
  );
}
