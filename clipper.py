#!/usr/bin/env python3
"""
clipper.py - free, local, watermark-free "long video -> vertical shorts" pipeline.

    python clipper.py run <url_or_local_file> [--top 8] [--min 20] [--max 45]
        [--model small] [--layout blur|crop|split] [--crop-x 0.5]
        [--facecam x,y,w,h|NAME] [--split-top 0.4] [--no-captions]

    python clipper.py facecam <video_or_url> [--at 1:00] [--box x,y,w,h] [--save NAME]
        [--keywords "a,b"] [--section 1:00:00-1:40:00] [--transcript file.json]
        [--work work] [--out clips] [--dry-run]

Pipeline: download (yt-dlp) -> transcribe (faster-whisper, cached) ->
score moments (audio loudness + transcript signals, no LLM) ->
render 1080x1920 H.264/AAC clips with burned-in word-highlight captions.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# EDIT ME: reaction keywords / phrases that signal a hype moment.
# Lowercase. Multi-word phrases are matched as consecutive words.
# Add a streamer's catchphrases here or at runtime with --keywords "a,b".
# --------------------------------------------------------------------------- #
KEYWORDS = [
    "omg", "oh my god", "no way", "bro", "bruh", "insane", "crazy", "let's go",
    "lets go", "what", "wait", "yo", "dude", "holy", "wow", "run", "go go",
    "cops", "cop", "police", "chase", "crash", "crashed", "wanted", "stars",
    "heist", "gta", "trailer", "vice city", "leonida", "lucia", "jason",
    "clip it", "clip that", "chat", "help", "dead", "died", "kill", "boom",
    "explosion", "shoot", "gun", "money", "bank", "jail", "escape", "drive",
    "fast", "jump", "hit", "yes", "noo", "nooo", "oh no", "lmao", "lol",
    "hahaha", "haha", "unbelievable", "clutch", "scream", "rage",
]

# Scoring knobs
HOP = 0.5                 # seconds per analysis bin
W_ENERGY, W_TEXT = 0.55, 0.45
W_DENSITY, W_KEYWORD, W_PUNCT = 0.35, 0.45, 0.20   # inside the text signal
GAP_BUFFER = 5.0          # min seconds between picked clips
PRE_ROLL, POST_ROLL = 0.25, 0.5
SNAP_TOLERANCE = 3.0      # how far (s) to look for a Whisper segment boundary
MIN_WORDS = 10

# Render knobs
OUT_W, OUT_H = 1080, 1920
CAPTION_FONT, CAPTION_SIZE, CAPTION_MARGIN_V = "Arial", 84, 520
SPLIT_TOP = 0.40          # split layout: share of the 1920px height given to the facecam panel
FACECAM_FILE = Path(__file__).resolve().with_name("facecams.json")   # saved per-streamer boxes
WORDS_PER_CHUNK = 3


# --------------------------------------------------------------------------- #
# Small utilities
# --------------------------------------------------------------------------- #
class ClipperError(RuntimeError):
    pass


def log(msg: str) -> None:
    print(msg, flush=True)


def install_hint(name: str) -> str:
    system = platform.system()
    if name in ("ffmpeg", "ffprobe"):
        return {
            "Windows": "winget install Gyan.FFmpeg   (or: choco install ffmpeg / scoop install ffmpeg), then open a new terminal",
            "Darwin": "brew install ffmpeg",
        }.get(system, "sudo apt install ffmpeg   (Fedora: sudo dnf install ffmpeg, Arch: sudo pacman -S ffmpeg)")
    return f"pip install {name}"


def find_bin(name: str) -> str | None:
    """shutil.which, plus common winget locations (PATH isn't refreshed until a new shell)."""
    found = shutil.which(name)
    if found:
        return found
    if platform.system() == "Windows":
        local = Path(os.environ.get("LOCALAPPDATA", ""))
        links = local / "Microsoft" / "WinGet" / "Links" / f"{name}.exe"
        if links.exists():
            return str(links)
        pkgs = local / "Microsoft" / "WinGet" / "Packages"
        if pkgs.exists():
            for p in pkgs.glob(f"*FFmpeg*/**/bin/{name}.exe"):
                return str(p)
    return None


BINS: dict[str, str | None] = {}


def check_binaries() -> None:
    for name in ("ffmpeg", "ffprobe"):
        BINS[name] = find_bin(name)
        if not BINS[name]:
            log(f"[warn] '{name}' not found on PATH. Install it with:\n       {install_hint(name)}")
        elif not shutil.which(name):
            # Found outside PATH (e.g. fresh winget install): add it so child tools like yt-dlp see it too.
            os.environ["PATH"] = str(Path(BINS[name]).parent) + os.pathsep + os.environ.get("PATH", "")
    if not all(BINS.values()):
        log("[warn] Continuing, but audio analysis/rendering will fail until ffmpeg+ffprobe are installed.")


def need_bin(name: str) -> str:
    path = BINS.get(name) or find_bin(name)
    if not path:
        raise ClipperError(f"'{name}' is required for this step but was not found.\n  Install: {install_hint(name)}")
    return path


def run_cmd(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-15:])
        raise ClipperError(f"Command failed ({Path(args[0]).name}, exit {proc.returncode}):\n{tail}")
    return proc


def hms(t: float, sep: str = ":") -> str:
    t = max(0, int(t))
    return f"{t // 3600}{sep}{(t % 3600) // 60:02d}{sep}{t % 60:02d}"


def parse_time(s: str) -> float:
    """'1:02:03', '62:03', '3723', '3723.5' -> seconds."""
    s = s.strip()
    if not s:
        raise ValueError("empty time")
    parts = s.split(":")
    if len(parts) > 3:
        raise ValueError(s)
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def parse_section(s: str) -> tuple[float, float | None]:
    """'1:00:00-1:40:00' -> (3600, 6000); '1:00:00-' -> (3600, None) = to the end."""
    m = re.fullmatch(r"\s*([0-9:.]*)\s*-\s*([0-9:.]*)\s*", s or "")
    if not m:
        raise ClipperError(f"--section must look like START-END, e.g. 1:00:00-1:40:00 (got {s!r})")
    try:
        a = parse_time(m.group(1)) if m.group(1) else 0.0
        b = parse_time(m.group(2)) if m.group(2) else None
    except ValueError:
        raise ClipperError(f"--section has an invalid time: {s!r}")
    if b is not None and b <= a:
        raise ClipperError(f"--section end must be after start (got {s!r})")
    return a, b


def section_tag(sec: tuple[float, float | None] | None) -> str:
    if not sec:
        return ""
    a, b = sec
    return f"_s{int(a)}-{'end' if b is None else int(b)}"


