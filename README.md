# Clip Studio

A single-file Twitch stream clipper with a built-in video editor. It finds the best moments in a live stream or a VOD (chat spikes plus streamer loudness), cuts them into vertical 9:16 clips, adds captions, and lets you polish each clip before exporting.

Everything lives in one file: `clip_studio.py`. It has a window (tkinter) and a command line.

> Only clip creators who allow it, and credit them. Music, sound effects and voices have their own licences, so check them before you post a monetized video.

---

## What it does

**Finding clips**
- Watches a live stream in real time (`streamlink` + `ffmpeg` rolling buffer) or analyses a VOD, using chat activity and the streamer's loudness to score moments.
- Works on several streams at once, one tab per stream.
- Can use Claude to trim, title and write a hook for each clip (optional, needs an Anthropic API key).

**Editing each clip** (the review window, with a live preview)
- Captions from Whisper. Click, drag a box over, or delete words. Drag the captions on the preview to move them. Change the look in *Customize captions*.
- A hook title shown at the start. Press Enter for a new row, and the rows stack. It can blur the background behind it and fade out, and you choose how long it stays.
- Blur boxes, text, and pictures, GIFs or video overlays with fade in/out and speed.
- Facecam on top with gameplay below. You can move or zoom the facecam slowly, switch it off, or give each clip its own facecam box.
- Video options: clip size and position, blurred borders on or off for 16:9 clips in 9:16, clip speed.
- Join clips together, cut parts out, split, and undo.
- Timeline with a row for each thing. Bars snap to the playhead, cuts and clip edges, and you can drag a box around several bars to select them.

**Sound**
- Clip volume, fades and loudness evening.
- Voice focus (removes game sound around speech) and Chipmunk and Deep voice parts that fade in and out.
- Music from the web: the Shorts top 15, popular songs (Apple previews), search, and free music with licences that allow commercial use.
- Sound effects from MyInstants (soundboard buttons).
- Text to speech with several voices and voice styles.
- Preview the sound in the editor before you export.

---

## Install

You need Python 3.9 or newer and `ffmpeg` on your PATH (ffmpeg 6 or newer is what it was tested with).

```
python -m pip install numpy requests faster-whisper yt-dlp streamlink anthropic
```

Or use the included list: `python -m pip install -r requirements.txt`

Optional extras:

| Extra | What it gives you | Install |
|---|---|---|
| `tkinterdnd2` | Drag files into the windows | `python -m pip install tkinterdnd2` |
| `demucs` | Better AI voice separation for Voice focus | `python -m pip install demucs` |
| `edge-tts` | Extra online voices for text to speech (needs internet) | `python -m pip install edge-tts` |
| TwitchDownloaderCLI | The best chat and VOD downloader | download it and pass `--tdl path` |
| `espeak-ng` (Linux) | Text to speech voices on Linux | your package manager |

tkinter comes with most Python installs. If the window won't open, install the tkinter package for your system. The command-line features work without it.

Check your setup at any time:

```
python clip_studio.py doctor
python clip_studio.py doctor --load-model
```

---

## Run it

Open the window:

```
python clip_studio.py
```

1. Enter a channel (or a VOD), pick your settings and start watching.
2. Tick **Review each clip** to preview and edit every clip before any AI credits are spent.
3. Open a clip in the review window, edit it, then export.

Your settings are saved in `clip_studio_settings.json` next to where you run the app. Paste your Anthropic API key in the window, or set `ANTHROPIC_API_KEY`.

### Command line

```
python clip_studio.py clip --vod 2345678901 --analyze-only            # list the best moments
python clip_studio.py clip --vod 2345678901 --top 8 --credit @streamer # cut vertical clips with captions
python clip_studio.py clip --chat chat.json --video stream.mp4 --audio --ai
python clip_studio.py chat vod 2345678901 chat.json                    # download a VOD's chat
python clip_studio.py chat live channelname chat.json                  # record live chat (Ctrl+C to stop)
python clip_studio.py captions myvideo.mp4 --preset Boxed              # test captions on any video
python clip_studio.py facecam stream.mp4                               # test facecam detection and layout
```

Useful options for `clip`: `--top`, `--before`, `--after`, `--gap`, `--layout blur|crop|facecam`, `--cam x,y,w,h`, `--landscape`, `--no-captions`, `--ai`, `--credit`, `--caption-preset`, `--whisper-model`, `--language`, `--out`. Run `python clip_studio.py clip --help` for all of them.

---

## Using the editor

| To do this | Do this |
|---|---|
| Move the playhead | Click the ruler or the preview controls |
| Move captions | Click them on the preview and drag; **↺ Reset caption position** puts them back |
| Cut a clip in two | Put the playhead on it and press **✂ Cut here** (or Ctrl+B). Delete a piece to cut it out; removed parts show red |
| Join another clip | **＋ Join another clip onto this one...**, then drop or pick the file |
| Change clip speed | Double-click a clip bar, or **⏩ Clip speed...** in the Video section |
| Move or zoom the facecam | **Adjust facecam...**, then **Camera movement** |
| Add a sound effect | **💥 Sound effects (MyInstants)...**, click to hear, **＋** to add |
| Add text to speech | **🗨 Text to speech...**, pick a voice and style, **Add to the clip** |
| Edit a bar | Double-click it. Drag its ends to resize; **🗑 Delete** or the Delete key removes it. Ctrl+C / Ctrl+V copy a bar |
| Undo | Ctrl+Z |

Bars snap to the playhead, cuts and clip edges; a dashed yellow guide shows what they're snapping to.

---

## Tips and limits

- **Chat needed:** moment finding works best with chat data. Without chat, use `--audio` for loudness only.
- **Whisper model:** `medium` is accurate but slow on a CPU. Use `small` or `base` to go faster.
- **Portrait sources:** a 9:16 source video is passed through instead of getting a facecam layout.
- **Joined clips:** each clip is fitted to the same frame. Enlarging one can look a little soft.
- **No transitions yet:** joined clips cut straight from one to the next, with no crossfade.
- **Web features** (music search, MyInstants, online voices) need internet. If one tab fails to load, the window shows the reason.
- **Text to speech** uses the voices already on your computer (Windows, Mac or Linux). If none are found, the window says what to install.
- **Sound effects and music** come from third parties. Free music is the safest for monetized posts.

---

## Files in this folder

| File | What it is |
|---|---|
| `clip_studio.py` | The whole app (use this one) |
| `requirements.txt` | Python packages to install |
| `clipper.py`, `live_clipper_gui.py`, `moments.py`, `peak_detector.py`, `stream_buffer.py`, `twitch_chat.py` | Earlier separate versions of the same ideas; `clip_studio.py` replaces them |
