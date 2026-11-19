# Claude Video Editor — Engines

Root: `D:\Claude_Video_Editor`
User folders (untouched by setup): Brand Kit, Exports, Raw Footage, References, Storyboards.

| Engine | Location / command | Notes |
|---|---|---|
| FFmpeg 9.0.2 (Gyan full build) | `ffmpeg`, `ffprobe` (winget Links on PATH) | GPU encode: `-c:v h264_nvenc -preset p5 -cq 23` |
| whisper.cpp b5454 (CUDA 11.8) | `_engines\whisper\cuda\Release\whisper-cli.exe` | Model `_engines\whisper\models\ggml-large-v3-turbo-q5_0.bin`. cuBLAS 11 DLLs copied in from NVIDIA wheel. CPU fallback: `_engines\whisper\cpu\Release\whisper-cli.exe` |
| yt-dlp 2026.10.26 + Deno | `yt-dlp` | Section grab: `--download-sections "*10-15" --force-keyframes-at-cuts` |
| Remotion 4.0.545 | `_engines\remotion` | `npx remotion render src/index.ts KineticCaption out.mp4 --concurrency=2` |
| HyperFrames 0.9.66 | `_engines\hyperframes\card-test` | `npx hyperframes render -o out.mp4 -w 1` (1 worker: low RAM). Never `publish`/`share`/`cloud` (uploads to HeyGen) |

## Whisper word-level transcription
Input must be 16 kHz mono WAV:
```
ffmpeg -i in.mp4 -vn -ac 1 -ar 16000 audio.wav
whisper-cli.exe -m <model> -f audio.wav -l auto -ml 1 -sow -osrt -ojf -of out
```
`-l en` / `-l hi` to force language. `out.json` holds per-word `timestamps.from/to`.

## Machine limits
- 7.3 GB RAM (often ~1-2 GB free): one heavy render at a time; Remotion concurrency 2; HyperFrames 1 worker.
- GTX 1650 4 GB: whisper large-v3-turbo fits; first GPU run after reboot is slow (one-time CUDA JIT).

## Test results (2026-11-19)
| Engine | Test | Result |
|---|---|---|
| FFmpeg | slice 2-5s, silence detect + jump cut (10s→8s), audio extract, NVENC | PASS |
| Whisper | TTS sentence, 17/17 words exact, word timestamps; GPU 5.2s vs CPU 23s | PASS |
| yt-dlp | 5s section of Big Buck Bunny (CC-BY) | PASS |
| Remotion | 3s 1080p kinetic captions | PASS |
| HyperFrames | 3s 1080p lower-third + counter card | PASS |

Test outputs: `_engines\tests\`.
