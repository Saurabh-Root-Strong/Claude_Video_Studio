import React from "react";
import {
  AbsoluteFill,
  interpolate,
  spring,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export type Word = { text: string; start: number; end: number }; // seconds

export type KineticCaptionProps = {
  title: string;
  words: Word[];
  accent: string;
};

export const KineticCaption: React.FC<KineticCaptionProps> = ({
  title,
  words,
  accent,
}) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const t = frame / fps;

  const titleIn = spring({ frame, fps, config: { damping: 14 } });

  return (
    <AbsoluteFill
      style={{
        background: "linear-gradient(135deg, #0f172a 0%, #1e293b 100%)",
        fontFamily: "Segoe UI, Arial, sans-serif",
        justifyContent: "center",
        alignItems: "center",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: 140,
          fontSize: 64,
          fontWeight: 800,
          color: "white",
          letterSpacing: 2,
          transform: `translateY(${interpolate(titleIn, [0, 1], [-60, 0])}px)`,
          opacity: titleIn,
        }}
      >
        {title}
      </div>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          justifyContent: "center",
          gap: 40,
          maxWidth: 1500,
          padding: "0 60px",
        }}
      >
        {words.map((w, i) => {
          const local = Math.round((t - w.start) * fps);
          const pop = spring({ frame: local, fps, config: { damping: 12 } });
          const active = t >= w.start && t < w.end;
          const shown = t >= w.start;
          return (
            <span
              key={i}
              style={{
                fontSize: 96,
                fontWeight: 900,
                color: active ? accent : "white",
                opacity: shown ? 1 : 0.15,
                transform: `scale(${shown ? interpolate(pop, [0, 1], [0.6, active ? 1.12 : 1]) : 0.9})`,
                textShadow: active ? `0 0 30px ${accent}` : "none",
              }}
            >
              {w.text}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
