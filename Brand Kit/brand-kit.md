# Brand Kit — "Clean Split" editing profile

Status: **APPROVED 2026-11-19**, audio/pause numbers updated by Prompt 3 the same day · for: Hindi finance reels (9:16) · machine-readable twin: `brand-kit.json`
(it is a look called **★ Brand Kit**: new projects start on it; pick it in Scenes → Look for any project. Read-only from projects – change it here)

---

## 1. What the reference does (measured, not guessed)

Reference: `References/saurabh-first/India_mein_Stock_Market_Expo [K1tEM0fWc-o].mp4`. It is 55.8 s long, 608×1080 pixels, 30 fps (frames per second).
I measured it with 112 sampled frames, shot detection in each half of the screen, two transcripts, and audio level / spectrum analysis.

| Area | What it does |
|---|---|
| Layout | Split screen: the top ~46 % is B-roll (cutaway footage) that changes every ~2.9 s. The bottom shows an ultra-close selfie against a white wall. |
| Language | The speaker talks Hindi; the captions are an English translation. |
| Captions | White rounded pill with a thin black outline, placed on the seam between the halves. Lowercase, 1–4 words. The keyword is ~1.6× bigger (emphasis by size, not colour). Each caption fades in over ~150 ms. |
| Pauses | Very tight: only 3 gaps longer than 0.12 s in 56 s. Speech runs at ~170 wpm (words per minute). |
| Sound | Voice only. No music bed (quietest moments fall to −63 dBFS, decibels below full scale) and no whoosh/pop SFX (sound effects) at cuts. Loudness −18.1 LUFS (Loudness Units relative to Full Scale), loudness range 2.7 LU (heavily compressed). |
| Colour | Speaker half is bright and calm (average brightness 161 of 255). B-roll half is darker and more vivid (103). |

## 2. Your profile (approved decisions)

| Decision | Choice |
|---|---|
| Caption language | **Keep our captions as they are: Hinglish**, in the current style. White words with a black outline, the spoken word highlighted orange, 4 words. The font is now **Poppins**. Captions sit **on the seam**. |
| Background | Green screen is **replaced with warm off-white `#F4F1EC`** with a faint vignette. Detected automatically from the frame edges. |
| Font | **Poppins** (free, OFL license). Bundled in `_engines/studio/fonts/`, so every PC has it. Covers English and Hindi script. |
| Top panel | **Cards made by Claude**: title / number / chart on navy `#0F1B2D`, with keywords in orange. A card pops in when it changes, then pushes in slowly (4 %) so the panel never sits still. |
| Music | **Quiet background music.** It is set 12 LU under your voice and **ducks −18 dB under dialogue**, with a 1 s fade in and 1.5 s fade out. It uses your own track if one is in `Brand Kit/music/`; otherwise an original generated soft chord pad. |

Fixed parts of the look:
- **9:16, 1080×1920, 30 fps.** The top panel is 44 %, with a 4 px **orange `#F97316` seam** (your signature).
- **Speaker framing:** eyes at ~2/3 of the frame height. Adjust with `face up` / `face down`.
- **Pause removal:** pauses longer than **0.35 s** are shortened to ~0.24 s (natural breath). Language is set to Hindi for transcription.
- **Audio polish:** high-pass at 80 Hz, light de-noise and compression, then **−14 LUFS** (measured two-pass, then checked on the final mix and corrected) with peaks ≤ −1 dB.
- **Colour:** light brightness/contrast lift on the speaker.

## 3. How to use it

| Where | What to type |
|---|---|
| Scene box | `card: title bajaj *IPO* listing aaj` (`*word*` = orange) · `card: number 2x+ \| listing gain` · `card: chart 10,12,11,15 \| Nifty 5 days` · `card off` (empty panel) · `card: same` (keep the previous card) |
| Whole-video box | `layout split` / `layout full` · `headline: ...` (card for scenes without their own) · `music on` / `music off` · `audio polish` · `background off` / `background on` · `face up` / `face down` |
| Storyboard | Before rendering, Claude plans the edit beat by beat (cuts, punch-ins, cards); you approve/reject each beat at Scenes tab → **Storyboard**. |
| Claude inbox | "make the cards" → Claude writes one card per idea, using only facts from your speech or data you give. Charts only use real numbers. |

## 4. Not built yet
- B-roll video clips in the top panel. You chose cards, so only cards are supported for now.
- Retake removal is done by Claude's plan ("✨ Claude: plan the edit" on the Scenes page → storyboard you approve), not automatically.
- A count-up animation on number cards.
