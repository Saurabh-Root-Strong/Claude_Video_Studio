"""Split a video's sound into the VOICE and everything else (Demucs htdemucs, Meta, MIT licence).

    run_sep.py <ffmpeg> <input media> <output file> [music|vocals]

music (default) = everything EXCEPT the voice (a reference's background music); vocals = the voice alone.
The output format follows the file name (.m4a -> AAC, .wav -> 16-bit WAV).
Reads / writes audio through ffmpeg (raw float32 pipes) - no torchaudio file I/O, which changes between versions.
Prints "LEVEL <kept part rms dB> <total rms dB>" so the studio can tell when the part asked for is nearly silent
(music taken from a clip that is all talking, or the voice from a song without singing).
"""
import subprocess
import sys

import numpy as np
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model


def main():
    ffmpeg, src, out = sys.argv[1], sys.argv[2], sys.argv[3]
    want = sys.argv[4] if len(sys.argv) > 4 else "music"
    torch.set_num_threads(max(1, (torch.get_num_threads() or 2)))
    model = get_model("htdemucs")
    model.eval()
    sr = model.samplerate  # 44100
    raw = subprocess.run([ffmpeg, "-v", "error", "-i", src, "-vn", "-ac", "2", "-ar", str(sr), "-f", "f32le", "-"],
                         capture_output=True, check=True).stdout
    x = np.frombuffer(raw, dtype=np.float32).reshape(-1, 2).T.copy()  # (2, n)
    if x.shape[1] < sr // 2:
        raise SystemExit("too short")
    wav = torch.from_numpy(x)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    vi = model.sources.index("vocals")
    # in 2-minute pieces, each with 5 s of context either side (only its middle is kept): memory stays flat on a
    # 7 GB PC however long the clip is, and the joins are inaudible (the model saw across them)
    n = wav.shape[1]
    piece, ctx = 120 * sr, 5 * sr
    part = np.zeros((2, n), dtype=np.float32)
    norm = (wav - mean) / std
    for s in range(0, n, piece):
        e = min(n, s + piece)
        a, b = max(0, s - ctx), min(n, e + ctx)
        with torch.no_grad():
            st = apply_model(model, norm[:, a:b][None], device="cpu", shifts=1, split=True, overlap=0.25,
                             progress=False)[0] * std + mean
        keep = st[vi] if want == "vocals" else sum(st[i] for i in range(len(model.sources)) if i != vi)
        part[:, s:e] = keep[:, s - a:s - a + (e - s)].numpy().astype(np.float32)
        del st, keep
    rms = lambda a: 20 * np.log10(float(np.sqrt(np.mean(a ** 2))) + 1e-9)  # noqa: E731
    print(f"LEVEL {rms(part):.1f} {rms(x):.1f}", flush=True)
    peak = float(np.max(np.abs(part))) or 1.0
    if peak > 0.99:
        part *= 0.99 / peak
    codec = ["-c:a", "pcm_s16le"] if out.lower().endswith(".wav") else ["-c:a", "aac", "-b:a", "192k"]
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "-", *codec, out],
                   input=part.T.tobytes(), check=True)


if __name__ == "__main__":
    main()
