"""Cluster mined questions and draw a stratified sample with a fixed seed.

Input is JSON Lines with at least ``{"question", "pool_id"}`` per line.
Questions are embedded with a gateway embedding model and clustered with
spherical k-means. Each cluster contributes in proportion to its size (at
least ``--min-per-cluster``), drawn in random order; the order is kept so a
question with no code-derivable answer is replaced by the next one from the
same cluster. Within each cluster, picks alternate test, test, dev::

    uv run --no-sync python -m dev.evals.omnigent_guide.sample \\
        --input pool.jsonl --k 14 --total 60 --profile dev --out sample.jsonl

Pure Python, so the eval needs no numeric dependency.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from operator import mul
from pathlib import Path

Vector = list[float]


def embed(texts: list[str], model: str, profile: str | None) -> list[Vector]:
    from openai import OpenAI

    from tests.harness_bench.runtime_env import resolve_bench_env

    env = resolve_bench_env(profile).base_env
    client = OpenAI(base_url=env["OPENAI_BASE_URL"], api_key=env["OPENAI_API_KEY"])
    import time

    from openai import RateLimitError

    vectors: list[Vector] = []
    for start in range(0, len(texts), 16):
        for attempt in range(8):
            try:
                reply = client.embeddings.create(model=model, input=texts[start : start + 16])
                break
            except RateLimitError:
                # Pay-per-token endpoints cap requests per second.
                time.sleep(2**attempt)
        else:
            raise RuntimeError(f"embedding endpoint {model} kept rate-limiting")
        vectors.extend(_unit(item.embedding) for item in reply.data)
        time.sleep(1.0)
    return vectors


def _unit(v: Vector) -> Vector:
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norm for x in v]


def _dot(a: Vector, b: Vector) -> float:
    return sum(map(mul, a, b))


def kmeans(x: list[Vector], k: int, seed: int, iters: int = 50) -> list[int]:
    """Spherical k-means with k-means++ seeding; returns a label per row."""
    rng = random.Random(seed)
    centers = [x[rng.randrange(len(x))]]
    while len(centers) < k:
        dist = [1 - max(_dot(v, c) for c in centers) for v in x]
        total = sum(dist)
        pick, acc, r = 0, 0.0, rng.random() * total
        for i, d in enumerate(dist):
            acc += d
            if acc >= r:
                pick = i
                break
        centers.append(x[pick])
    labels = [-1] * len(x)
    for _ in range(iters):
        new = [max(range(k), key=lambda j: _dot(v, centers[j])) for v in x]
        if new == labels:
            break
        labels = new
        for j in range(k):
            members = [v for v, lab in zip(x, labels, strict=True) if lab == j]
            if members:
                centers[j] = _unit([sum(col) / len(members) for col in zip(*members, strict=True)])
    return labels


def allocate(sizes: dict[int, int], total: int, minimum: int) -> dict[int, int]:
    """Proportional allocation with a floor, adjusted to sum to *total*."""
    pool = sum(sizes.values())
    alloc = {c: min(n, max(minimum, round(total * n / pool))) for c, n in sizes.items()}
    while sum(alloc.values()) > total:
        biggest = max((c for c in alloc if alloc[c] > minimum), key=lambda c: alloc[c])
        alloc[biggest] -= 1
    while sum(alloc.values()) < total:
        room = [c for c in alloc if alloc[c] < sizes[c]]
        if not room:
            break
        alloc[max(room, key=lambda c: sizes[c] / (alloc[c] + 1))] += 1
    return alloc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--k", type=int, default=14)
    parser.add_argument("--total", type=int, default=60)
    parser.add_argument("--min-per-cluster", type=int, default=2)
    parser.add_argument("--seed", type=int, default=20261014)
    parser.add_argument("--model", default="databricks-bge-large-en")
    parser.add_argument("--profile", default=None)
    parser.add_argument("--vectors", type=Path, default=None, help="Cache file for embeddings.")
    args = parser.parse_args(argv)

    pool = [json.loads(line) for line in args.input.read_text().splitlines() if line.strip()]
    if args.vectors and args.vectors.exists():
        vectors = json.loads(args.vectors.read_text())
    else:
        vectors = embed([q["question"] for q in pool], args.model, args.profile)
        if args.vectors:
            args.vectors.write_text(json.dumps(vectors))
    labels = kmeans(vectors, args.k, args.seed)
    rng = random.Random(args.seed)
    clusters: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        clusters.setdefault(label, []).append(index)
    alloc = allocate({c: len(m) for c, m in clusters.items()}, args.total, args.min_per_cluster)
    with args.out.open("w") as handle:
        for label, members in sorted(clusters.items()):
            rng.shuffle(members)
            for rank, index in enumerate(members):
                item = dict(pool[index])
                item.update(
                    cluster_id=label, cluster_size=len(members), draw_rank=rank, quota=alloc[label]
                )
                # Splits follow the draw order: two test picks, then one dev pick.
                item["split"] = "dev" if rank % 3 == 2 else "test"
                handle.write(json.dumps(item) + "\n")
    print(
        json.dumps(
            {
                "pool": len(pool),
                "clusters": {c: len(m) for c, m in sorted(clusters.items())},
                "quota": alloc,
            },
            indent=1,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
