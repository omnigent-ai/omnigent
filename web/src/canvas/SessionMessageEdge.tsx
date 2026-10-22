import { BaseEdge, getBezierPath, type EdgeProps } from "@xyflow/react";
import { CARD_HEIGHT } from "./canvasLayout";

export function sessionMessagePath(
  props: Pick<
    EdgeProps,
    "sourceX" | "sourceY" | "targetX" | "targetY" | "sourcePosition" | "targetPosition"
  >,
): [string, number, number] {
  const { sourceX, sourceY, targetX, targetY } = props;
  if (targetX > sourceX) {
    const [path, labelX, labelY] = getBezierPath(props);
    return [path, labelX, labelY - 18];
  }
  // Return directions pass above the cards instead of crossing beneath them.
  const top = Math.min(sourceY, targetY) - CARD_HEIGHT / 2 - 56;
  const bend = 48;
  return [
    `M ${sourceX},${sourceY} C ${sourceX + bend},${sourceY} ${sourceX + bend},${top} ${sourceX},${top} L ${targetX},${top} C ${targetX - bend},${top} ${targetX - bend},${targetY} ${targetX},${targetY}`,
    (sourceX + targetX) / 2,
    top,
  ];
}

export function SessionMessageEdge(props: EdgeProps) {
  const [path, labelX, labelY] = sessionMessagePath(props);
  return <BaseEdge {...props} path={path} labelX={labelX} labelY={labelY} />;
}

export const sessionMessageEdgeTypes = { sessionMessage: SessionMessageEdge };
