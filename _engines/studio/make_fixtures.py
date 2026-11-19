"""Generate the self-test videos (kept out of git). Windows only: uses System.Speech for the voice.

    .venv\\Scripts\\python.exe make_fixtures.py
Creates _engines/tests/studio/recap_test.mp4 (26 s, speech with pauses + a hard visual cut)
and phone_rot.mp4 (landscape pixels with a 90° rotation flag, like phone footage).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import core  # noqa: E402

OUT = core.ENGINES / "tests" / "studio"

PS_SPEECH = r"""
Add-Type -AssemblyName System.Speech
$sp = New-Object System.Speech.Synthesis.SpeechSynthesizer
$sp.SetOutputToWaveFile('{wav}')
$pb = New-Object System.Speech.Synthesis.PromptBuilder
$pb.AppendText('Good evening. This is the weekly market recap.'); $pb.AppendBreak([TimeSpan]::FromSeconds(2))
$pb.AppendText('The Nifty closed higher for the third week in a row, led by banks and IT stocks.'); $pb.AppendBreak([TimeSpan]::FromSeconds(1.5))
$pb.AppendText('Um, so, let me check my notes.'); $pb.AppendBreak([TimeSpan]::FromSeconds(2))
$pb.AppendText('Next week, watch the inflation data on Tuesday. Thanks for watching, see you next time.')
$sp.Speak($pb); $sp.Dispose()
"""


def run(cmd):
    subprocess.run([str(c) for c in cmd], check=True, capture_output=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    recap = OUT / "recap_test.mp4"
    if not recap.exists():
        wav = OUT / "speech.wav"
        run(["powershell", "-NoProfile", "-Command", PS_SPEECH.format(wav=wav)])
        dur = float(subprocess.run([core.FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                    str(wav)], capture_output=True, text=True).stdout)
        half = round(dur / 2, 2)
        run([core.FFMPEG, "-y", "-v", "error",
             "-f", "lavfi", "-i", f"testsrc2=size=1920x1080:rate=30:duration={half}",
             "-f", "lavfi", "-i", f"smptehdbars=size=1920x1080:rate=30:duration={round(dur - half, 2)}",
             "-i", wav, "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]", "-map", "2:a",
             "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-c:a", "aac", recap])
        wav.unlink(missing_ok=True)
        print("made", recap)
    phone = OUT / "phone_rot.mp4"
    if not phone.exists():
        land = OUT / "land.mp4"
        run([core.FFMPEG, "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1920x1080:r=30:d=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", land])
        run([core.FFMPEG, "-y", "-v", "error", "-display_rotation:v:0", "90", "-i", land, "-c", "copy", phone])
        land.unlink(missing_ok=True)
        print("made", phone)


if __name__ == "__main__":
    main()
