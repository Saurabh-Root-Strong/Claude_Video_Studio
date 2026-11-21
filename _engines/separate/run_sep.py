"""Background music of a video: everything EXCEPT the voice (Demucs htdemucs, Meta, MIT licence).

    run_sep.py <ffmpeg> <input media> <output .m4a>

Reads / writes audio through ffmpeg (raw float32 pipes) - no torchaudio file I/O, which changes between versions.
Prints "LEVEL <music rms dB> <total rms dB>" so the studio can tell a reference that has almost no music.
"""
import subprocess
import sys

import numpy as np
import torch
from demucs.apply import apply_model
from demucs.pretrained import get_model


def main():
    ffmpeg, src, out = sys.argv[1], sys.argv[2], sys.argv[3]
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
    with torch.no_grad():
        stems = apply_model(model, ((wav - mean) / std)[None], device="cpu", shifts=1, split=True, overlap=0.25,
                            progress=False)[0]
    stems = stems * std + mean
    vi = model.sources.index("vocals")
    music = sum(stems[i] for i in range(len(model.sources)) if i != vi).numpy().astype(np.float32)  # (2, n)
    rms = lambda a: 20 * np.log10(float(np.sqrt(np.mean(a ** 2))) + 1e-9)  # noqa: E731
    print(f"LEVEL {rms(music):.1f} {rms(x):.1f}", flush=True)
    peak = float(np.max(np.abs(music))) or 1.0
    if peak > 0.99:
        music *= 0.99 / peak
    subprocess.run([ffmpeg, "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", "2", "-i", "-",
                    "-c:a", "aac", "-b:a", "192k", out], input=music.T.tobytes(), check=True)


if __name__ == "__main__":
    main()
