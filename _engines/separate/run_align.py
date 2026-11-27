"""Forced alignment: WHEN each word of a known text is said (Meta MMS_FA, wav2vec2 trained on 1,100+ languages,
romanised text – Hinglish as typed works as is; CC-BY-NC 4.0 model weights, for personal use).

    run_align.py <ffmpeg> <input media> <job.json> <out.json>

job.json  {"blocks": [{"s": clip start s, "e": clip end s, "words": ["gold", "achanak", ...]}, ...]}
out.json  {"blocks": [{"times": [[s, e, score] | null, ...]}, ...]}   (clip seconds; null = word had no letters)

Each block is aligned on its own stretch of audio (a script line between two time stamps), with a "*" token at both
ends so speech that isn't in the text (a take, an extra word) doesn't drag the words. Audio is read through ffmpeg
(raw float32 pipe) - no torchaudio file I/O, which changes between versions.
"""
import json
import re
import subprocess
import sys

import numpy as np
import torch
import torchaudio
from torchaudio.pipelines import MMS_FA as BUNDLE

PAD = 0.35


def main():
    ffmpeg, src, job_p, out_p = sys.argv[1:5]
    job = json.load(open(job_p, encoding="utf-8"))
    torch.set_num_threads(max(1, torch.get_num_threads() or 2))
    model = BUNDLE.get_model(with_star=True)
    model.eval()
    dic = BUNDLE.get_dict(star="*")
    sr = int(BUNDLE.sample_rate)
    out = []
    for b in job["blocks"]:
        a0 = max(0.0, float(b["s"]) - PAD)
        dur = float(b["e"]) + PAD - a0
        raw = subprocess.run([ffmpeg, "-v", "error", "-ss", f"{a0:.3f}", "-t", f"{dur:.3f}", "-i", src, "-vn", "-ac", "1",
                              "-ar", str(sr), "-f", "f32le", "-"], capture_output=True, check=True).stdout
        wav = torch.from_numpy(np.frombuffer(raw, dtype=np.float32).copy())[None]
        # "*" = a free stretch (speech that isn't in the text, between two lines)
        keep = ["*" if w == "*" else re.sub(r"[^a-z']", "", w.lower()) for w in b["words"]]
        keep = ["".join(c for c in k if c in dic) for k in keep]
        idx = [i for i, k in enumerate(keep) if k]
        times = [None] * len(keep)
        if idx and wav.shape[1] > sr * 0.2:
            # emissions in 30 s windows with 1 s of context each side (attention memory grows with length²);
            # every window's own frames are kept, so the joined emission has the same frame clock
            win, ctx, n = 30 * sr, sr, wav.shape[1]
            parts = []
            with torch.inference_mode():
                for a in range(0, n, win):
                    lo, hi = max(0, a - ctx), min(n, a + win + ctx)
                    em_w, _ = model(wav[:, lo:hi])
                    fpr = em_w.shape[1] / (hi - lo)  # frames per sample
                    f0 = round((a - lo) * fpr)
                    f1 = f0 + round((min(n, a + win) - a) * fpr)
                    parts.append(em_w[0, f0:f1])
            em = torch.cat(parts)
            # CTC needs at least as many frames as tokens (+ repeats); a too-short stretch keeps the estimate
            toks = [dic["*"]] + [dic[c] for i in idx for c in keep[i]] + [dic["*"]]
            if em.shape[0] > len(toks) * 1.2:
                ali, scores = torchaudio.functional.forced_align(em[None], torch.tensor([toks], dtype=torch.int32), blank=0)
                ali, scores = ali[0].tolist(), scores[0].exp().tolist()
                ratio = wav.shape[1] / em.shape[0] / sr  # seconds per frame
                # walk the frames: each non-blank, non-repeat frame starts the next token
                spans, prev = [], 0
                for f, t in enumerate(ali):
                    if t != 0 and (f == 0 or t != ali[f - 1] or prev == 0):
                        spans.append([f, f, scores[f]])
                    elif t != 0 and spans:
                        spans[-1][1] = f
                    prev = t
                body = spans[1:-1] if len(spans) == len(toks) else None
                if body is not None:
                    k = 0
                    for i in idx:
                        n = len(keep[i])
                        ch = body[k:k + n]
                        k += n
                        s = a0 + ch[0][0] * ratio
                        e = a0 + (ch[-1][1] + 1) * ratio
                        if keep[i] != "*":
                            times[i] = [round(s, 3), round(e, 3), round(sum(c[2] for c in ch) / n, 3)]
        out.append({"times": times})
    json.dump({"blocks": out}, open(out_p, "w", encoding="utf-8"))
    print("OK", flush=True)


if __name__ == "__main__":
    main()
