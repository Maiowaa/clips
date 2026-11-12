"""Generate a 90s synthetic test video (quiet tone + loud burst at 40-48s) and a fake
word-timed transcript with excited speech during the burst.

    python tests/make_synthetic.py        -> tests/synthetic.mp4, tests/synthetic.transcript.json
"""
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from clipper import find_bin  # noqa: E402

HERE = Path(__file__).resolve().parent
VIDEO = HERE / "synthetic.mp4"
TRANSCRIPT = HERE / "synthetic.transcript.json"

CALM = ("so we are just driving around the city right now looking for the next mission "
        "and the weather is pretty nice today I think we should check the map first "
        "then maybe grab some food and see what the other players are doing over there").split()
HYPE = "OH MY GOD NO WAY BRO! THE COPS ARE CHASING US! LET'S GO! THIS IS INSANE! FIVE STARS WANTED! CRASH!".split()


def make_video() -> None:
    ff = find_bin("ffmpeg")
    if not ff:
        sys.exit("ffmpeg not found")
    audio = "aevalsrc='0.02*sin(2*PI*440*t)+between(t,40,48)*0.7*sin(2*PI*880*t)':s=48000:d=90"
    subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=90",
                    "-f", "lavfi", "-i", audio,
                    "-c:v", "libx264", "-preset", "veryfast", "-threads", "2", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(VIDEO)], check=True)


def make_transcript() -> None:
    segments = []

    def add(words, t, step):
        ws = [{"start": round(t + i * step, 2), "end": round(t + i * step + step * 0.85, 2), "word": w}
              for i, w in enumerate(words)]
        segments.append({"start": ws[0]["start"], "end": ws[-1]["end"],
                         "text": " ".join(w["word"] for w in ws), "words": ws})
        return ws[-1]["end"]

    ci = 0

    def calm(t, stop):
        nonlocal ci
        while t + 10 * 0.45 < stop:
            words = (CALM * 2)[ci % len(CALM): ci % len(CALM) + 10]
            ci += 10
            t = add(words, t, 0.45) + 0.8      # sentence + pause
        return t

    calm(1.0, 39.8)
    add(HYPE[:9], 40.2, 0.42)                  # two excited segments inside the burst
    add(HYPE[9:], 44.2, 0.42)
    calm(48.6, 88.0)
    TRANSCRIPT.write_text(json.dumps({"language": "en", "duration": 90, "segments": segments}, indent=1),
                          encoding="utf-8")


if __name__ == "__main__":
    make_video()
    make_transcript()
    print(f"wrote {VIDEO} and {TRANSCRIPT}")
