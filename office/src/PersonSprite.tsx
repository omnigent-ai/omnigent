import { useEffect, useRef, useState } from "react";
import { PERSON_HEIGHT, PERSON_WIDTH, type Palette, drawPerson } from "./sprites.ts";

const STEP_MS = 350;

interface PersonSpriteProps {
  palette: Palette;
  /** Size of one sprite pixel in CSS pixels. */
  pixel?: number;
  /** Step in place, as a working worker does in the office. */
  walking?: boolean;
}

/** One office character drawn the same way as on the room canvas. */
export function PersonSprite({ palette, pixel = 3, walking = false }: PersonSpriteProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const [frame, setFrame] = useState(0);

  useEffect(() => {
    if (!walking) {
      setFrame(0);
      return;
    }
    const timer = window.setInterval(() => setFrame((current) => current + 1), STEP_MS);
    return () => window.clearInterval(timer);
  }, [walking]);

  useEffect(() => {
    const context = canvasRef.current?.getContext("2d");
    if (!context) return;
    context.clearRect(0, 0, PERSON_WIDTH * pixel, PERSON_HEIGHT * pixel);
    drawPerson(context, 0, 0, pixel, palette, frame);
  }, [palette, pixel, frame]);

  return (
    <canvas
      ref={canvasRef}
      className="person-sprite"
      width={PERSON_WIDTH * pixel}
      height={PERSON_HEIGHT * pixel}
      aria-hidden="true"
    />
  );
}
