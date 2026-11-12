# clipper: long video → vertical shorts (free, local, no watermark)

`clipper.py` downloads a stream VOD or video, or takes a local file. It transcribes the audio locally with faster-whisper and scores every moment by **audio loudness + reaction words**, without using an LLM. Then it renders the best moments as **1080×1920 H.264 shorts** with burned-in, word-highlighted captions. There are no API keys, no accounts and no paid services; everything runs on your machine.

## Install

1. Python 3.11+ (yt-dlp is dropping 3.10). With pyenv, the repo's `.python-version` selects 3.11.9 automatically. Then:
   ```bash
   pip install -r requirements.txt
   ```
2. ffmpeg (provides `ffmpeg` and `ffprobe`):
   - Windows: `winget install Gyan.FFmpeg` (then open a new terminal; clipper also finds winget installs that aren't on PATH yet)
   - macOS: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`

The first transcription downloads the Whisper model (tiny ≈ 75 MB, small ≈ 480 MB) into the Hugging Face cache.

## Examples

```bash
# 8 clips of 20–45 s from a VOD, blurred-background layout, small model
python clipper.py run "https://www.youtube.com/watch?v=VIDEO_ID"

# local file, centre-crop layout shifted right, streamer catchphrases, faster model
python clipper.py run stream.mp4 --layout crop --crop-x 0.65 --keywords "chat,clip it,we're so back" --model base

# preview the picks only (reuses cached download + transcript), shorter clips
python clipper.py run stream.mp4 --min 15 --max 30 --top 12 --dry-run

# long VOD: only download + process 1h00-1h40 (clip names keep the VOD's real timestamps)
python clipper.py run "https://www.twitch.tv/videos/123456789" --section 1:00:00-1:40:00 --model base
```

### Long streams: use `--section`

A 10-hour VOD is many GB to download and hours of CPU transcription. Use `--section START-END` (`1:00:00-1:40:00`, `45:00-1:30:00`, or `2:15:00-` for "to the end") to process it in chunks:

- **URL**: yt-dlp downloads only that part. The cuts are re-encoded so they are frame-accurate, and each section is cached separately in `work/`.
- **Local file**: nothing is cut. Only that range's audio is decoded, transcribed and scored, which also keeps memory use low.
- Clip filenames and `clips.csv` always show the time in the **original VOD**, so `clip_01_1-21-05.mp4` really is at 1:21:05 in the stream.

Running `--model base` on 40 minutes of stream took about 7 minutes on a laptop CPU, including rendering 8 clips.

Output goes to `clips/clip_01_0-12-34.mp4` and so on, plus `clips/clips.csv` (file, start, end, score, transcript snippet for your post caption). Downloads and transcripts are cached in `work/`, so reruns with different settings skip both steps.

## Tuning tips

- **`--min` / `--max`**: the scoring window is about (min+max)/2 seconds. Use 15–30 for punchy TikTok/Shorts clips, and 30–60 when you need context, such as a full chase.
- **`--keywords`**: add the streamer's catchphrases, and names of crew members or places they shout. To change them permanently, edit `KEYWORDS` at the top of `clipper.py`.
- **`--layout blur`** (default) keeps the whole gameplay frame, which is safest for HUD and minimap. **`--layout crop`** fills the screen. Use `--crop-x` (0 = left, 1 = right) to follow the action or the facecam.
- **`--layout split --facecam NAME`** puts the facecam on top and the gameplay below, with captions on the seam. Presets for `kai`, `lacy` and `stableronaldo`/`ron` are included. Each clip is checked automatically: clipper picks the saved cam position that's actually on screen and falls back to blur if there isn't one. To add a streamer, run `python clipper.py facecam <video_or_url> --at 1:00 --box x,y,w,h --save NAME`; rough boxes snap to the cam's edges. Details are in [HOW_TO_USE.md](HOW_TO_USE.md#4-split-layout-facecam-on-top-gameplay-below).
- **`--model`**: `tiny`/`base` are fast on CPU, `small` is the best default, and `medium` is more accurate but slow.
- **`--transcript file.json`** skips Whisper entirely. Use it for re-tuning, or with a transcript you already have.
- Scoring weights (`W_ENERGY`, `W_TEXT`, `GAP_BUFFER` …) are constants at the top of the file.
- On a low-RAM machine, rendering 1080×1920 uses a few hundred MB. Close other apps if ffmpeg reports `Cannot allocate memory`.

## Please

- **Only clip footage the campaign allows**, and follow its rules on length, credit and tags.
- **Review every clip by hand before posting.** The picker is a heuristic, and loud does not always mean good. Check that the captions are correct, especially names and slang that Whisper mishears.
- This tool deliberately has **no auto-posting or upload automation**.

## Testing

```bash
python tests/make_synthetic.py
python clipper.py run tests/synthetic.mp4 --transcript tests/synthetic.transcript.json --dry-run
```
The synthetic video has a loud burst and excited speech at 40–48 s, and the top pick should cover it.