def probe_duration(video: Path) -> float | None:
    if not (BINS.get("ffprobe") or find_bin("ffprobe")):
        return None
    out = run_cmd([need_bin("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                   "-of", "default=noprint_wrappers=1:nokey=1", str(video)]).stdout.strip()
    try:
        return float(out)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 1. Download
# --------------------------------------------------------------------------- #
def is_url(s: str) -> bool:
    return bool(re.match(r"^https?://", s, re.I))


def download(url: str, work: Path, sec: tuple[float, float | None] | None = None) -> Path:
    try:
        import yt_dlp  # noqa: WPS433 (lazy import)
        from yt_dlp.utils import download_range_func
    except ImportError:
        raise ClipperError("yt-dlp is not installed. Run: pip install yt-dlp")
    work.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
        "noplaylist": True,
        "outtmpl": str(work / f"%(title).60s [%(id)s]{section_tag(sec)}.%(ext)s"),
        "restrictfilenames": True,
        "quiet": False,
        "noprogress": False,
    }
    if sec:
        a, b = sec
        # Only fetch this part of the VOD. Re-encoding at the cut points keeps it frame-accurate.
        opts["download_ranges"] = download_range_func(None, [(a, math.inf if b is None else b)])
        opts["force_keyframes_at_cuts"] = True
    ff = BINS.get("ffmpeg")
    if ff:
        opts["ffmpeg_location"] = str(Path(ff).parent)
    log(f"[download] {url}" + (f"  section {hms(sec[0])}-{'end' if sec[1] is None else hms(sec[1])}" if sec else ""))
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info.get("requested_downloads"):
                path = Path(info["requested_downloads"][0]["filepath"])
            else:
                path = Path(ydl.prepare_filename(info)).with_suffix(".mp4")
    except yt_dlp.utils.DownloadError as e:
        raise ClipperError(f"Download failed: {e}\n  Try: pip install -U yt-dlp  (sites change often)")
    if not path.exists():
        raise ClipperError(f"yt-dlp finished but the output file was not found: {path}")
    log(f"[download] -> {path}")
    return path


# --------------------------------------------------------------------------- #
# 2. Transcribe
# --------------------------------------------------------------------------- #
def load_transcript(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):           # bare list of segments
        data = {"segments": data}
    if "segments" not in data:
        raise ClipperError(f"Transcript {path} has no 'segments' key.")
    for seg in data["segments"]:
        seg.setdefault("words", [])
        seg.setdefault("text", " ".join(w["word"] for w in seg["words"]))
    return data


def seek_args(lo: float, hi: float | None) -> list[str]:
    """ffmpeg input options that decode only [lo, hi) (accurate: we decode, not stream-copy)."""
    args = ["-ss", f"{lo:.3f}"] if lo > 0 else []
    if hi is not None:
        args += ["-t", f"{hi - lo:.3f}"]
    return args


def decode_audio_range(video: Path, lo: float, hi: float | None):
    """Mono 16 kHz float32 numpy audio for [lo, hi), decoded by ffmpeg (only that range is in RAM)."""
    import numpy as np
    proc = subprocess.run([need_bin("ffmpeg"), "-v", "error", *seek_args(lo, hi), "-i", str(video),
                           "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"], capture_output=True)
    if proc.returncode != 0:
        raise ClipperError("ffmpeg could not decode audio: " + proc.stderr.decode(errors="replace")[-500:])
    return np.frombuffer(proc.stdout, dtype=np.int16).astype(np.float32) / 32768.0


_MODELS: dict = {}


def transcribe(video: Path, model_name: str, cache: Path, rng: tuple[float, float] | None = None) -> dict:
    """Transcribe the whole file, or only rng=(lo, hi) seconds; timestamps are always file-absolute."""
    if cache.exists():
        log(f"[transcribe] using cached {cache}")
        return load_transcript(cache)
    os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
    try:
        from faster_whisper import WhisperModel  # lazy: --transcript works without it
    except ImportError:
        raise ClipperError("faster-whisper is not installed. Run: pip install faster-whisper\n"
                           "  (or pass an existing transcript with --transcript file.json)")
    model = _MODELS.get(model_name)
    if model is None:   # keep it loaded: live mode transcribes a new chunk every few minutes
        log(f"[transcribe] loading Whisper '{model_name}' (int8, CPU) - first run downloads the model")
        model = _MODELS[model_name] = WhisperModel(model_name, device="cpu", compute_type="int8")
    if rng:
        lo = rng[0]
        log(f"[transcribe] section {hms(rng[0])}-{hms(rng[1])}")
        source = decode_audio_range(video, rng[0], rng[1])
    else:
        lo, source = 0.0, str(video)
    segments_iter, info = model.transcribe(source, word_timestamps=True, vad_filter=True)
    segments = []
    total = info.duration or 0
    for seg in segments_iter:
        words = [{"start": round(w.start + lo, 3), "end": round(w.end + lo, 3), "word": w.word.strip()}
                 for w in (seg.words or []) if w.word.strip()]
        segments.append({"start": round(seg.start + lo, 3), "end": round(seg.end + lo, 3),
                         "text": seg.text.strip(), "words": words})
        if total:
            print(f"\r[transcribe] {seg.end / total * 100:5.1f}%", end="", flush=True)
    print()
    data = {"language": info.language, "duration": total, "model": model_name,
            "range": list(rng) if rng else None, "segments": segments}
    cache.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"[transcribe] {len(segments)} segments -> {cache}")
    return data


def all_words(tr: dict) -> list[dict]:
    return [w for seg in tr["segments"] for w in seg["words"]]


# --------------------------------------------------------------------------- #
# 3. Signals + moment picking
# --------------------------------------------------------------------------- #
def audio_energy_db(video: Path, n_bins: int, lo: float = 0.0, hi: float | None = None):
    """Per-HOP loudness (dB) of [lo, hi) from a mono 16 kHz s16le stream piped out of ffmpeg."""
    import numpy as np
    ff = BINS.get("ffmpeg") or find_bin("ffmpeg")
    if not ff:
        log("[warn] ffmpeg missing: skipping audio energy (text signal only).")
        return np.zeros(n_bins)
    sr = 16000
    hop_samples = int(sr * HOP)
    block = hop_samples * 2 * 240        # 2 bytes/sample, 2 min of audio per read
    proc = subprocess.Popen([ff, "-v", "error", *seek_args(lo, hi), "-i", str(video),
                             "-vn", "-ac", "1", "-ar", str(sr),
                             "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    dbs: list[float] = []
    leftover = b""
    assert proc.stdout is not None
    while True:
        chunk = proc.stdout.read(block)
        if not chunk:
            break
        buf = leftover + chunk
        usable = len(buf) - (len(buf) % (hop_samples * 2))
        leftover = buf[usable:]
        if usable:
            x = np.frombuffer(buf[:usable], dtype=np.int16).astype(np.float32).reshape(-1, hop_samples)
            rms = np.sqrt(np.mean(x * x, axis=1)) / 32768.0
            dbs.extend((20 * np.log10(rms + 1e-10)).tolist())
    proc.wait()
    if leftover and len(leftover) >= 2:
        x = np.frombuffer(leftover[: len(leftover) // 2 * 2], dtype=np.int16).astype(np.float32)
        dbs.append(float(20 * np.log10(np.sqrt(np.mean(x * x)) / 32768.0 + 1e-10)))
    if not dbs:
        log("[warn] no audio stream found: energy signal is zero.")
        return np.zeros(n_bins)
    arr = np.array(dbs[:n_bins] + [min(dbs)] * max(0, n_bins - len(dbs)))
    return arr


def zscore_median(x):
    import numpy as np
    sd = float(np.std(x))
    if sd < 1e-9:
        return np.zeros_like(x)
    return np.clip((x - np.median(x)) / sd, 0, None)


def norm95(x):
    import numpy as np
    if not len(x) or float(np.max(x)) <= 0:
        return np.zeros_like(x, dtype=float)
    ref = float(np.percentile(x, 95))
    if ref <= 0:
        ref = float(np.max(x))
    return np.clip(x / ref, 0, 1)


_TOKEN_RE = re.compile(r"[^a-z0-9']+")


def tokenize(word: str) -> str:
    return _TOKEN_RE.sub("", word.lower().replace("’", "'"))


def text_signals(words: list[dict], n_bins: int, keywords: list[str], lo: float = 0.0):
    import numpy as np
    density = np.zeros(n_bins)
    kw = np.zeros(n_bins)
    punct = np.zeros(n_bins)

    def b(t: float) -> int:
        return min(n_bins - 1, max(0, int((t - lo) / HOP)))

    tokens = [tokenize(w["word"]) for w in words]
    for w in words:
        mid = (w["start"] + w["end"]) / 2
        density[b(mid)] += 1
        if "!" in w["word"] or "?" in w["word"]:
            punct[b(w["end"])] += 1
    phrases = sorted({tuple(k.split()) for k in keywords if k.strip()}, key=len, reverse=True)
    for i in range(len(tokens)):
        for ph in phrases:
            if tuple(tokens[i:i + len(ph)]) == ph:
                kw[b(words[i]["start"])] += 1
                break
    text = W_DENSITY * norm95(density) + W_KEYWORD * norm95(kw) + W_PUNCT * norm95(punct)
    return text, density, kw, punct


@dataclass
class Clip:
    start: float
    end: float
    score: float
    snippet: str = ""

    @property
    def dur(self) -> float:
        return self.end - self.start


def snap(ws: float, we: float, segs: list[dict], lo: float, hi: float, cmin: float, cmax: float):
    """Snap a window to Whisper segment boundaries, add pre/post-roll, clamp to [lo, hi] and min/max."""
    starts = [s["start"] for s in segs]
    ends = [s["end"] for s in segs]

    def nearest(vals, t):
        cands = [v for v in vals if abs(v - t) <= SNAP_TOLERANCE]
        return min(cands, key=lambda v: abs(v - t)) if cands else t

    s = max(lo, nearest(starts, ws) - PRE_ROLL)
    e = min(hi, nearest(ends, we) + POST_ROLL)
    if e - s > cmax:
        # pull the end back to the last segment end that still fits
        fits = [v + POST_ROLL for v in ends if s + cmin <= v + POST_ROLL <= s + cmax]
        e = max(fits) if fits else s + cmax
    if e - s < cmin:
        later = [v + POST_ROLL for v in ends if s + cmin <= v + POST_ROLL <= s + cmax]
        e = min(later) if later else s + cmin
    e = min(e, hi)
    if e - s < cmin:                    # near the end of the range: slide start back
        s = max(lo, e - cmin)
    return round(s, 3), round(e, 3)


def pick_moments(video: Path | None, tr: dict, lo: float, hi: float, top: int, cmin: float, cmax: float,
                 keywords: list[str]) -> list[Clip]:
    """Score [lo, hi) of the file (seconds) and return the top clips in file-absolute times."""
    import numpy as np
    n = max(1, int(math.ceil((hi - lo) / HOP)))
    words = [w for w in all_words(tr) if lo <= w["start"] and w["end"] <= hi]
    segs = [s for s in tr["segments"] if s["end"] > lo and s["start"] < hi]
    energy = zscore_median(audio_energy_db(video, n, lo, hi)) if video else np.zeros(n)
    text, *_ = text_signals(words, n, keywords, lo)
    score = W_ENERGY * norm95(energy) + W_TEXT * text

    win = max(1, int(round((cmin + cmax) / 2 / HOP)))
    if win >= n:
        win = n
    from numpy.lib.stride_tricks import sliding_window_view
    view = sliding_window_view(score, win)
    wscore = view.mean(axis=1) + 0.5 * view.max(axis=1)

    order = np.argsort(-wscore, kind="stable")
    picked: list[Clip] = []
    for i in order:
        if len(picked) >= top:
            break
        ws, we = lo + i * HOP, min(hi, lo + (i + win) * HOP)
        if any(ws < c.end + GAP_BUFFER and we > c.start - GAP_BUFFER for c in picked):
            continue                     # cheap pre-check before snapping
        s, e = snap(ws, we, segs, lo, hi, cmin, cmax)
        if any(s < c.end + GAP_BUFFER and e > c.start - GAP_BUFFER for c in picked):
            continue
        in_clip = [w for w in words if w["start"] >= s and w["end"] <= e]
        if len(in_clip) < MIN_WORDS:
            continue
        snippet = " ".join(w["word"] for w in in_clip)
        if len(snippet) > 220:
            snippet = snippet[:220].rsplit(" ", 1)[0] + "..."
        picked.append(Clip(s, e, round(float(wscore[i]), 4), snippet))
    return picked


# --------------------------------------------------------------------------- #
# 4. Captions (ASS) + render
# --------------------------------------------------------------------------- #
def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "").replace("{", "(").replace("}", ")").upper()


def build_ass(words: list[dict], clip: Clip, margin_v: int = CAPTION_MARGIN_V) -> str:
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{CAPTION_FONT},{CAPTION_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,7,3,2,70,70,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    ws = [w for w in words if w["end"] > clip.start and w["start"] < clip.end]
    # group into chunks of N words, breaking on long pauses
    chunks: list[list[dict]] = []
    for w in ws:
        if chunks and len(chunks[-1]) < WORDS_PER_CHUNK and w["start"] - chunks[-1][-1]["end"] < 1.0:
            chunks[-1].append(w)
        else:
            chunks.append([w])
    lines = []
    yellow, white = r"{\c&H00FFFF&}", r"{\c&HFFFFFF&}"
    for ci, ch in enumerate(chunks):
        next_start = chunks[ci + 1][0]["start"] if ci + 1 < len(chunks) else clip.end
        texts = [ass_escape(w["word"]) for w in ch]
        for wi, w in enumerate(ch):
            st = w["start"]
            en = ch[wi + 1]["start"] if wi + 1 < len(ch) else min(next_start, w["end"] + 0.4)
            en = max(en, st + 0.05)
            st_r, en_r = st - clip.start, min(en, clip.end) - clip.start
            if en_r <= 0:
                continue
            body = " ".join(f"{yellow}{t}{white}" if k == wi else t for k, t in enumerate(texts))
            lines.append(f"Dialogue: 0,{ass_time(st_r)},{ass_time(en_r)},Default,,0,0,0,,{body}")
    return head + "\n".join(lines) + "\n"


@dataclass
class Layout:
    name: str = "blur"                     # blur | crop | split
    crop_x: float = 0.5                    # crop/split: horizontal position of the gameplay crop
    facecam: tuple[float, float, float, float] | None = None   # split: x, y, w, h as 0-1 fractions
    split_top: float = SPLIT_TOP           # split: share of output height for the facecam panel

    @property
    def top_h(self) -> int:
        return int(round(OUT_H * self.split_top / 2)) * 2       # even pixel count

    @property
    def caption_margin_v(self) -> int:
        # split: centre the captions on the seam between facecam and gameplay
        return OUT_H - self.top_h - CAPTION_SIZE // 2 - 10 if self.name == "split" else CAPTION_MARGIN_V


def video_filter(lay: Layout, captions: bool) -> str:
    cx = min(1.0, max(0.0, lay.crop_x))
    if lay.name == "split":
        if not lay.facecam:
            raise ClipperError("--layout split needs --facecam (x,y,w,h or a saved name). "
                               "Find the box with: python clipper.py facecam <video>")
        x, y, w, h = lay.facecam
        top, bot = lay.top_h, OUT_H - lay.top_h
        fc = (f"[0:v]split=2[cam][game];"
              f"[cam]crop=w=iw*{w:.5f}:h=ih*{h:.5f}:x=iw*{x:.5f}:y=ih*{y:.5f},"
              f"scale={OUT_W}:{top}:force_original_aspect_ratio=increase,crop={OUT_W}:{top},setsar=1[top];"
              f"[game]crop=w='min(iw,ih*{OUT_W}/{bot})':h='min(ih,iw*{bot}/{OUT_W})':"
              f"x='(iw-ow)*{cx:.4f}':y='(ih-oh)/2',scale={OUT_W}:{bot},setsar=1[bot];"
              f"[top][bot]vstack=inputs=2[v0]")
    elif lay.name == "crop":
        fc = (f"[0:v]crop=w='min(iw,ih*9/16)':h='min(ih,iw*16/9)':x='(iw-ow)*{cx:.4f}':y='(ih-oh)/2',"
              f"scale={OUT_W}:{OUT_H},setsar=1[v0]")
    else:
        fc = (f"[0:v]split=2[bg][fg];"
              f"[bg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
              f"scale={OUT_W // 4}:{OUT_H // 4},boxblur=12:2,scale={OUT_W}:{OUT_H},"
              f"eq=brightness=-0.15:saturation=1.1[bgb];"
              f"[fg]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=decrease:force_divisible_by=2[fgs];"
              f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1[v0]")
    if captions:
        fc += ";[v0]ass=captions.ass[v]"
    else:
        fc += ";[v0]null[v]"
    return fc


def probe_size(video: Path) -> tuple[int, int]:
    out = run_cmd([need_bin("ffprobe"), "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height", "-of", "csv=p=0", str(video)]).stdout.strip()
    try:   # MPEG-TS files list the stream twice (program + stream): use the first line
        first = next(line for line in out.splitlines() if line.strip())
        w, h = (int(v) for v in first.split(",")[:2])
        return w, h
    except ValueError:
        raise ClipperError(f"Could not read the video size of {video}")


def load_facecams() -> dict:
    if FACECAM_FILE.exists():
        try:
            return json.loads(FACECAM_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ClipperError(f"{FACECAM_FILE.name} is not valid JSON: {e}")
    return {}


Box = tuple[float, float, float, float]      # x, y, w, h as 0-1 fractions of the source frame


def preset_boxes(value) -> list[Box]:
    """A preset is one box [x,y,w,h] or a list of boxes (one per stream scene/layout)."""
    if value and isinstance(value[0], (int, float)):
        return [tuple(value)]  # type: ignore[list-item]
    return [tuple(b) for b in value]  # type: ignore[misc]


def parse_facecam(spec: str, video: Path | None) -> list[Box]:
    """'x,y,w,h' (0-1 fractions or source pixels) or a saved preset name -> candidate boxes."""
    nums = re.fullmatch(r"\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*", spec)
    if nums:
        return [parse_box(spec, video)]
    presets = load_facecams()
    if spec in presets:
        return preset_boxes(presets[spec])
    known = ", ".join(sorted(presets)) or "none saved yet"
    raise ClipperError(f"Unknown facecam {spec!r}. Use x,y,w,h or a saved name ({known}).\n"
                       "  Save one with: python clipper.py facecam <video> --box x,y,w,h --save NAME")


def parse_box(spec: str, video: Path | None) -> Box:
    nums = re.fullmatch(r"\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*", spec)
    if not nums:
        raise ClipperError(f"Box must be x,y,w,h (got {spec!r})")
    if nums:
        vals = [float(v) for v in nums.groups()]
        if any(v > 1 for v in vals):                    # pixels -> fractions of the source frame
            if video is None:
                raise ClipperError("Pixel facecam boxes need the video to read its size.")
            sw, sh = probe_size(video)
            vals = [vals[0] / sw, vals[1] / sh, vals[2] / sw, vals[3] / sh]
        x, y, w, h = vals
        if w <= 0 or h <= 0 or x < 0 or y < 0 or x + w > 1.001 or y + h > 1.001:
            raise ClipperError(f"--facecam box {spec!r} is outside the frame. Use x,y,w,h with x+w<=1 and y+h<=1 "
                               "(fractions) or pixel values inside the video size.")
        return (round(x, 4), round(y, 4), round(min(w, 1 - x), 4), round(min(h, 1 - y), 4))
    raise AssertionError


# --- facecam overlay detection ------------------------------------------------
# A facecam is pasted onto the stream as a rectangle, so its border is a sharp, straight edge that
# survives whatever the streamer does (head down, looking away) and vanishes when the scene changes
# (fullscreen cam, BRB, IRL). We score each candidate box by edge strength *on* its border vs just
# beside it: ~1 = no overlay there (max 1.1 measured), real overlays 5-30 on Kai/Lacy/Stable Ronaldo VODs.
BORDER_MIN = 3.0          # median border score needed to trust a box
FACECAM_SAMPLES = 5       # frames sampled per clip


def grab_gray(video: Path, t: float, size: tuple[int, int]):
    import numpy as np
    sw, sh = size
    raw = subprocess.run([need_bin("ffmpeg"), "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", str(video),
                          "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                         capture_output=True).stdout
    if len(raw) < sw * sh:
        return None
    return np.frombuffer(raw[: sw * sh], np.uint8).reshape(sh, sw).astype(np.float32)


def to_px(box: Box, size: tuple[int, int]) -> tuple[int, int, int, int]:
    sw, sh = size
    x, y, w, h = box
    return int(round(x * sw)), int(round(y * sh)), int(round(w * sw)), int(round(h * sh))


def border_score(img, px: tuple[int, int, int, int]) -> float:
    import numpy as np
    H, W = img.shape
    x, y, w, h = px
    gx = np.abs(np.diff(img, axis=1))       # gx[:, c] = |img[:, c+1] - img[:, c]|
    gy = np.abs(np.diff(img, axis=0))
    ys = slice(y + h // 10, y + h - h // 10)
    xs = slice(x + w // 10, x + w - w // 10)

    def side(profile, pos: int, lim: int) -> float:
        on = max(profile(p) for p in range(pos - 2, pos + 2) if 0 <= p < lim)
        off = [profile(p) for d in range(5, 12) for p in (pos - d, pos + d) if 0 <= p < lim]
        return on / (float(np.mean(off)) + 1.0)

    sides = []
    if x > 3:
        sides.append(side(lambda c: gx[ys, c].mean(), x - 1, W - 1))
    if x + w < W - 3:
        sides.append(side(lambda c: gx[ys, c].mean(), x + w - 1, W - 1))
    if y > 3:
        sides.append(side(lambda r: gy[r, xs].mean(), y - 1, H - 1))
    if y + h < H - 3:
        sides.append(side(lambda r: gy[r, xs].mean(), y + h - 1, H - 1))
    # Mean of the two strongest sides: in dark scenes one or two cam sides blend into the gameplay,
    # but a real overlay still has >= 2 clear edges, while empty boxes stay ~1 on every side.
    top2 = sorted(sides, reverse=True)[:2]
    return float(np.mean(top2)) if top2 else 0.0


def snap_box(img, px: tuple[int, int, int, int], radius: int = 30) -> tuple[int, int, int, int]:
    """Move each side of a rough box to the strongest straight edge within `radius` px."""
    import numpy as np
    H, W = img.shape
    x, y, w, h = px
    gx = np.abs(np.diff(img, axis=1))
    gy = np.abs(np.diff(img, axis=0))
    ys = slice(y + h // 10, y + h - h // 10)
    xs = slice(x + w // 10, x + w - w // 10)

    def best(profile, guess: int, lim: int) -> int:
        # Among strong edges (>= 60% of the strongest nearby), take the one closest to the guess, so
        # a neighbouring overlay (e.g. a viewer-count bar under the cam) doesn't steal the edge.
        cands = list(range(max(0, guess - radius), min(lim, guess + radius)))
        if not cands:
            return guess
        vals = {p: profile(p) for p in cands}
        strong = [p for p in cands if vals[p] >= 0.6 * max(vals.values())]
        return min(strong, key=lambda p: abs(p - guess)) + 1

    left = 0 if x <= radius else best(lambda c: gx[ys, c].mean(), x - 1, W - 1)
    right = W if x + w >= W - radius else best(lambda c: gx[ys, c].mean(), x + w - 1, W - 1)
    top = 0 if y <= radius else best(lambda r: gy[r, xs].mean(), y - 1, H - 1)
    bottom = H if y + h >= H - radius else best(lambda r: gy[r, xs].mean(), y + h - 1, H - 1)
    return left, top, right - left, bottom - top


def choose_facecam(video: Path, clip: Clip, boxes: list[Box], size: tuple[int, int]) -> tuple[Box | None, float]:
    """Pick the candidate box whose overlay border is present during this clip (None = no facecam)."""
    import numpy as np
    times = [clip.start + clip.dur * (i + 0.5) / FACECAM_SAMPLES for i in range(FACECAM_SAMPLES)]
    frames = [f for f in (grab_gray(video, t, size) for t in times) if f is not None]
    if not frames:
        return None, 0.0
    scored = [(float(np.median([border_score(f, to_px(b, size)) for f in frames])), b) for b in boxes]
    score, box = max(scored, key=lambda s: s[0])
    return (box if score >= BORDER_MIN else None), score


def render(video: Path, clip: Clip, out_path: Path, words: list[dict], lay: Layout, captions: bool) -> None:
    ff = need_bin("ffmpeg")
    with tempfile.TemporaryDirectory(prefix="clipper_") as td:
        tdir = Path(td)
        if captions:
            (tdir / "captions.ass").write_text(build_ass(words, clip, lay.caption_margin_v), encoding="utf-8")
        args = [ff, "-hide_banner", "-loglevel", "error", "-y",
                "-ss", f"{clip.start:.3f}", "-i", str(video.resolve()), "-t", f"{clip.dur:.3f}",
                "-filter_complex", video_filter(lay, captions),
                "-map", "[v]", "-map", "0:a:0?",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                str(out_path.resolve())]
        # cwd = temp dir so the ass filter gets a bare relative filename (no Windows/macOS escaping bugs)
        run_cmd(args, cwd=tdir)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def layout_for_clip(video: Path, c: Clip, a: argparse.Namespace, lay: Layout, cam_boxes: list[Box],
                    size: tuple[int, int]) -> tuple[Layout, str]:
    """Split layouts: pick the facecam position visible in this clip, or fall back to blur."""
    if a.layout != "split":
        return lay, a.layout
    if a.no_facecam_check:
        return Layout("split", a.crop_x, cam_boxes[0], a.split_top), "split"
    box, bscore = choose_facecam(video, c, cam_boxes, size)
    if box:
        return Layout("split", a.crop_x, box, a.split_top), f"split (facecam edge {bscore:.1f})"
    return Layout("blur", a.crop_x, None, a.split_top), f"blur: no facecam found (edge {bscore:.1f})"


def resolve_layout(a: argparse.Namespace, video: Path | None) -> tuple[Layout, list[Box]]:
    lay = Layout(a.layout, a.crop_x, None, a.split_top)
    cam_boxes: list[Box] = []
    if a.layout == "split":
        if not a.facecam:
            raise ClipperError("--layout split needs --facecam (x,y,w,h or a saved name).\n"
                               "  Find the box with: python clipper.py facecam <video_or_url> --at 1:00")
        cam_boxes = parse_facecam(a.facecam, video)
        log(f"[layout] split: {len(cam_boxes)} facecam position(s) for '{a.facecam}', facecam panel {lay.top_h}px"
            + ("" if a.no_facecam_check else "; each clip is checked and falls back to blur if no facecam"))
    return lay, cam_boxes


def cmd_run(a: argparse.Namespace) -> int:
    check_binaries()
    work, out = Path(a.work), Path(a.out)
    work.mkdir(parents=True, exist_ok=True)
    if a.min <= 0 or a.max < a.min:
        raise ClipperError("--min must be > 0 and --max must be >= --min")

    sec = parse_section(a.section) if a.section else None
    tag = section_tag(sec)
    # `offset` maps times in the working file back to the original VOD's clock, so clip
    # names and clips.csv always show where the moment is in the full stream.
    offset = 0.0
    if is_url(a.source):
        vid = _video_id(a.source)
        cached = sorted(work.glob(f"*{vid}[]]{tag}.mp4")) if vid else []
        video = cached[0] if cached else download(a.source, work, sec)
        if cached:
            log(f"[download] reusing {video}")
        offset = sec[0] if sec else 0.0
        file_sec = None                  # the downloaded file *is* the section
    else:
        video = Path(a.source)
        if not video.exists():
            raise ClipperError(f"Input file not found: {video}")
        file_sec = sec                   # analyse only this range of the local file

    lay, cam_boxes = resolve_layout(a, video)

    duration = probe_duration(video)
    if a.transcript:
        tr = load_transcript(Path(a.transcript))
        log(f"[transcribe] loaded {a.transcript}")
    else:
        rng = None
        if file_sec:
            if not duration:
                raise ClipperError("--section on a local file needs ffprobe to read the duration.")
            rng = (file_sec[0], min(duration, file_sec[1] or duration))
            if rng[0] >= duration:
                raise ClipperError(f"--section starts at {hms(rng[0])} but the video is only {hms(duration)} long.")
        tr = transcribe(video, a.model, work / f"{video.stem}{tag if file_sec else ''}.transcript.json", rng)

    words = all_words(tr)
    duration = duration or tr.get("duration") or (words[-1]["end"] if words else 0)
    if not duration:
        raise ClipperError("Could not determine video duration (is ffprobe installed?)")
    lo, hi = 0.0, duration
    if file_sec:
        lo, hi = file_sec[0], min(duration, file_sec[1] or duration)
        if lo >= duration:
            raise ClipperError(f"--section starts at {hms(lo)} but the video is only {hms(duration)} long.")
    n_words = sum(1 for w in words if lo <= w["start"] and w["end"] <= hi)
    log(f"[pick] range {hms(lo + offset)}-{hms(hi + offset)} ({hms(hi - lo)}), {n_words} words")

    keywords = KEYWORDS + [k.strip().lower() for k in (a.keywords or "").split(",") if k.strip()]
    clips = pick_moments(video, tr, lo, hi, a.top, a.min, a.max, keywords)
    if not clips:
        log("[pick] no qualifying moments found (need >= %d words per window)." % MIN_WORDS)
        return 1

    log(f"\n{'#':>2}  {'start':>8}  {'end':>8}  {'len':>5}  {'score':>6}  snippet")
    for i, c in enumerate(clips, 1):
        log(f"{i:>2}  {hms(c.start + offset):>8}  {hms(c.end + offset):>8}  {c.dur:5.1f}  {c.score:6.3f}  "
            f"{c.snippet[:70]}")
    if a.dry_run:
        log("\n[dry-run] nothing rendered.")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    rows = []
    size = probe_size(video) if a.layout == "split" else (0, 0)
    for i, c in enumerate(clips, 1):
        name = f"clip_{i:02d}_{hms(c.start + offset, '-')}.mp4"
        clip_lay, used = layout_for_clip(video, c, a, lay, cam_boxes, size)
        log(f"[render] {i}/{len(clips)} {name} ({c.dur:.1f}s, layout={used})")
        render(video, c, out / name, words, clip_lay, not a.no_captions)
        rows.append({"file": name, "start": hms(c.start + offset), "end": hms(c.end + offset),
                     "start_s": round(c.start + offset, 3), "end_s": round(c.end + offset, 3),
                     "score": c.score, "layout": used, "snippet": c.snippet})
    csv_path = out / "clips.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        wr = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        wr.writeheader()
        wr.writerows(rows)
    log(f"\n[done] {len(rows)} clips in {out.resolve()}  (metadata: {csv_path.name})")
    log("Review every clip by hand before posting, and only clip footage the campaign allows.")
    return 0


def cmd_facecam(a: argparse.Namespace) -> int:
    """Save preview PNGs that help find the facecam box, optionally saving it as a named preset."""
    check_binaries()
    ff = need_bin("ffmpeg")
    at = parse_time(a.at)
    if is_url(a.source) and a.live:
        video = grab_live_sample(a.source, Path(a.work))
        at_in_file = 3.0
    elif is_url(a.source):
        # only fetch a few seconds around --at
        video = download(a.source, Path(a.work), (at, at + 4))
        at_in_file = 1.0
    else:
        video = Path(a.source)
        if not video.exists():
            raise ClipperError(f"Input file not found: {video}")
        at_in_file = at
    sw, sh = probe_size(video)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    grid = out / "facecam_grid.png"
    # 10% grid (brighter every 50%) so the box can be read off as fractions
    vf = ("drawgrid=w=iw/10:h=ih/10:t=2:c=white@0.45,"
          "drawgrid=w=iw/2:h=ih/2:t=4:c=red@0.7")
    box = parse_box(a.box, video) if a.box else None
    if box:
        img = grab_gray(video, at_in_file, (sw, sh))
        if img is not None:
            px = to_px(box, (sw, sh))
            if not a.no_snap:
                snapped = snap_box(img, px)
                if snapped != px:
                    log(f"[facecam] snapped your box {px} to the overlay edges -> {snapped} (pixels)")
                px = snapped
                box = (round(px[0] / sw, 4), round(px[1] / sh, 4), round(px[2] / sw, 4), round(px[3] / sh, 4))
            score = border_score(img, px)
            ok = score >= BORDER_MIN
            log(f"[facecam] overlay edge score {score:.1f} "
                + ("(looks like a real facecam border)" if ok else
                   f"(WEAK: below {BORDER_MIN}; is the facecam visible at --at? is the box right?)"))
        x, y, w, h = box
        vf += f",drawbox=x=iw*{x}:y=ih*{y}:w=iw*{w}:h=ih*{h}:t=6:c=yellow"
    run_cmd([ff, "-v", "error", "-y", "-ss", f"{at_in_file:.3f}", "-i", str(video.resolve()),
             "-frames:v", "1", "-vf", vf, str(grid.resolve())])
    log(f"[facecam] source {sw}x{sh}. Grid preview: {grid.resolve()}")
    log("          Thin lines every 10% of the frame, red lines at 50%.")
    if not box:
        log("          Read the facecam's left, top, width and height off the grid as fractions\n"
            "          (e.g. 0.0,0.0,0.25,0.3) or as pixels, then rerun with --box to check it.")
        return 0
    split = out / "facecam_split_preview.png"
    lay = Layout("split", a.crop_x, box, a.split_top)
    run_cmd([ff, "-v", "error", "-y", "-ss", f"{at_in_file:.3f}", "-i", str(video.resolve()),
             "-frames:v", "1", "-filter_complex", video_filter(lay, captions=False), "-map", "[v]",
             str(split.resolve())])
    log(f"[facecam] box {box} drawn in yellow. Split preview: {split.resolve()}")
    if a.save:
        presets = load_facecams()
        boxes = [] if a.reset or a.save not in presets else [list(b) for b in preset_boxes(presets[a.save])]
        if any(max(abs(p - q) for p, q in zip(b, box)) < 0.01 for b in boxes):
            log(f"[facecam] '{a.save}' already has this position; not adding a duplicate.")
        else:
            boxes.append(list(box))
            presets[a.save] = boxes
            FACECAM_FILE.write_text(json.dumps(presets, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            log(f"[facecam] saved '{a.save}' position #{len(boxes)} in {FACECAM_FILE.name}. "
                f"Use: --layout split --facecam {a.save}")
    return 0


# --------------------------------------------------------------------------- #
# Live mode: record a live stream in rolling chunks and clip each chunk as it finishes
# --------------------------------------------------------------------------- #
STREAM_QUALITY = "1080p60,1080p,720p60,720p,best"


def channel_slug(url: str) -> str:
    tail = [p for p in re.split(r"[/?#&=]", url.split("://", 1)[-1]) if p][1:] or ["stream"]
    name = tail[-1] if tail[0] in ("videos", "watch", "live") else tail[0]
    return re.sub(r"[^A-Za-z0-9_-]+", "_", name.lstrip("@"))[:40] or "stream"


def recorder_cmd(url: str, recorder: str) -> list[str]:
    ff = need_bin("ffmpeg")
    if recorder == "streamlink":
        return [sys.executable, "-m", "streamlink", "--stdout", "--loglevel", "warning",
                "--retry-open", "3", "--hls-live-edge", "3", "--ffmpeg-ffmpeg", ff, url, STREAM_QUALITY]
    return [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-part", "-o", "-",
            "-f", "best[height<=1080]/best", "--ffmpeg-location", str(Path(ff).parent), url]


def pick_recorder(choice: str) -> str:
    if choice != "auto":
        return choice
    try:
        import streamlink  # noqa: F401
        return "streamlink"
    except ImportError:
        log("[live] streamlink not installed (pip install streamlink); using yt-dlp to record. "
            "Streamlink is recommended: it filters Twitch ads.")
        return "ytdlp"


class Recorder:
    """streamlink/yt-dlp -> stdout -> ffmpeg segment muxer writing chunk_00000.ts, chunk_00001.ts, ..."""

    def __init__(self, url: str, recorder: str, chunk_dir: Path, chunk_s: int):
        chunk_dir.mkdir(parents=True, exist_ok=True)
        self.dir = chunk_dir
        self.log_path = chunk_dir / "recorder.log"
        self._log = self.log_path.open("w", encoding="utf-8", errors="replace")
        self.src = subprocess.Popen(recorder_cmd(url, recorder), stdout=subprocess.PIPE, stderr=self._log)
        seg = [need_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
               "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy",
               "-f", "segment", "-segment_time", str(chunk_s), "-segment_format", "mpegts",
               "-reset_timestamps", "1", str(chunk_dir / "chunk_%05d.ts")]
        self.seg = subprocess.Popen(seg, stdin=self.src.stdout, stderr=self._log)
        assert self.src.stdout is not None
        self.src.stdout.close()   # ffmpeg owns the pipe now

    def running(self) -> bool:
        return self.seg.poll() is None

    def stop(self) -> None:
        if self.src.poll() is None:
            self.src.terminate()          # ffmpeg sees EOF and finalises the last chunk
        try:
            self.seg.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.seg.kill()
        self._log.close()

    def chunks(self) -> list[Path]:
        """Paths by index (chunk_00000 ... newest), including ones already deleted after processing,
        so list positions stay equal to chunk numbers."""
        nums = [int(p.stem.split("_")[1]) for p in self.dir.glob("chunk_*.ts")]
        return [self.dir / f"chunk_{i:05d}.ts" for i in range(max(nums) + 1)] if nums else []

    def error_tail(self) -> str:
        try:
            return "\n".join(self.log_path.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-6:])
        except OSError:
            return ""


def grab_live_sample(url: str, work: Path, seconds: int = 8) -> Path:
    """Record a few seconds of a live stream (for the facecam command)."""
    work.mkdir(parents=True, exist_ok=True)
    out_path = work / f"live_sample_{channel_slug(url)}.ts"
    log(f"[facecam] grabbing {seconds}s from the live stream...")
    src = subprocess.Popen(recorder_cmd(url, pick_recorder("auto")), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE)
    ff = subprocess.run([need_bin("ffmpeg"), "-v", "error", "-y", "-i", "pipe:0", "-t", str(seconds),
                         "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", str(out_path)],
                        stdin=src.stdout, capture_output=True)
    src.kill()
    err = (src.stderr.read() if src.stderr else b"").decode(errors="replace").strip()
    if ff.returncode != 0 or not out_path.exists() or out_path.stat().st_size < 10000:
        raise ClipperError("Couldn't record from the live stream. Is the channel live right now?"
                           + (f"\n  {err.splitlines()[-1]}" if err else ""))
    return out_path


def concat_ts(parts: list[Path], out_path: Path) -> None:
    """Join chunks into one file with continuous timestamps (stream copy, takes ~1 s)."""
    lst = out_path.with_suffix(".txt")
    lst.write_text("".join(f"file '{p.resolve().as_posix()}'\n" for p in parts), encoding="utf-8")
    run_cmd([need_bin("ffmpeg"), "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
             "-map", "0:v:0?", "-map", "0:a:0?", "-c", "copy", str(out_path)])
    lst.unlink(missing_ok=True)


def cmd_live(a: argparse.Namespace) -> int:
    import time
    from datetime import datetime, timedelta

    check_binaries()
    need_bin("ffmpeg")
    if not is_url(a.source):
        raise ClipperError("live needs a channel URL, e.g. https://www.twitch.tv/NAME, "
                           "https://www.youtube.com/@NAME/live or https://kick.com/NAME")
    chunk_s = int(a.chunk * 60)
    if chunk_s < max(60, a.max * 2):
        raise ClipperError(f"--chunk must be at least {max(1.0, a.max * 2 / 60):.1f} minutes for --max {a.max:g}")
    recorder = pick_recorder(a.recorder)
    lay, cam_boxes = resolve_layout(a, None)
    keywords = KEYWORDS + [k.strip().lower() for k in (a.keywords or "").split(",") if k.strip()]
    slug = channel_slug(a.source)
    out = Path(a.out) if a.out else Path("live_clips") / slug
    out.mkdir(parents=True, exist_ok=True)
    csv_path = out / "clips.csv"
    fields = ["file", "moment_time", "stream_offset", "duration", "score", "layout", "snippet"]
    deadline = time.monotonic() + a.max_minutes * 60 if a.max_minutes else None
    total = 0

    log(f"[live] {a.source} -> {out.resolve()}")
    log(f"[live] recorder={recorder}, {a.chunk:g}-min chunks, {a.overlap}s overlap, "
        f"up to {a.top_per_chunk} clip(s) per chunk, model={a.model}. Ctrl+C to stop.")

    while True:   # one iteration per recording session (stream start -> stream end)
        started = datetime.now()
        chunk_dir = Path(a.work) / "live" / slug / started.strftime("%Y%m%d-%H%M%S")
        rec = Recorder(a.source, recorder, chunk_dir, chunk_s)
        log(f"[live] recording started {started:%Y-%m-%d %H:%M:%S}")
        chunk_starts: list[float] = []      # chunk index -> seconds since recording start
        picked: list[tuple[float, float]] = []
        done, stopping = 0, False
        try:
            while True:
                if deadline and not stopping and time.monotonic() > deadline:
                    log(f"[live] --max-minutes {a.max_minutes:g} reached, stopping the recording")
                    rec.stop()
                    stopping = True
                alive = rec.running()
                chunks = rec.chunks()
                ready = chunks if not alive else chunks[:-1]   # the newest chunk is still being written
                while done < len(ready):
                    total += _process_live_chunk(a, ready, done, chunk_starts, picked, started, lay, cam_boxes,
                                                 keywords, out, csv_path, fields)
                    done += 1
                if not alive:
                    break
                time.sleep(5)
        except KeyboardInterrupt:
            log("\n[live] stopping: finishing the chunks already recorded (Ctrl+C again to quit now)")
            rec.stop()
            chunks = rec.chunks()
            while done < len(chunks):
                total += _process_live_chunk(a, chunks, done, chunk_starts, picked, started, lay, cam_boxes,
                                             keywords, out, csv_path, fields)
                done += 1
            log(f"[live] done: {total} clip(s) in {out.resolve()}")
            return 0
        rec.stop()
        if done == 0:
            tail = rec.error_tail()
            log("[live] the stream isn't live (or couldn't be opened)." + (f"\n        {tail}" if tail else ""))
        else:
            log(f"[live] recording ended after {done} chunk(s), "
                f"{timedelta(seconds=int((datetime.now() - started).total_seconds()))}; {total} clip(s) so far")
        if not a.keep_chunks:
            shutil.rmtree(chunk_dir, ignore_errors=True)
        if stopping or not a.wait:
            break
        log(f"[live] --wait: checking again in {a.wait_interval}s")
        time.sleep(a.wait_interval)
    log(f"[live] done: {total} clip(s) in {out.resolve()}")
    log("Review every clip by hand before posting, and only clip footage the campaign allows.")
    return 0


def _process_live_chunk(a, chunks, k, chunk_starts, picked, started, lay, cam_boxes, keywords, out,
                        csv_path, fields) -> int:
    """Clip chunk k, analysed together with the last `--overlap` seconds of chunk k-1."""
    import time
    from datetime import timedelta
    cur = chunks[k]
    if not cur.exists():
        chunk_starts.append(chunk_starts[-1] if chunk_starts else 0.0)
        return 0
    dur = probe_duration(cur) or 0.0
    chunk_starts.append(chunk_starts[-1] + (probe_duration(chunks[k - 1]) or 0.0) if k else 0.0)
    if dur < a.min:
        log(f"[live] chunk {k + 1}: only {dur:.0f}s, skipping")
        return 0
    if k and chunks[k - 1].exists():
        prev_dur = chunk_starts[k] - chunk_starts[k - 1]
        window = cur.with_name(f"window_{k:05d}.ts")
        concat_ts([chunks[k - 1], cur], window)
        lo = max(0.0, prev_dur - a.overlap)
        base = chunk_starts[k - 1]                     # window time 0 = start of chunk k-1
    else:
        window, lo, base = cur, 0.0, chunk_starts[k]
    hi = (probe_duration(window) or (lo + dur))
    t0 = time.monotonic()
    tr = transcribe(window, a.model, window.with_suffix(".transcript.json"), (lo, hi))
    clips = pick_moments(window, tr, lo, hi, a.top_per_chunk * 4, a.min, a.max, keywords)
    fresh = []
    for c in clips:
        s, e = base + c.start, base + c.end
        if any(s < pe + GAP_BUFFER and e > ps - GAP_BUFFER for ps, pe in picked):
            continue                                   # already clipped from the previous window
        fresh.append(c)
        picked.append((s, e))
        if len(fresh) >= a.top_per_chunk:
            break
    size = probe_size(window) if a.layout == "split" else (0, 0)
    rows = []
    for c in fresh:
        moment = started + timedelta(seconds=base + c.start)
        name = f"{channel_slug(a.source)}_{moment:%Y%m%d_%H-%M-%S}.mp4"
        clip_lay, used = layout_for_clip(window, c, a, lay, cam_boxes, size)
        render(window, c, out / name, all_words(tr), clip_lay, not a.no_captions)
        rows.append({"file": name, "moment_time": f"{moment:%Y-%m-%d %H:%M:%S}",
                     "stream_offset": hms(base + c.start), "duration": round(c.dur, 1), "score": c.score,
                     "layout": used, "snippet": c.snippet})
        log(f"[live]   + {name} ({c.dur:.0f}s, {used}): {c.snippet[:60]}")
    if rows:
        new = not csv_path.exists()
        with csv_path.open("a", newline="", encoding="utf-8-sig" if new else "utf-8") as f:
            wr = csv.DictWriter(f, fieldnames=fields)
            if new:
                wr.writeheader()
            wr.writerows(rows)
    log(f"[live] chunk {k + 1} ({hms(chunk_starts[k])}-{hms(chunk_starts[k] + dur)} into the recording): "
        f"{len(rows)} clip(s), processed in {time.monotonic() - t0:.0f}s")
    # free disk: the previous chunk is no longer needed, nor are this window's temp files
    if not a.keep_chunks:
        if k:
            chunks[k - 1].unlink(missing_ok=True)
        if window != cur:
            window.unlink(missing_ok=True)
        window.with_suffix(".transcript.json").unlink(missing_ok=True)
    return len(rows)


def _video_id(url: str) -> str | None:
    """Best-effort video id for download caching (YouTube ids, Twitch VOD numbers)."""
    m = re.search(r"(?:v=|youtu\.be/|shorts/|/live/)([A-Za-z0-9_-]{11})", url) \
        or re.search(r"twitch\.tv/videos/(\d+)", url)
    return m.group(1) if m else None


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    if sys.version_info < (3, 11):
        log(f"[warn] Python {sys.version_info.major}.{sys.version_info.minor} detected; clipper targets 3.11+ "
            "(yt-dlp is dropping older versions). See README.")
    p = argparse.ArgumentParser(prog="clipper.py", description="Long video -> vertical shorts, fully local.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def clip_options(sp, default_model: str) -> None:
        sp.add_argument("--min", type=float, default=20, help="min clip length s (default 20)")
        sp.add_argument("--max", type=float, default=45, help="max clip length s (default 45)")
        sp.add_argument("--model", default=default_model,
                        help=f"faster-whisper model: tiny/base/small/medium/large-v3 (default {default_model})")
        sp.add_argument("--layout", choices=["blur", "crop", "split"], default="blur",
                        help="blur: full frame on blurred bg; crop: 9:16 slice; split: facecam on top, gameplay below")
        sp.add_argument("--crop-x", type=float, default=0.5,
                        help="crop/split: gameplay crop position, 0=left, 0.5=centre, 1=right")
        sp.add_argument("--facecam", help='split: facecam box "x,y,w,h" (0-1 fractions or pixels) or a saved name')
        sp.add_argument("--split-top", type=float, default=SPLIT_TOP,
                        help=f"split: share of the height for the facecam panel (default {SPLIT_TOP})")
        sp.add_argument("--no-facecam-check", action="store_true",
                        help="split: always use the first facecam box (skip per-clip detection/blur fallback)")
        sp.add_argument("--no-captions", action="store_true")
        sp.add_argument("--keywords", default="", help='extra keywords, comma-separated: "chat,clip it"')
        sp.add_argument("--work", default="work", help="download/cache dir (default work)")

    r = sub.add_parser("run", help="download/transcribe/pick/render")
    r.add_argument("source", help="URL (yt-dlp) or local video file")
    r.add_argument("--top", type=int, default=8, help="number of clips (default 8)")
    clip_options(r, "small")
    r.add_argument("--section", metavar="START-END",
                   help='only process part of the video, e.g. "1:00:00-1:40:00" or "2:15:00-" (to the end). '
                        "URLs download just that part; clip names/CSV keep the full VOD's timestamps")
    r.add_argument("--transcript", help="use this transcript JSON instead of running Whisper")
    r.add_argument("--out", default="clips", help="output dir (default clips)")
    r.add_argument("--dry-run", action="store_true", help="only print the picks")

    lv = sub.add_parser("live", help="record a live stream and clip it in rolling chunks while it's live")
    lv.add_argument("source", help="channel URL: https://www.twitch.tv/NAME, https://www.youtube.com/@NAME/live, "
                                   "https://kick.com/NAME")
    clip_options(lv, "base")
    lv.add_argument("--chunk", type=float, default=10, help="minutes per chunk (default 10): lower = faster clips")
    lv.add_argument("--overlap", type=int, default=60,
                    help="seconds of the previous chunk re-checked so boundary moments aren't lost (default 60)")
    lv.add_argument("--top-per-chunk", type=int, default=2, help="max clips per chunk (default 2)")
    lv.add_argument("--max-minutes", type=float, default=0, help="stop recording after this many minutes")
    lv.add_argument("--wait", action="store_true",
                    help="if the channel is offline (or the stream ends), keep checking until it goes live")
    lv.add_argument("--wait-interval", type=int, default=120, help="seconds between --wait checks (default 120)")
    lv.add_argument("--recorder", choices=["auto", "streamlink", "ytdlp"], default="auto",
                    help="auto = streamlink if installed (filters Twitch ads), else yt-dlp")
    lv.add_argument("--keep-chunks", action="store_true", help="keep the recorded chunks (uses ~2 GB/hour)")
    lv.add_argument("--out", help="output dir (default live_clips/<channel>)")

    f = sub.add_parser("facecam", help="preview/find the facecam box for --layout split and save it per streamer")
    f.add_argument("source", help="local video or URL (a URL downloads only ~4 s around --at)")
    f.add_argument("--live", action="store_true", help="source is a live channel: grab a frame from the stream now")
    f.add_argument("--at", default="60", help="timestamp to grab the frame from (default 60 = 1:00)")
    f.add_argument("--box", help='facecam box to check, "x,y,w,h" (fractions or pixels)')
    f.add_argument("--save", metavar="NAME",
                   help="add --box to the streamer's preset (a preset can hold several positions, "
                        "one per stream scene); clipper picks the right one per clip")
    f.add_argument("--reset", action="store_true", help="with --save: replace the preset instead of adding")
    f.add_argument("--no-snap", action="store_true", help="don't snap --box to the overlay's edges")
    f.add_argument("--crop-x", type=float, default=0.5, help="gameplay crop position for the split preview")
    f.add_argument("--split-top", type=float, default=SPLIT_TOP, help="facecam panel share for the preview")
    f.add_argument("--work", default="work", help="download dir for URLs (default work)")
    f.add_argument("--out", default="facecam_preview", help="where to write the preview PNGs")
    a = p.parse_args(argv)
    try:
        if a.cmd == "facecam":
            if a.save and not a.box:
                raise ClipperError("--save needs --box x,y,w,h")
            return cmd_facecam(a)
        if not 0.15 <= a.split_top <= 0.7:
            raise ClipperError("--split-top should be between 0.15 and 0.7")
        if a.min <= 0 or a.max < a.min:
            raise ClipperError("--min must be > 0 and --max must be >= --min")
        return cmd_live(a) if a.cmd == "live" else cmd_run(a)
    except ClipperError as e:
        print(f"\n[error] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
