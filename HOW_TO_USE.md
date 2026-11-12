# How to use clipper

This is a step-by-step guide to turning a long GTA stream into vertical shorts you can post. For install details and how scoring works, see [README.md](README.md).

---

## 1. One-time setup

You need **Python 3.11 or newer**. If you use pyenv, this folder's `.python-version` file switches you to 3.11.9 automatically:

```bash
pyenv install 3.11.9
```

```bash
python --version
```

It should print `Python 3.11.9` when run inside this folder. Then:

```bash
pip install -r requirements.txt
```

You also need **ffmpeg**:

| OS | Command |
|---|---|
| Windows | `winget install Gyan.FFmpeg`, then **open a new terminal** |
| macOS | `brew install ffmpeg` |
| Linux | `sudo apt install ffmpeg` |

Check that everything works with the built-in test (takes about 30 seconds and needs no internet):

```bash
python tests/make_synthetic.py
python clipper.py run tests/synthetic.mp4 --transcript tests/synthetic.transcript.json --dry-run
```

The #1 pick should start around `0:00:39` with the "OH MY GOD NO WAY BRO" line.

---

## 2. The workflow (every stream)

### Step 1: Find the VOD and pick a chunk

Open the stream VOD (YouTube or Twitch) and note roughly where the action is. Process it in **30–60 minute chunks** with `--section` rather than the whole 10-hour stream.

### Step 2: Preview the picks (fast, nothing rendered)

```bash
python clipper.py run "VOD_URL" --section 1:00:00-1:40:00 --model base --dry-run
```

The first run downloads that chunk and transcribes it (about 5–7 min for 40 min of stream). After that it's cached, so later runs on the same section take seconds.

You get a table like:

```
 #     start       end    len   score  snippet
 1   1:21:05   1:21:39   33.5   0.951  Oh my god. Like bow. Yo, I can watch Spider-Man...
 2   1:32:40   1:33:13   32.7   0.770  I ain't gonna crash don't worry, chat...
```

The times are the **real VOD timestamps**, so you can jump to them in the VOD to check.

### Step 3: Tune until the list looks good

Rerun `--dry-run` with changes (each rerun is near-instant):

- Too much in-game dialogue or music? Add the streamer's own words: `--keywords "chat,clip it,on god,dawg"`
- Want shorter, punchier clips? Use `--min 15 --max 30`
- Want more options to choose from? Use `--top 15`

### Step 4: Render

Run the same command without `--dry-run`:

```bash
python clipper.py run "VOD_URL" --section 1:00:00-1:40:00 --model base --keywords "chat,clip it"
```

Output:

```
clips/
  clip_01_1-21-05.mp4    <- 1080x1920, captions burned in, ready for Shorts/TikTok/Reels
  clip_02_1-32-40.mp4
  ...
  clips.csv              <- file, start, end, score, transcript snippet
```

### Step 5: Review every clip by hand

Watch each one before posting:

- Is it actually a good moment? Loud does not always mean funny.
- Are the captions right? Whisper mishears names and slang, and in-game NPC dialogue gets captioned too.
- Does it follow the campaign's rules on length, credit/tags, swearing, and which footage is allowed?

Delete the bad ones. The `snippet` column in `clips.csv` is a good starting point for the post caption.

### Step 6: Post manually

Clipper never uploads anything. You post the clips yourself.

---

## 3. Options cheat sheet

| Option | Default | What it does |
|---|---|---|
| `--section 1:00:00-1:40:00` | whole video | Only process this part. `2:15:00-` means "to the end". |
| `--top 8` | 8 | How many clips to make |
| `--min 20` / `--max 45` | 20 / 45 | Clip length range in seconds |
| `--model base` | small | Whisper model: `tiny` (fastest) → `base` → `small` → `medium` (most accurate, slow) |
| `--layout blur` | blur | Full frame over a blurred background (keeps HUD, minimap and facecam) |
| `--layout crop` | | Fills the screen with a 9:16 slice |
| `--layout split` | | Facecam on top, gameplay below (needs `--facecam`, see section 4) |
| `--facecam kai` | | Split: a saved preset name, or a box `x,y,w,h` |
| `--split-top 0.4` | 0.4 | Split: share of the height for the facecam |
| `--crop-x 0.5` | 0.5 | Where to cut the gameplay for `crop`/`split`: `0` = left edge, `1` = right edge |
| `--keywords "a,b"` | | Extra hype words or phrases to boost |
| `--no-captions` | | Skip burned-in captions |
| `--dry-run` | | Print the picks and render nothing |
| `--transcript f.json` | | Use an existing transcript instead of Whisper |
| `--work work` / `--out clips` | | Cache folder / output folder |

