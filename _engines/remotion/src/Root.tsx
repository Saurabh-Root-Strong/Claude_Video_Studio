import React from "react";
import { Composition } from "remotion";
import { KineticCaption, KineticCaptionProps } from "./KineticCaption";

const defaults: KineticCaptionProps = {
  title: "ENGINE TEST",
  accent: "#f97316",
  words: [
    { text: "Claude", start: 0.3, end: 0.8 },
    { text: "video", start: 0.8, end: 1.3 },
    { text: "editor", start: 1.3, end: 1.9 },
    { text: "is", start: 1.9, end: 2.2 },
    { text: "ready", start: 2.2, end: 3.0 },
  ],
};

export const RemotionRoot: React.FC = () => (
  <>
    <Composition
      id="KineticCaption"
      component={KineticCaption}
      durationInFrames={90}
      fps={30}
      width={1920}
      height={1080}
      defaultProps={defaults}
    />
    <Composition
      id="KineticCaptionVertical"
      component={KineticCaption}
      durationInFrames={90}
      fps={30}
      width={1080}
      height={1920}
      defaultProps={defaults}
    />
  </>
);
