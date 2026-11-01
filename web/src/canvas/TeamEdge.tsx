// A straight line between two cards, from border to border, so it never loops
// behind a card however the cards are arranged.

import { BaseEdge, useInternalNode, type EdgeProps, type InternalNode } from "@xyflow/react";

interface Point {
  x: number;
  y: number;
}

interface Box extends Point {
  width: number;
  height: number;
}

/** Where the line from a box's center towards a point leaves the box. */
export function boxBorderPoint(box: Box, toward: Point): Point {
  const centerX = box.x + box.width / 2;
  const centerY = box.y + box.height / 2;
  const deltaX = toward.x - centerX;
  const deltaY = toward.y - centerY;
  if (deltaX === 0 && deltaY === 0) return { x: centerX, y: centerY };
  const scale = Math.min(
    deltaX === 0 ? Infinity : box.width / 2 / Math.abs(deltaX),
    deltaY === 0 ? Infinity : box.height / 2 / Math.abs(deltaY),
  );
  return { x: centerX + deltaX * scale, y: centerY + deltaY * scale };
}

function nodeBox(node: InternalNode): Box {
  return {
    ...node.internals.positionAbsolute,
    width: node.measured.width ?? node.width ?? 0,
    height: node.measured.height ?? node.height ?? 0,
  };
}

function boxCenter(box: Box): Point {
  return { x: box.x + box.width / 2, y: box.y + box.height / 2 };
}

export function TeamEdge({ id, source, target }: EdgeProps) {
  const sourceNode = useInternalNode(source);
  const targetNode = useInternalNode(target);
  if (!sourceNode || !targetNode) return null;
  const sourceBox = nodeBox(sourceNode);
  const targetBox = nodeBox(targetNode);
  const start = boxBorderPoint(sourceBox, boxCenter(targetBox));
  const end = boxBorderPoint(targetBox, boxCenter(sourceBox));
  return (
    <BaseEdge
      id={id}
      path={`M ${start.x} ${start.y} L ${end.x} ${end.y}`}
      style={{ stroke: "var(--brand-accent)", strokeWidth: 1.5 }}
    />
  );
}