To change the keywords permanently, edit the `KEYWORDS` list at the top of `clipper.py`.

---

## 4. Split layout (facecam on top, gameplay below)

This is the classic streamer-shorts format. You set the facecam box **once per streamer** and reuse it.

**1. Get a grid screenshot** from a moment where the facecam is visible. This works with a local file or a URL; a URL downloads only about 4 seconds of video.

```bash
python clipper.py facecam "VOD_URL_or_file" --at 1:00:00
```

Open `facecam_preview/facecam_grid.png`. The thin lines are every 10% of the frame and the red lines are at 50%. Note the facecam's left, top, width and height. Fractions work (`0.0,0.25,0.2,0.2`), and so do source pixels (`6,268,388,216` on a 1920x1080 stream).

**2. Check the box and save it under the streamer's name:**

```bash
python clipper.py facecam "VOD_URL_or_file" --at 1:00:00 --box 6,268,388,216 --save kai
```

Check `facecam_preview/facecam_split_preview.png`. If the face is cut off, adjust `--box` and run it again. `--crop-x` moves the gameplay crop left or right in the preview too.

**3. Render with the preset:**

```bash
python clipper.py run "VOD_URL" --section 1:00:00-1:40:00 --model base --layout split --facecam kai
```

- `--split-top 0.4` (default) gives the facecam 40% of the height. Use `0.33` for a smaller cam.
- Captions sit on the seam between the two panels.
- Presets are stored in `facecams.json`.
- **No facecam?** Many GTA RP streamers (e.g. NoPixel) don't show one. Use `--layout blur` or `crop` for them.
- If the streamer moves their facecam between streams, save a new preset.

---

## 5. Recipes

**Whole stream in chunks** (use a different `--out` per chunk so files don't mix):

```bash
python clipper.py run "VOD_URL" --section 0:00:00-0:45:00 --model base --out clips/part1
python clipper.py run "VOD_URL" --section 0:45:00-1:30:00 --model base --out clips/part2
```

**Re-render with a different layout** (no re-download or re-transcribe; the cache is reused):

```bash
python clipper.py run "VOD_URL" --section 1:00:00-1:40:00 --layout crop --crop-x 0.4 --out clips_crop
```

**A file you already have on disk:**

```bash
python clipper.py run "D:\recordings\stream.mp4" --section 30:00-1:10:00 --model base
```

---

## 6. Troubleshooting

| Problem | Fix |
|---|---|
| `'ffmpeg' not found` | Install it (section 1), then open a **new** terminal |
| `Cannot allocate memory` from ffmpeg | Close other apps (browser tabs, games). Rendering 1080x1920 needs a few hundred MB free. |
| `Download failed` | Run `pip install -U "yt-dlp[default]"`. Sites change often. |
| `403 Forbidden` during a download | Usually temporary on YouTube's side. Run the same command again. |
| yt-dlp warns about a "JS challenge" or "n challenge" | Install the extras with `pip install -U "yt-dlp[default]"`. YouTube also needs a JS runtime such as [Deno](https://deno.com). |
| Picks are mostly cutscenes or music | Add `--keywords` with the streamer's catchphrases, and use `--section` to skip mission-heavy parts |
| Captions misspell names | Expected with small models. Try `--model small`, and fix it in your editor if it matters. |
| Transcription is slow | Use `--model base` or `tiny`, and smaller `--section` chunks |
| Windows + pyenv: `The system cannot find the path specified` when running **yt-dlp directly** with `<` or `>` in arguments | That's the pyenv `.bat` shim, not clipper. Run yt-dlp through `clipper.py`, or call `python.exe` by its full path. |

---

## 7. Where files go and cleanup

- `work/`: downloaded VOD chunks (`*.mp4`, about 1.3 GB per 40 min at 1080p) and cached transcripts (`*.transcript.json`). **Safe to delete** once you're done with a stream; it gets re-downloaded if needed.
- `clips/`: your finished shorts and `clips.csv`.
- The Whisper model is cached in your user folder (`~/.cache/huggingface`), downloaded once per model size.
