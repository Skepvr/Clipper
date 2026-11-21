#!/usr/bin/env python3
"""clip_studio.py - everything in one file: Twitch chat-spike clipper + live GUI.

  python clip_studio.py                       # open the live GUI (watch a stream in real time)
  python clip_studio.py clip --vod 2345678901 --analyze-only      # list the best moments (free, seconds)
  python clip_studio.py clip --vod 2345678901 --top 8 --credit @streamer     # cut vertical clips + captions
  python clip_studio.py clip --chat chat.json --video stream.mp4 --audio --ai
  python clip_studio.py chat vod 2345678901 chat.json             # just download a VOD's chat
  (GUI: tick 'Review each clip' to preview every clip + a live edited preview before any AI credits are spent)
  python clip_studio.py chat live channelname chat.json           # record live chat (Ctrl+C to stop)
  python clip_studio.py doctor [--load-model]                     # check your setup / fix problems
  python clip_studio.py captions myvideo.mp4 --preset Boxed       # test captions on any video
  python clip_studio.py facecam stream.mp4                        # test facecam detection + the 9:16 cam-on-top layout

Needs: Python 3.9+, ffmpeg on PATH, `pip install numpy requests faster-whisper yt-dlp streamlink anthropic`.
Optional: TwitchDownloaderCLI (best chat/VOD downloader), ANTHROPIC_API_KEY (for --ai), tkinter (GUI).
Only clip creators who allow it, and credit them.
"""
import argparse, base64, csv, difflib, glob, json, os, queue, random, re, shutil, socket, subprocess, sys, tempfile, threading, time, urllib.parse, webbrowser
import statistics as st
from collections import Counter, deque
import numpy as np
import requests
try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import colorchooser, filedialog, scrolledtext, simpledialog
except ImportError:            # CLI still works without tkinter
    tk = scrolledtext = filedialog = colorchooser = tkfont = simpledialog = None
try:                           # optional: lets you DRAG files into the windows (python -m pip install tkinterdnd2)
    from tkinterdnd2 import TkinterDnD, DND_FILES
    HAVE_DND = True
except Exception:
    TkinterDnD = DND_FILES = None; HAVE_DND = False
VIDEO_EXT = (".mp4", ".mkv", ".mov", ".ts", ".webm", ".avi", ".flv", ".m4v")


def enable_drop(widget, callback, with_event=False):
    """Make `widget` accept files dragged onto it from Explorer/Finder. callback(list_of_paths). False if drag & drop isn't available."""
    if not HAVE_DND: return False
    try:
        widget.drop_target_register(DND_FILES)
        widget.dnd_bind("<<Drop>>", lambda e: callback([x for x in widget.tk.splitlist(e.data) if os.path.isfile(x)], *((e,) if with_event else ())))
        return True
    except Exception:
        return False

# ---- ffmpeg discovery: PATH -> usual install folders -> bundled copy (pip install imageio-ffmpeg) ----
def _ffmpeg_candidates():
    home, local = os.path.expanduser("~"), os.environ.get("LOCALAPPDATA", "")
    pats = [os.path.join(local, "Microsoft", "WinGet", "Packages", "Gyan.FFmpeg*", "**", "bin"),
            os.path.join(local, "Microsoft", "WinGet", "Links"), r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin",
            r"C:\ProgramData\chocolatey\bin", os.path.join(home, "scoop", "shims"),
            "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]
    out = []
    for pat in pats:
        out += glob.glob(pat, recursive=True) if "*" in pat else [pat]
    return [d for d in out if os.path.isdir(d)]


def ensure_ffmpeg():
    """Make `ffmpeg` callable by name. Returns True if found."""
    if shutil.which("ffmpeg"):
        return True
    for d in _ffmpeg_candidates():
        if shutil.which("ffmpeg", path=d):
            os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]; return True
    try:                                             # pip install imageio-ffmpeg  (bundled static ffmpeg)
        import imageio_ffmpeg
        cache = os.path.join(tempfile.gettempdir(), "clip_studio_ffmpeg"); os.makedirs(cache, exist_ok=True)
        dst = os.path.join(cache, "ffmpeg.exe" if os.name == "nt" else "ffmpeg")
        if not os.path.exists(dst):
            shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), dst); os.chmod(dst, 0o755)
        os.environ["PATH"] = cache + os.pathsep + os.environ["PATH"]; return True
    except Exception:
        return False


def setup_workdir():
    """Make sure we run from a writable folder (double-clicking can start us in C:\\Windows\\System32).
    Keeps your current folder when it's fine; otherwise uses the script's folder, then ~/ClipStudio."""
    def writable(d):
        try:
            os.makedirs(d, exist_ok=True)
            t = os.path.join(d, ".write_test"); open(t, "w").close(); os.remove(t)
            return True
        except OSError:
            return False
    cwd, win = os.getcwd(), os.environ.get("WINDIR", "").lower()
    if writable(cwd) and not (win and cwd.lower().startswith(win)):
        return cwd
    for d in (os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.expanduser("~"), "ClipStudio")):
        if writable(d) and not (win and d.lower().startswith(win)):
            os.chdir(d); return d
    return cwd



def folder_mb(path):
    """Total size of everything in a folder, in MB."""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try: total += os.path.getsize(os.path.join(root, f))
            except OSError: pass
    return total / 1e6


ensure_ffmpeg()

CLI_HELP = """Find the best chat-spike moments of a Twitch VOD, cut only those pieces, caption them, make them vertical."""



# ============================================================================
# 1. TWITCH CHAT (download VOD chat / record live chat)
# ============================================================================
GQL = "https://gql.twitch.tv/gql"
CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"
HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def download_vod_chat(video_id: str, progress=True):
    msgs, cursor = [], None
    while True:
        vars_ = {"videoID": str(video_id)}
        vars_["cursor" if cursor else "contentOffsetSeconds"] = cursor or 0
        body = [{"operationName": "VideoCommentsByOffsetOrCursor", "variables": vars_,
                 "extensions": {"persistedQuery": {"version": 1, "sha256Hash": HASH}}}]
        r = requests.post(GQL, json=body, headers={"Client-Id": CLIENT_ID}, timeout=30)
        r.raise_for_status()
        comments = r.json()[0]["data"]["video"]["comments"]
        for e in comments["edges"]:
            n = e["node"]
            text = "".join(f.get("text", "") for f in n["message"]["fragments"])
            msgs.append({"t": float(n["contentOffsetSeconds"]),
                         "user": (n.get("commenter") or {}).get("displayName", "?"), "text": text})
            cursor = e["cursor"]
        if progress and msgs:
            print(f"\rchat: {len(msgs)} msgs, at {msgs[-1]['t']/60:.0f} min", end="", flush=True)
        if not comments["pageInfo"]["hasNextPage"]:
            break
    print()
    return msgs


def record_live_chat(channel: str, out_path: str):
    s = socket.create_connection(("irc.chat.twitch.tv", 6667))
    for line in ("PASS SCHMOOPIIE", "NICK justinfan12345", f"JOIN #{channel.lower()}"):
        s.send((line + "\r\n").encode())
    start, msgs, buf = time.time(), [], ""
    try:
        while True:
            buf += s.recv(4096).decode("utf-8", "ignore")
            *lines, buf = buf.split("\r\n")
            for ln in lines:
                if ln.startswith("PING"):
                    s.send(b"PONG :tmi.twitch.tv\r\n")
                elif "PRIVMSG" in ln:
                    user = ln.split("!", 1)[0].lstrip(":")
                    text = ln.split("PRIVMSG", 1)[1].split(":", 1)[1]
                    msgs.append({"t": time.time() - start, "user": user, "text": text})
            if len(msgs) % 50 == 0:
                json.dump(msgs, open(out_path, "w"))
    except KeyboardInterrupt:
        pass
    json.dump(msgs, open(out_path, "w"))
    return msgs


def load_chat(path, with_length=False):
    """Loads our format, or a TwitchDownloaderCLI JSON export. with_length -> (msgs, vod_length_or_None)."""
    d = json.load(open(path))
    length = None
    if isinstance(d, list):
        msgs = d
    else:
        msgs = [{"t": c["content_offset_seconds"], "user": (c.get("commenter") or {}).get("display_name", "?"),
                 "text": c["message"]["body"]} for c in d["comments"]]
        v = d.get("video") or {}
        length = v.get("length") or v.get("end")
    return (msgs, length) if with_length else msgs


def get_chat(vod, out="chat.json", tdl=None):
    """Best-effort: TwitchDownloaderCLI (most reliable) -> our own GQL downloader."""
    import shutil, subprocess
    exe = tdl or shutil.which("TwitchDownloaderCLI")
    if exe:
        try:
            subprocess.run([exe, "chatdownload", "--id", str(vod), "-o", out], check=True)
            return load_chat(out, with_length=True)
        except Exception as e:
            print(f"TwitchDownloaderCLI failed ({e}); falling back to built-in downloader")
    msgs = download_vod_chat(re.sub(r".*videos/", "", str(vod)).split("?")[0])
    json.dump(msgs, open(out, "w"))
    return msgs, None


# ============================================================================
# 2. REAL-TIME PEAK DETECTOR (live heat score)
# ============================================================================
HYPE = re.compile(r"\b(kekw|omegalul|lul|lmao|lmfao|pog\w*|clip( it)?|no way|insane|wtf|monka\w*|holy|w)\b", re.I)


class PeakDetector:
    def __init__(self, trigger=3.0, window=8.0, baseline=300.0, cooldown=90.0, min_msgs=8, warmup=30.0):
        self.trigger, self.window, self.baseline = trigger, window, baseline
        self.cooldown, self.min_msgs, self.warmup = cooldown, min_msgs, warmup
        self.msgs, self.hist = deque(), deque()
        self.first = None
        self.last_trigger = -1e9

    def add(self, t, user, text):
        hype = bool(HYPE.search(text)) or (len(text) > 3 and text.isupper())
        self.msgs.append((t, user, int(hype)))

    @staticmethod
    def _z(x, history, floor):
        med = st.median(history)
        mad = st.median([abs(v - med) for v in history])
        return min(10.0, max(0.0, (x - med) / (1.4826 * mad + max(floor, 0.35 * med)))), med

    def tick(self, now):
        """Call ~2x/second. Returns dict with rate, uniq, hype, score, level (score/trigger), trigger."""
        self.first = self.first or now
        while self.msgs and self.msgs[0][0] < now - self.window:
            self.msgs.popleft()
        rate = len(self.msgs)
        uniq = len({m[1] for m in self.msgs})  # unique chatters: one person spamming doesn't count
        hype = sum(m[2] for m in self.msgs)
        self.hist.append((now, rate, uniq, hype))
        while self.hist and self.hist[0][0] < now - self.baseline:
            self.hist.popleft()
        base = [h for h in self.hist if h[0] < now - self.window]  # exclude the current burst itself
        out = dict(rate=rate, uniq=uniq, hype=hype, score=0.0, level=0.0, base_rate=0.0,
                   warming=True, trigger=False)
        if now - self.first < self.warmup or len(base) < 20:
            return out
        zr, med = self._z(rate, [h[1] for h in base], 2.0)
        zu, _ = self._z(uniq, [h[2] for h in base], 2.0)
        zh, _ = self._z(hype, [h[3] for h in base], 1.5)
        score = 0.5 * zr + 0.35 * zu + 0.15 * zh
        out.update(score=score, level=score / self.trigger, base_rate=med / self.window, warming=False)
        if score >= self.trigger and rate >= self.min_msgs and now - self.last_trigger >= self.cooldown:
            self.last_trigger = now
            out["trigger"] = True
        return out


class AudioHeat:
    """STREAMER HEAT: how much louder than normal the stream audio is right now.
    Short-term loudness (last `window` s, in dBFS) minus the rolling median loudness of the last ~5 min.
    Because it's relative to the stream's own normal level, a stream that is always loud doesn't trigger constantly,
    but a scream / big reaction (typically +10..20 dB over normal talking) does."""
    def __init__(self, rise=9.0, window=3.0, baseline=300.0, floor_db=-50.0, warmup=30.0, cooldown=90.0):
        self.rise, self.window, self.baseline, self.floor_db = rise, window, baseline, floor_db
        self.warmup, self.cooldown = warmup, cooldown
        self.frames = deque()          # (t, dBFS) ~10 per second
        self.first = None
        self.last_trigger = -1e9

    def add(self, t, db):
        self.first = self.first or t
        self.frames.append((t, max(-90.0, float(db))))

    def tick(self, now):
        while self.frames and self.frames[0][0] < now - self.baseline - self.window:
            self.frames.popleft()
        out = dict(db=-90.0, base_db=-90.0, rise=0.0, level=0.0, warming=True, trigger=False, silent=True)
        cur = [d for t, d in self.frames if t >= now - self.window]
        if not cur:
            return out
        short = float(np.mean(cur))
        out["db"] = short; out["silent"] = False
        base = [d for t, d in self.frames if t < now - self.window]
        if (self.first is None or now - self.first < self.warmup) or len(base) < 100:
            return out
        med = float(np.median(base))
        rise = short - med
        out.update(base_db=med, rise=rise, level=max(0.0, rise) / self.rise, warming=False)
        # needs: a real rise, audibly loud in absolute terms, and the stream isn't basically silent normally
        if rise >= self.rise and short > self.floor_db and med > self.floor_db - 10 and now - self.last_trigger >= self.cooldown:
            self.last_trigger = now
            out["trigger"] = True
        return out


# ============================================================================
# 3. WHOLE-VOD MOMENT ANALYSIS (chat spikes, ascii graph)
# ============================================================================
BARS = " ▁▂▃▄▅▆▇█"


def hms(s):
    s = max(0, int(round(s)))
    return f"{s//3600}:{s//60%60:02d}:{s%60:02d}"


def twitch_link(vod_id, t):
    t = int(max(0, t))
    return f"https://www.twitch.tv/videos/{vod_id}?t={t//3600}h{t//60%60}m{t%60}s"


def _local_median(x, around):
    return np.array([np.median(x[max(0, i - around): i + around + 1]) for i in range(len(x))])


def analyze(msgs, length=None, bucket=10, top=5, before=30, after=10, gap=90, audio=None, max_clip=90, min_x=2.5):
    length = max(length or 0, max(m["t"] for m in msgs))
    n = int(length // bucket) + 1
    raw, hype = np.zeros(n), np.zeros(n)
    users = [Counter() for _ in range(n)]
    for m in msgs:
        b = int(m["t"] // bucket)
        if 0 <= b < n:
            raw[b] += 1; users[b][m["user"]] += 1
            hype[b] += bool(HYPE.search(m["text"])) or (len(m["text"]) > 3 and m["text"].isupper())
    uniq = np.array([len(u) for u in users], float)
    cnt = np.array([sum(min(v, 3) for v in u.values()) for u in users], float)  # max 3 msgs per chatter per bucket
    around = max(1, round(300 / bucket))                       # compare with the 5 min on each side
    med_c, med_u = _local_median(cnt, around), _local_median(uniq, around)
    ratio = cnt / (med_c + 1)                                   # "x normal" (same idea as chat-clips)
    score = 0.6 * ratio + 0.4 * (uniq / (med_u + 1))            # unique chatters keep one spammer from winning
    score *= 1 + 0.5 * np.divide(hype, cnt, out=np.zeros(n), where=cnt > 0)
    if audio is not None and len(audio) >= n:
        a = np.asarray(audio[:n]); score += 0.25 * a / (_local_median(a, around) + 1e-6)

    peaks = []
    for i in np.argsort(-score):
        t = i * bucket
        if cnt[i] < 5 or ratio[i] < min_x or any(abs(p["t"] - t) < gap for p in peaks):
            continue
        thr = med_c[i] + 0.25 * (cnt[i] - med_c[i])
        a_ = i                                                  # walk back to where chat STARTED rising
        while a_ > 0 and cnt[a_ - 1] > thr and (i - a_) * bucket < 90: a_ -= 1
        b_ = i                                                  # ...and forward to where it calms down
        while b_ < n - 1 and cnt[b_ + 1] > thr and (b_ - i) * bucket < 60: b_ += 1
        start = max(0, a_ * bucket - before)
        end = min(length, (b_ + 1) * bucket + after)
        end = min(end, start + max_clip)
        lines = Counter(m["text"].strip().lower() for m in msgs if a_ * bucket <= m["t"] < (b_ + 1) * bucket and m["text"].strip())
        label = ", ".join(f"{k[:18]} x{v}" for k, v in lines.most_common(3))
        peaks.append(dict(t=float(t), onset=float(a_ * bucket), msgs=int(raw[i]), chatters=int(uniq[i]),
                          x_normal=round(float(ratio[i]), 1), score=round(float(score[i]), 2),
                          start=float(start), end=float(end), label=label))
        if len(peaks) >= top:
            break
    return sorted(peaks, key=lambda p: p["t"]), cnt


def ascii_graph(cnt, peaks, bucket, width=60):
    per = max(1, -(-len(cnt) // width))
    cols = [cnt[k * per:(k + 1) * per].max() for k in range(-(-len(cnt) // per))]
    mx = max(cols) or 1
    row = "".join(BARS[int(round(c / mx * 8))] for c in cols)
    marks = [" "] * len(cols)
    for p in peaks:
        marks[min(len(cols) - 1, int(p["t"] // bucket // per))] = "^"
    return row + "\n" + "".join(marks)


def report(peaks, cnt, msgs, length, bucket, vod_id=None):
    print(f"\n{len(msgs)} chat messages, {hms(length)} of stream\n")
    print(ascii_graph(cnt, peaks, bucket))
    print(f"0:00:00{' ' * 40}{hms(length)}\n")
    for k, p in enumerate(peaks, 1):
        link = f"  {twitch_link(vod_id, p['start'])}" if vod_id else ""
        print(f"#{k} {hms(p['t'])}  {p['msgs']} msgs/{p['chatters']} chatters in {bucket}s ({p['x_normal']}x normal)"
              f"  -> clip {hms(p['start'])} to {hms(p['end'])}\n     chat: {p['label']}{link}")


# ============================================================================
# 4. LIVE STREAM ROLLING BUFFER (streamlink + ffmpeg)
# ============================================================================
class StreamBuffer:
    def __init__(self, buf_dir="buffer", keep_secs=900, log=print, on_audio=None):
        self.dir, self.keep, self.log, self.on_audio = os.path.abspath(buf_dir), keep_secs, log, on_audio
        self.segs, self.offsets = [], []  # segs: (file, start, end) in stream-seconds
        self.stop_ev, self.lock = threading.Event(), threading.Lock()
        self.procs, self._seen = [], 0

    # wall-clock <-> stream-time mapping (min() because detection delay only ever adds)
    @property
    def offset(self):
        return min(self.offsets[-10:]) if self.offsets else None

    @property
    def buffered(self):
        with self.lock:
            return (self.segs[-1][2] - self.segs[0][1]) if self.segs else 0.0

    @staticmethod
    def _streamlink_cmd():
        """Find streamlink: the PATH command, or `python -m streamlink` (works even when pip's Scripts
        folder isn't on PATH, the usual Windows problem)."""
        import importlib.util
        if shutil.which("streamlink"):
            return ["streamlink"]
        if importlib.util.find_spec("streamlink"):
            return [sys.executable, "-m", "streamlink"]
        raise RuntimeError(f"streamlink isn't installed for this Python. Run:  \"{sys.executable}\" -m pip install streamlink")

    def start(self, channel, quality="720p60,720p,best"):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("ffmpeg not found. Easiest fix: run  python -m pip install imageio-ffmpeg  and restart this app. "
                               "Or install it: Windows  winget install Gyan.FFmpeg  |  Mac  brew install ffmpeg  (then close & reopen the app)")
        sl_cmd = self._streamlink_cmd()
        os.makedirs(self.dir, exist_ok=True)
        for f in glob.glob(os.path.join(self.dir, "seg_*.ts")) + glob.glob(os.path.join(self.dir, "list.csv")):
            os.remove(f)
        self.list_path = os.path.join(self.dir, "list.csv")
        sl = subprocess.Popen(sl_cmd + ["--twitch-low-latency", "--stdout", f"twitch.tv/{channel}", quality],
                              stdout=subprocess.PIPE, stderr=open(os.path.join(self.dir, "streamlink.log"), "w"))
        ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-i", "pipe:0", "-c", "copy", "-f", "segment",
                               "-segment_time", "4", "-segment_format", "mpegts", "-segment_list", self.list_path,
                               "-segment_list_type", "csv", "-reset_timestamps", "1",
                               os.path.join(self.dir, "seg_%06d.ts"),
                               # second output: tiny mono 8 kHz audio copy -> loudness meter (STREAMER HEAT)
                               "-map", "0:a:0?", "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "pipe:1"],
                              stdin=sl.stdout, stdout=subprocess.PIPE, stderr=open(os.path.join(self.dir, "ffmpeg.log"), "w"))
        sl.stdout.close()
        self.procs = [sl, ff]
        threading.Thread(target=self._audio_reader, args=(ff.stdout,), daemon=True).start()
        threading.Thread(target=self._poll, daemon=True).start()

    def _audio_reader(self, pipe):
        """Reads 100 ms chunks of raw audio and reports loudness in dBFS (0 = full scale). Always drains the pipe."""
        n = 1600  # 100 ms of 8 kHz s16 mono
        try:
            while not self.stop_ev.is_set():
                b = pipe.read(n)
                if not b:
                    return
                if self.on_audio and len(b) >= 2:
                    x = np.frombuffer(b[: len(b) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
                    rms = float(np.sqrt(np.mean(x * x))) if x.size else 0.0
                    self.on_audio(time.time(), 20 * np.log10(max(rms, 1e-5)))
        except Exception:
            return

    def _poll(self):
        while not self.stop_ev.is_set():
            try:
                lines = open(self.list_path).read().splitlines() if os.path.exists(self.list_path) else []
            except OSError:
                lines = []
            for ln in lines[self._seen:]:
                try:
                    name, s, e = ln.split(",")
                    with self.lock:
                        self.segs.append((os.path.join(self.dir, name), float(s), float(e)))
                        self.offsets.append(time.time() - float(e))
                except ValueError:
                    continue
            self._seen = len(lines)
            with self.lock:  # prune old segments so the disk doesn't fill up
                while self.segs and self.segs[-1][2] - self.segs[0][2] > self.keep:
                    try: os.remove(self.segs[0][0])
                    except OSError: pass
                    self.segs.pop(0)
            if self.procs[0].poll() is not None and self.procs[1].poll() is not None:
                tail = open(os.path.join(self.dir, "streamlink.log")).read()[-300:]
                self.log(f"Stream ended / not live. streamlink said: {tail.strip()}")
                return
            time.sleep(0.5)

    def cut(self, wall_start, wall_end, out_path):
        """Cut [wall_start, wall_end] (time.time() values) from the buffer into out_path (mp4)."""
        off = self.offset
        if off is None:
            raise RuntimeError("buffer not ready yet")
        s0, s1 = wall_start - off, wall_end - off
        with self.lock:
            use = [x for x in self.segs if x[2] > s0 and x[1] < s1 and os.path.exists(x[0])]
        if not use:
            raise RuntimeError("requested time is not in the buffer")
        start = max(s0, use[0][1])
        dur = min(s1, use[-1][2]) - start
        lst = out_path + ".txt"
        open(lst, "w").write("".join(f"file '{u[0].replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for u in use))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                        "-ss", f"{start - use[0][1]:.2f}", "-t", f"{dur:.2f}", "-c:v", "libx264", "-preset", "veryfast",
                        "-crf", "20", "-c:a", "aac", out_path], check=True)
        os.remove(lst)
        return out_path, dur

    def stop(self):
        self.stop_ev.set()
        for p in self.procs:
            try: p.terminate()
            except Exception: pass


# ============================================================================
# 5. CLIPPING: transcript, AI judge, captions, vertical render, CLI
# ============================================================================
JUDGE_MODEL = "claude-sonnet-5-5"


def sh(*a, **k):
    return subprocess.run(a, check=True, capture_output=True, **k)


def duration(video):
    if shutil.which("ffprobe"):
        return float(sh("ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", video).stdout)
    r = subprocess.run(["ffmpeg", "-i", video], capture_output=True, text=True)   # bundled ffmpeg has no ffprobe
    h, m, sec = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r.stderr).groups()
    return int(h) * 3600 + int(m) * 60 + float(sec)


def audio_bins(video, bucket):  # loudness per bucket over the WHOLE file (only possible with a local video)
    raw = sh("ffmpeg", "-i", video, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-").stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    per = 8000 * bucket
    return [float(np.sqrt(np.mean(a[i*per:(i+1)*per] ** 2) + 1e-9)) for i in range(len(a) // per + 1)]


# ---------- transcript + frames + AI judge ----------
# ---------- captions: style, hand-edited transcripts, Whisper ----------
class CaptionError(RuntimeError):
    """Raised with a plain-English message when captions can't be generated."""


LEGACY_CAPTION = dict(font="Impact", size=110, words=3, color="#FFFFFF", highlight="#FFFF00", outline_color="#000000",
                      outline=6, shadow=2, bold=False, upper=True, strip_punct=False, highlight_mode="color",
                      position="below", y_offset=560, box=False, hook_color="#FFE500", censor=False)
# the default look: big white words with a thick black outline, the word being said sits in a purple box, 3 words at a time,
# centred on the line where the facecam meets the gameplay, swear words shown as ****
CAPTION_DEFAULT = dict(font="Arial Black", size=80, words=3, color="#FFFFFF", highlight="#9146FF", outline_color="#000000",
                       outline=8, shadow=0, bold=False, upper=False, strip_punct=False, highlight_mode="box",
                       position="seam", y_offset=560, box=False, hook_color="#FF1F1F", censor=True)
CAPTION_PRESETS = {
    "TikTok Purple (default)": {},
    "Bold Yellow": dict(LEGACY_CAPTION),
    "Clean White": {**LEGACY_CAPTION, **dict(font="Arial", size=84, bold=True, words=5, upper=False, highlight_mode="none", outline=4, shadow=1)},
    "One Word Pop": {**LEGACY_CAPTION, **dict(words=1, size=150, highlight_mode="pop", highlight="#00FF66", outline=8)},
    "Boxed": {**LEGACY_CAPTION, **dict(font="Arial Black", size=78, words=4, upper=False, box=True, outline_color="#000000", outline=16, shadow=0, highlight="#FFD400")},
    "Neon": {**LEGACY_CAPTION, **dict(font="Arial Black", size=96, highlight="#00E5FF", outline_color="#6A00FF", outline=8, highlight_mode="both")},
    "Minimal": {**LEGACY_CAPTION, **dict(font="Segoe UI", size=70, words=6, color="#F5F5F5", highlight="#FFB300", upper=False, outline=3, shadow=1)},
}
HIGHLIGHT_MODES = {"Color": "color", "Pop (bigger)": "pop", "Color + Pop": "both", "Box behind the word": "box", "None": "none"}
POSITIONS = {"Below the video": "below", "Middle": "middle", "Top": "top", "Where cam meets gameplay": "seam"}
FONTS = ["Impact", "Arial Black", "Arial", "Verdana", "Segoe UI", "Trebuchet MS", "Comic Sans MS", "Georgia", "Courier New"]
WHISPER = dict(model="medium", language="en", device="cpu", clean=True, vocab="", vad=True)     # cpu always works; GPU needs extra NVIDIA libraries
SETTINGS_FILE = "clip_studio_settings.json"


FONTS_DIR = "clip_studio_fonts"          # imported font files live here; ffmpeg's subtitle renderer is pointed at this folder
FONT_EXTS = (".ttf", ".otf", ".ttc")
_REGISTERED_FONTS = set()


def font_family(path):
    """Family name stored inside a .ttf/.otf/.ttc file (what the caption style needs), or the file name if it can't be read."""
    import struct
    try:
        data = open(path, "rb").read()
        base = 0
        if data[:4] == b"ttcf": base = struct.unpack(">I", data[12:16])[0]                  # collection: use the first font
        ntab = struct.unpack(">H", data[base + 4: base + 6])[0]
        for i in range(ntab):
            tag, _, off, ln = struct.unpack(">4sIII", data[base + 12 + 16 * i: base + 28 + 16 * i])
            if tag == b"name": break
        else: raise ValueError("no name table")
        fmt, count, so = struct.unpack(">HHH", data[off: off + 6]); best = None
        for k in range(count):
            pid, eid, lid, nid, ln2, o2 = struct.unpack(">6H", data[off + 6 + 12 * k: off + 18 + 12 * k])
            if nid != 1: continue                                                              # 1 = font family
            raw = data[off + so + o2: off + so + o2 + ln2]
            txt = raw.decode("utf-16-be", "ignore") if pid in (0, 3) else raw.decode("mac_roman", "ignore")
            score = (pid == 3) * 2 + (lid in (0x409, 0)) + (pid == 3 and lid == 0x409)
            if txt.strip() and (best is None or score > best[0]): best = (score, txt.strip())
        if best: return best[1]
    except Exception:
        pass
    return os.path.splitext(os.path.basename(path))[0]


def register_fonts():
    """Windows: make imported fonts visible to this program's own windows too (only for this run; nothing is installed system-wide)."""
    if os.name != "nt" or not os.path.isdir(FONTS_DIR): return
    try:
        import ctypes
        for f in os.listdir(FONTS_DIR):
            fp = os.path.abspath(os.path.join(FONTS_DIR, f))
            if f.lower().endswith(FONT_EXTS) and fp not in _REGISTERED_FONTS:
                ctypes.windll.gdi32.AddFontResourceExW(fp, 0x10, 0); _REGISTERED_FONTS.add(fp)      # 0x10 = private to this process
    except Exception:
        pass


def import_font_file(path):
    """Copy a font file into the app's fonts folder. Returns the family name to use in the caption style."""
    if not path.lower().endswith(FONT_EXTS): raise ValueError("Pick a .ttf, .otf or .ttc font file.")
    os.makedirs(FONTS_DIR, exist_ok=True)
    dest = os.path.join(FONTS_DIR, os.path.basename(path))
    if os.path.abspath(path) != os.path.abspath(dest): shutil.copyfile(path, dest)
    register_fonts()
    return font_family(dest)


def imported_fonts():
    """{family name: file} for every font in the fonts folder."""
    out = {}
    if os.path.isdir(FONTS_DIR):
        for f in sorted(os.listdir(FONTS_DIR)):
            if f.lower().endswith(FONT_EXTS): out.setdefault(font_family(os.path.join(FONTS_DIR, f)), os.path.join(FONTS_DIR, f))
    return out


def _ff_path(p):
    return os.path.abspath(p).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")


def fonts_opt():
    """':fontsdir=...' for ffmpeg's ass filter when fonts have been imported (empty otherwise)."""
    return f":fontsdir='{_ff_path(FONTS_DIR)}'" if imported_fonts() else ""


def load_settings():
    try: return json.load(open(SETTINGS_FILE, encoding="utf-8"))
    except Exception: return {}


def save_settings(**kv):
    d = load_settings(); d.update(kv)
    try: json.dump(d, open(SETTINGS_FILE, "w", encoding="utf-8"), indent=2)
    except OSError: pass


def resolve_style(style=None):
    S = {**CAPTION_DEFAULT, **(style or {})}
    S["size"] = int(max(30, min(250, S["size"]))); S["words"] = int(max(1, min(8, S["words"])))
    S["outline"] = max(0, min(20, float(S["outline"]))); S["shadow"] = max(0, min(10, float(S["shadow"])))
    S["y_offset"] = int(max(0, min(1200, S["y_offset"]))); S["censor"] = bool(S.get("censor", False))
    if S["highlight_mode"] not in HIGHLIGHT_MODES.values(): S["highlight_mode"] = "color"
    if S["position"] not in POSITIONS.values(): S["position"] = "below"
    for k in ("cap_x", "cap_y"):                                   # a hand-placed caption centre (fraction of the picture); None = use the Position setting
        try: S[k] = max(0.0, min(1.0, float(S[k]))) if S.get(k) is not None else None
        except (TypeError, ValueError): S[k] = None
    if S["cap_x"] is None or S["cap_y"] is None: S["cap_x"] = S["cap_y"] = None
    return S


def _rgb(hexcol):
    h = str(hexcol).lstrip("#")
    return (h[0:2], h[2:4], h[4:6]) if len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h) else ("FF", "FF", "FF")


def ass_color(hexcol, alpha=0):           # style fields:  &HAABBGGRR
    r, g, b = _rgb(hexcol); return f"&H{alpha:02X}{b}{g}{r}".upper().replace("&H", "&H")


def ass_inline(hexcol):                   # override tags:  &HBBGGRR&
    r, g, b = _rgb(hexcol); return f"&H{b}{g}{r}&".upper().replace("&H", "&H")


def words_to_text(words):
    return " ".join(w["w"] for w in words)


def edit_words(words, text):
    """Apply a hand-corrected transcript to timed words. Same word count -> straight swap (timing kept).
    Added / removed / replaced words are re-timed inside the gap they sit in."""
    new = text.split()
    if not words: return []
    if len(new) == len(words):
        return [dict(w, w=t) for w, t in zip(words, new)]
    norm = lambda x: re.sub(r"[^\w']", "", x.lower())
    sm = difflib.SequenceMatcher(None, [norm(w["w"]) for w in words], [norm(x) for x in new], autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            out += [dict(words[i1 + k], w=new[j1 + k]) for k in range(i2 - i1)]
        elif tag in ("replace", "insert"):
            if tag == "replace": t0, t1 = words[i1]["s"], words[i2 - 1]["e"]
            else:
                t0 = words[i1 - 1]["e"] if i1 > 0 else words[0]["s"]
                t1 = words[i1]["s"] if i1 < len(words) else words[-1]["e"]
            n = j2 - j1; t1 = max(t1, t0 + 0.15 * n)
            for k in range(n):
                s = t0 + (t1 - t0) * k / n
                out.append(dict(s=s, e=s + (t1 - t0) / n, w=new[j1 + k]))
    for i in range(1, len(out)):                       # keep times in order
        out[i]["s"] = max(out[i]["s"], out[i - 1]["s"])
    return out


def get_whisper():
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise CaptionError("Captions need faster-whisper. Install it with:  python -m pip install faster-whisper   (then restart the app)")
    key = (WHISPER["model"], WHISPER["device"])
    if key not in _WHISPER_CACHE:
        try:
            _WHISPER_CACHE[key] = WhisperModel(WHISPER["model"], device=WHISPER["device"], compute_type="int8")
        except Exception as e:
            raise CaptionError(f"Could not load the Whisper '{WHISPER['model']}' model: {str(e)[:150]}. The first run downloads it "
                               "from Hugging Face (needs internet and a few hundred MB of space). Try model 'base' or 'tiny' if it's too big.")
    return _WHISPER_CACHE[key]


_WHISPER_CACHE = {}


SPEECH_CLEAN = "highpass=f=90,lowpass=f=7600,afftdn=nr=10:nf=-30,dynaudnorm=f=200:g=11:p=0.9"


def transcribe(video, start, end, on_words=None):
    """Speech -> [{'w','s','e'}] word timings (absolute seconds in the video). Local and free.
    ffmpeg decodes the audio and hands Whisper an ARRAY, so Whisper's own audio loader (PyAV) is never used. That avoids
    the "open() got an unexpected keyword argument 'metadata_errors'" error you get when faster-whisper and PyAV versions don't match."""
    model = get_whisper()
    base = ["ffmpeg", "-v", "error", "-ss", str(start), "-to", str(end), "-i", video, "-vn", "-ac", "1", "-ar", "16000"]
    # Clean-up before listening: cut rumble/hiss outside the voice range, reduce steady noise, even out loud and quiet speech.
    cleaners = ([["-af", SPEECH_CLEAN]] if WHISPER.get("clean", True) else []) + [[]]
    pcm = None
    for extra in cleaners:                      # if this ffmpeg lacks a filter, fall back to the plain audio
        try:
            pcm = sh(*base, *extra, "-f", "s16le", "-").stdout; break
        except subprocess.CalledProcessError as e:
            err = (e.stderr or b"").decode("utf-8", "ignore")[-150:]
    if pcm is None:
        raise CaptionError(f"ffmpeg couldn't read the audio from this clip: {err}")
    audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0
    if audio.size < 1600:
        return []                                        # under 0.1 s of audio
    try:
        kw = dict(word_timestamps=True, language=WHISPER["language"] or None, beam_size=5, best_of=5,
                  condition_on_previous_text=False,            # stops one wrong phrase from snowballing into the next sentences
                  initial_prompt=((WHISPER.get("vocab") or "").strip() or None))
        if WHISPER.get("vad", True):                           # lower threshold + padding so quiet speech over game audio isn't dropped
            kw.update(vad_filter=True, vad_parameters=dict(threshold=0.3, min_silence_duration_ms=400, speech_pad_ms=300))
        try: segs, _ = model.transcribe(audio, **kw)
        except TypeError:                                      # older faster-whisper without some options
            for k in ("best_of", "condition_on_previous_text", "vad_parameters"): kw.pop(k, None)
            segs, _ = model.transcribe(audio, **kw)
        out = []
        for sg in segs:                                  # (the model runs lazily, as we iterate)
            for w in (sg.words or []):
                if w.word.strip(): out.append({"w": w.word.strip(), "s": start + w.start, "e": start + w.end})
            if on_words:                                  # live: hand over everything heard so far, plus how far along we are (0..1)
                try: on_words(list(out), min(1.0, float(sg.end) / max(0.1, audio.size / 16000.0)))
                except Exception: pass
        return out
    except CaptionError:
        raise
    except Exception as e:
        msg = str(e)
        if "metadata_errors" in msg:
            raise CaptionError("Your 'av' (PyAV) package doesn't match this faster-whisper. Fix:  python -m pip install -U av faster-whisper  (then restart)")
        raise CaptionError(f"Transcription failed: {msg[:150]}")


def frames_b64(video, start, end, n=6):
    out = []
    for i in range(n):
        t = start + (end - start) * i / (n - 1)
        out.append(base64.b64encode(sh("ffmpeg", "-ss", str(t), "-i", video, "-frames:v", "1", "-vf", "scale=512:-1",
                                       "-f", "image2pipe", "-vcodec", "mjpeg", "-").stdout).decode())
    return out


def judge(video, c, words, msgs):
    """Claude can't hear audio, so it gets: frames + Whisper transcript + chat. Returns score/title/hook/start/end."""
    import anthropic
    chat_txt = "\n".join(f"[{m['t']-c['start']:.0f}s] {m['text']}" for m in msgs if c["start"] <= m["t"] <= c["end"])[:3000]
    prompt = f"""You're a short-form editor picking viral clips from a stream.
Window: {c['start']:.0f}s-{c['end']:.0f}s ({c['end']-c['start']:.0f}s). Frames sampled evenly across it are attached. You cannot hear audio; use the transcript.
TRANSCRIPT (seconds): {' '.join(f"{w['w']}({w['s']:.0f})" for w in words) or '(none)'}
CHAT (seconds from window start): {chat_txt or '(none)'}
Reply with ONLY JSON: {{"score":1-10,"title":"short title","hook":"max 6 words of on-screen text for the first 2 seconds, e.g. wait for it","reason":"why","start":abs_sec,"end":abs_sec}}
Trim to a self-contained moment (setup -> payoff), 15-60s, opening right before the action. Score <5 if it's just loud/quiet filler."""
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": f}} for f in frames_b64(video, c["start"], c["end"])]
    r = anthropic.Anthropic().messages.create(model=JUDGE_MODEL, max_tokens=500,
                                              messages=[{"role": "user", "content": content + [{"type": "text", "text": prompt}]}])
    return json.loads(re.search(r"\{.*\}", r.content[0].text, re.S).group())


# ---------- captions / hook / credit (ASS) ----------
def ass_time(t):
    t = max(0, t)
    return f"{int(t//3600)}:{int(t%3600//60):02d}:{t%60:05.2f}"


_SWEAR_STEMS = ("fuck", "shit", "bitch", "cunt", "nigg", "pussy", "whore", "slut", "bullshit", "dickhead", "asshole", "faggot")
_SWEAR_EXACT = {"ass", "asses", "dick", "dicks", "cock", "cocks", "fag", "fags", "tits", "twat", "wank", "prick"}


def censor_word(w):
    """Swear words become asterisks the same length (like '****'). Punctuation around the word is kept."""
    m = re.match(r"^(\W*)(.*?)(\W*)$", w, re.S)
    pre, core, post = m.group(1), m.group(2), m.group(3); low = core.lower().replace("'", "")
    if low and (low in _SWEAR_EXACT or any(st in low for st in _SWEAR_STEMS)): return pre + "*" * max(3, len(core)) + post
    return w


def make_ass(words, start, end, path, vertical, hook="", credit="", style=None, texts=None):
    """Write the subtitle file: word-by-word captions in the given style, plus optional hook + credit."""
    S = resolve_style(style)
    W, H = (1080, 1920) if vertical else (1920, 1080)
    fs = int(S["size"] * (1 if vertical else 0.73))
    align, mv = {"below": (2, S["y_offset"] if vertical else int(S["y_offset"] * 0.2)), "seam": (5, 0),
                 "middle": (5, 0), "top": (8, 90 if vertical else 40)}[S["position"]]
    hook_y, credit_y = (330, 110) if vertical else (60, 30)
    base = 110 if vertical else 80                      # hook + credit keep their size however big the captions are
    box = S["box"]; boxcol = ass_color(S["outline_color"], 0x30)
    seam = S["position"] == "seam" and vertical
    if S["position"] == "seam" and not vertical: align, mv = 2, int(S["y_offset"] * 0.2)         # (no facecam seam in a landscape picture)
    hlbox = ass_color(S["highlight"]); pad = max(4, int(fs * 0.08))
    cap_pos = f"{{\\an5\\pos({W // 2},{int(cam_top_h(W, H))})}}" if seam else ""
    if S["cap_x"] is not None: cap_pos = f"{{\\an5\\pos({int(S['cap_x'] * W)},{int(S['cap_y'] * H)})}}"      # dragged to a spot on the preview
    hdr = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {W}
PlayResY: {H}
WrapStyle: 0
[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,OutlineColour,BackColour,Bold,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV
Style: Cap,{S['font']},{fs},{ass_color(S['color'])},{boxcol if box else ass_color(S['outline_color'])},{boxcol if box else '&H00000000'},{-1 if S['bold'] else 0},{3 if box else 1},{S['outline']:g},{0 if box else S['shadow']:g},{align},60,60,{mv}
Style: CapBox,{S['font']},{fs},{hlbox},{hlbox},{hlbox},{-1 if S['bold'] else 0},3,{pad},0,{align},60,60,{mv}
Style: Hook,Impact,{int(base*0.9)},{ass_color(S['hook_color'])},&H00000000,&H00000000,0,1,7,3,8,60,60,{hook_y}
Style: Credit,Arial,{int(base*0.4)},&H00FFFFFF,&H00000000,&H00000000,0,1,3,1,2,40,40,{credit_y}
Style: TxtBox,Impact,{int(base*0.8)},{ass_color(S['hook_color'])},&H00000000,&H00000000,0,3,8,0,5,60,60,0
Style: TxtTop,Impact,{int(base*0.8)},{ass_color(S['hook_color'])},&H00000000,&H00000000,0,1,6,2,8,60,60,{hook_y}
Style: TxtMid,Impact,{int(base*0.8)},{ass_color(S['hook_color'])},&H00000000,&H00000000,0,1,6,2,5,60,60,0
Style: TxtBot,Impact,{int(base*0.8)},{ass_color(S['hook_color'])},&H00000000,&H00000000,0,1,6,2,2,60,60,{credit_y + 60}
[Events]
Format: Layer,Start,End,Style,Text
"""
    def clean(t):
        t = re.sub(r"[{}\\]", "", t)
        if S["strip_punct"]: t = t.strip(".,!?;:\"“”…")
        if S["censor"]: t = censor_word(t)
        return t.upper() if S["upper"] else t
    ev = []
    if hook: ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(min(3.0, end - start))},Hook,{clean_hook(hook).replace(chr(10), chr(92) + 'N')}")
    for tx in (texts or []):                             # text overlays from the timeline (times are seconds into the raw clip)
        a0, a1 = max(0.0, tx.get("t0", 0.0) - start), min(end - start, tx.get("t1", 1e9) - start)
        body = re.sub(r"[{}\\]", "", tx.get("text", "")).strip().replace("\n", "\\N")
        if not body or a1 - a0 <= 0.05: continue
        it = text_defaults(tx, vertical); kfs = sorted((k["t"] - start for k in (tx.get("kf") or [])))
        st = lambda t: kf_state(dict(it, kf=[dict(k, t=k["t"] - start) for k in (tx.get("kf") or [])]), t, TEXT_KEYS)
        cuts_t = [a0] + [t for t in kfs if a0 + 0.03 < t < a1 - 0.03] + [a1]
        for u0, u1 in zip(cuts_t, cuts_t[1:]):
            s0, s1 = st(u0), st(u1); ms = int((u1 - u0) * 1000)
            f0, f1 = int(base * 0.8 * s0["size"]), int(base * 0.8 * s1["size"])
            p0, p1 = (s0["x"] * W, s0["y"] * H), (s1["x"] * W, s1["y"] * H)
            move = (f"\\pos({p0[0]:.0f},{p0[1]:.0f})" if p0 == p1 else f"\\move({p0[0]:.0f},{p0[1]:.0f},{p1[0]:.0f},{p1[1]:.0f},0,{ms})")
            grow = f"\\t(0,{ms},\\fs{f1})" if f0 != f1 else ""
            look = text_look(tx)
            if tx.get("hook") and S.get("upper_hook", True) and not tx.get("keep_case"): body_ = clean_hook(tx.get("text", "")).replace("\n", "\\N")
            else: body_ = body
            pop = f"\\fscx55\\fscy55\\t(0,220,\\fscx100\\fscy100)" if (tx.get("pop") and u0 == cuts_t[0]) else ""
            ev.append(f"Dialogue: 2,{ass_time(u0)},{ass_time(u1)},{'TxtBox' if tx.get('box') else 'TxtMid'},{{\\an5{move}\\fs{f0}{grow}{look}{pop}}}{body_}")
    credit_txt = re.sub(r"[{}\\]", "", credit or "")
    if credit: ev.append(f"Dialogue: 1,{ass_time(0)},{ass_time(end - start)},Credit,{credit_txt}")
    ws = [dict(w, w=clean(w["w"])) for w in words]
    ws = [w for w in ws if w["w"]]
    n, mode, hl, base = S["words"], S["highlight_mode"], ass_inline(S["highlight"]), ass_inline(S["color"])
    for i in range(0, len(ws), n):
        grp = ws[i:i + n]
        if mode == "none":
            ev.append(f"Dialogue: 0,{ass_time(grp[0]['s'] - start)},{ass_time(max(grp[-1]['e'], grp[0]['s'] + .2) - start)},Cap,{cap_pos}{' '.join(x['w'] for x in grp)}")
            continue
        for j, w in enumerate(grp):
            parts = []
            if mode == "box":                          # the word being said gets a coloured box: one line with only that word visible (drawn as a box), under the normal line
                e = grp[j + 1]["s"] if j + 1 < len(grp) else w["e"]; a_, b_ = ass_time(w['s'] - start), ass_time(max(e, w['s'] + .1) - start)
                ghost = " ".join(("{\\alpha&H00&}" + x["w"]) if k == j else ("{\\alpha&HFF&}" + x["w"]) for k, x in enumerate(grp))
                ev.append(f"Dialogue: 0,{a_},{b_},CapBox,{cap_pos}{ghost}")
                ev.append(f"Dialogue: 1,{a_},{b_},Cap,{cap_pos}{' '.join(x['w'] for x in grp)}")
                continue
            for k, x in enumerate(grp):
                if k == j:
                    tag = (f"\\c{hl}" if mode in ("color", "both") else "") + ("\\fscx118\\fscy118" if mode in ("pop", "both") else "")
                    parts.append("{" + tag + "}" + x["w"] + "{\\c" + base + "\\fscx100\\fscy100}")
                else:
                    parts.append(x["w"])
            e = grp[j + 1]["s"] if j + 1 < len(grp) else w["e"]
            ev.append(f"Dialogue: 0,{ass_time(w['s'] - start)},{ass_time(max(e, w['s'] + .1) - start)},Cap,{cap_pos}{' '.join(parts)}")
    open(path, "w", encoding="utf-8").write(hdr + "\n".join(ev))


TEXT_PRESET_Y = {"top": (0.20, 0.12), "middle": (0.5, 0.5), "bottom": (0.80, 0.88)}          # (vertical, landscape) centre of the text, as a fraction of the picture height


def text_defaults(tx, vertical):
    """A text item with x / y / size filled in (older items only knew Top / Middle / Bottom)."""
    d = dict(tx); py = TEXT_PRESET_Y.get(d.get("pos"), TEXT_PRESET_Y["top"])[0 if vertical else 1]
    d.setdefault("x", 0.5); d.setdefault("size", 1.0)
    if d.get("y") is None: d["y"] = py
    return d


def text_look(tx):
    """ASS override tags for one text item's own font / colours / outline / box (empty when it just uses the default look)."""
    o = ""
    if tx.get("font"): o += f"\\fn{re.sub(r'[{}\\\\]', '', str(tx['font']))}"
    if tx.get("color"): o += f"\\c{ass_inline(tx['color'])}"
    if tx.get("box"):
        bc = ass_inline(tx.get("box_color") or "#000000"); o += f"\\3c{bc}\\4c{bc}\\bord{max(2, int(float(tx.get('outline', 7)) + 4))}"
    else:
        if tx.get("outline_color"): o += f"\\3c{ass_inline(tx['outline_color'])}"
        if tx.get("outline") is not None: o += f"\\bord{float(tx['outline']):g}"
    if tx.get("bold"): o += "\\b1"
    return o


def clean_hook(t):
    return re.sub(r"[{}\\]", "", t).upper()


# ---------- facecam: find the streamer's camera box, then build the "cam on top, gameplay below" layout ----------
QUALITIES = {"High quality (bigger file)": 18, "Standard": 20, "Small file": 24}
FORMATS = {"MP4  (.mp4)": ".mp4", "MOV  (.mov)": ".mov", "MKV  (.mkv)": ".mkv", "WebM  (.webm)": ".webm", "AVI  (.avi)": ".avi"}
FORMAT_NOTES = {".mp4": "Works everywhere: TikTok, YouTube Shorts, Reels, X, Discord.", ".mov": "Apple / video-editor friendly. Same picture quality as MP4.",
                ".mkv": "Flexible container, good for editing. Most sites won't accept it.", ".webm": "Web video (VP9 + Opus). Small files, but exporting is slower.",
                ".avi": "Old format: big files, but opens in very old software."}
RESOLUTIONS = {"Full size (1080p / 1080x1920)": None, "Smaller (720p / 720x1280)": "720"}
LAYOUTS = {"Blurred background": "blur", "Center crop": "crop", "Facecam on top (auto-detect)": "facecam"}


def video_size(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"Stream #.*Video:.*?[, ](\d{2,5})x(\d{2,5})", r.stderr)
    if not m: raise RuntimeError("couldn't read the video size")
    return int(m.group(1)), int(m.group(2))


def _face_detector():
    """f(gray_uint8_image) -> [(x, y, w, h)], or None when OpenCV's face models aren't installed.
    `pip install opencv-python` includes the Haar models used here."""
    try: import cv2
    except ImportError: return None
    base = getattr(getattr(cv2, "data", None), "haarcascades", "") or ""
    for name in ("haarcascade_frontalface_default.xml", "haarcascade_frontalface_alt2.xml"):
        p = os.path.join(base, name)
        if os.path.exists(p):
            casc = cv2.CascadeClassifier(p)
            if not casc.empty():
                def det(gray, casc=casc):
                    g = cv2.equalizeHist(gray); m = max(24, int(g.shape[0] * 0.04))
                    return [tuple(int(v) for v in b) for b in casc.detectMultiScale(g, scaleFactor=1.08, minNeighbors=6, minSize=(m, m))]
                return det
    return None


def sample_gray_frames(video, times, width=960):
    vw, vh = video_size(video); h = max(2, int(round(width * vh / vw / 2)) * 2)
    out = []
    for t in times:
        raw = sh("ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", video, "-frames:v", "1", "-vf", f"scale={width}:{h}",
                 "-f", "rawvideo", "-pix_fmt", "gray", "-").stdout
        if len(raw) == width * h: out.append(np.frombuffer(raw, np.uint8).reshape(h, width))
    return out


def persistent_face(dets_per_frame, min_frames):
    """A real facecam face stays in the SAME place in most frames; false detections in gameplay jump around.
    Returns ((x, y, w, h), frames_seen) for the most persistent face, or None."""
    clusters = []
    for fi, dets in enumerate(dets_per_frame):
        for (x, y, w, h) in dets:
            for c in clusters:
                bx, by, bw, bh = c["med"]
                if abs(x + w / 2 - (bx + bw / 2)) < 0.5 * bw and abs(y + h / 2 - (by + bh / 2)) < 0.5 * bh and 0.6 < w / bw < 1.6:
                    c["boxes"].append((x, y, w, h)); c["frames"].add(fi); c["med"] = tuple(np.median(c["boxes"], axis=0)); break
            else:
                clusters.append(dict(boxes=[(x, y, w, h)], frames={fi}, med=(x, y, w, h)))
    if not clusters: return None
    best = max(clusters, key=lambda c: (len(c["frames"]), c["med"][2]))
    return (best["med"], len(best["frames"])) if len(best["frames"]) >= min_frames else None


def refine_cam_box(frames, face):
    """Grow a face box into the whole camera rectangle (pixels of `frames`). Looks for the camera's straight border, which stays
    put while the game moves (median over frames); falls back to cam-shaped padding, and snaps to a screen edge if the cam sits in a corner."""
    H, W = frames[0].shape; fx, fy, fw, fh = face
    st = np.stack([f.astype(np.float32) for f in frames])
    gx = np.median(np.abs(st[:, :, 2:] - st[:, :, :-2]), axis=0)          # static vertical edges   (H, W-2)
    gy = np.median(np.abs(st[:, 2:, :] - st[:, :-2, :]), axis=0)          # static horizontal edges (H-2, W)
    cols = gx[int(max(0, fy)):int(min(H, fy + fh)), :].mean(axis=0); rows = gy[:, int(max(0, fx)):int(min(W, fx + fw))].mean(axis=1)
    def edge(profile, lo, hi):
        lo, hi = max(0, int(lo)), min(len(profile) - 1, int(hi))
        if hi - lo < 3: return None
        seg = profile[lo:hi + 1]; i = int(np.argmax(seg))
        return lo + i + 1 if seg[i] > 3.0 * (np.median(profile) + 1.0) and seg[i] > 12 else None
    L = edge(cols, fx - 2.6 * fw, fx - 0.3 * fw); R = edge(cols, fx + 1.3 * fw, fx + 3.6 * fw)
    T = edge(rows, fy - 2.2 * fh, fy - 0.2 * fh);  B = edge(rows, fy + 1.2 * fh, fy + 3.6 * fh)
    if L is None: L = 0 if fx < 1.3 * fw else fx - 0.9 * fw
    if R is None: R = W if W - (fx + fw) < 1.3 * fw else fx + fw + 0.9 * fw
    if T is None: T = 0 if fy < 1.0 * fh else fy - 0.7 * fh
    if B is None: B = H if H - (fy + fh) < 1.6 * fh else fy + fh + 1.0 * fh
    L, T, R, B = max(0, L), max(0, T), min(W, R), min(H, B)
    return (L / W, T / H, (R - L) / W, (B - T) / H)


def detect_facecam(video, start=0.0, end=None, samples=10, detector=None, log=print):
    """Find the streamer's facecam. Returns dict(x, y, w, h [fractions of the frame], ar [cam width/height in pixels], hits, samples) or None."""
    detector = detector or _face_detector()
    if detector is None:
        log("Facecam detection needs OpenCV's face models:  python -m pip install opencv-python   "
            "(or use Adjust facecam... to place the box yourself).")
        return None
    end = end if end is not None else duration(video)
    times = [start + (end - start) * (0.08 + 0.84 * i / max(1, samples - 1)) for i in range(samples)]
    frames = sample_gray_frames(video, times)
    if len(frames) < 2: log("Couldn't read enough frames to look for a facecam."); return None
    H, W = frames[0].shape; found = persistent_face([detector(f) for f in frames], max(2, int(np.ceil(0.4 * len(frames)))))
    if not found:
        log("No stable face found, so no facecam was detected. Use Adjust facecam... to place the box yourself."); return None
    face, hits = found
    if face[2] > 0.30 * W:
        log("The face fills much of the screen (a full-screen camera), so there's no separate facecam to cut out."); return None
    x, y, w, h = refine_cam_box(frames, face); vw, vh = video_size(video)
    return dict(x=x, y=y, w=w, h=h, ar=(w * vw) / max(1.0, h * vh), src_ar=vw / vh, hits=hits, samples=len(frames))


def cam_from_fractions(x, y, w, h, video):
    vw, vh = video_size(video)
    return dict(x=x, y=y, w=w, h=h, ar=(w * vw) / max(1.0, h * vh), src_ar=vw / vh)


def auto_gx(cam, W, GH):
    """Where to take the gameplay slice from (0 = far left, 1 = far right): centred, but shifted so the camera itself isn't in it."""
    src_ar = cam.get("src_ar", 16 / 9); cwf = min(1.0, (W / GH) / src_ar)          # slice width as a fraction of the screen width
    if cwf >= 0.999: return 0.5
    if cam["x"] + cam["w"] / 2 > 0.5:  return min(0.5, max(0.0, (cam["x"] - 0.005 - cwf) / (1 - cwf)))   # cam on the right: slide left
    return max(0.5, min(1.0, (cam["x"] + cam["w"] + 0.005) / (1 - cwf)))                                  # cam on the left: slide right


def cam_top_h(W, H, cam=None):
    """Height of the facecam strip in the vertical layout: 40% of the picture (what the reference clip uses), so the captions sit on the seam."""
    return int(H * 0.40) // 2 * 2


CAM_QUALITY = {"Standard": 0, "Enhanced (sharper)": 1, "Max (sharper + cleaner)": 2}


def cam_enhance(q):
    """Extra filters for the facecam strip. The camera is a small part of the screen blown up to full width, so it comes out soft:
    1 = a better scaler + sharpening, 2 = also removes noise / blockiness first and lifts contrast and colour slightly."""
    if q >= 2: return "hqdn3d=2.5:2:4:3,", ":flags=lanczos", ",unsharp=5:5:1.0:3:3:0.0,eq=contrast=1.04:saturation=1.07"
    if q == 1: return "", ":flags=lanczos", ",unsharp=5:5:0.8:3:3:0.0"
    return "", "", ""


CAM_MOTIONS = {"Still": "none", "Slow zoom in": "zoom_in", "Slow zoom out": "zoom_out", "Drift right": "pan_right", "Drift left": "pan_left",
               "Drift down": "pan_down", "Drift up": "pan_up", "Sway side to side": "sway"}


def cam_motion(cam, t0, tl):
    """(zoom, x, y) ffmpeg expressions for a moving facecam: zoom >= 1 is how far in we are; x / y are 0..1 = where the view sits inside the camera box.
    The motion runs from t0 over tl seconds (the clip / the part of the video this camera box is used for)."""
    m = cam.get("motion") or "none"
    if m not in CAM_MOTIONS.values() or m == "none" or not tl or tl <= 0.1: return None
    a = max(0.03, min(1.0, float(cam.get("amt", 20)) / 100)); p = f"clip((t-{t0:.3f})/{tl:.3f},0,1)"
    zin, zfix, c = f"(1+{a:.3f}*{p})", f"{1 + a:.3f}", "0.5"
    return {"zoom_in": (zin, c, c), "zoom_out": (f"(1+{a:.3f}*(1-{p}))", c, c), "pan_right": (zfix, p, c), "pan_left": (zfix, f"(1-{p})", c),
            "pan_down": (zfix, c, p), "pan_up": (zfix, c, f"(1-{p})"), "sway": (zfix, f"(0.5+0.5*sin(2*PI*(t-{t0:.3f})/5))", c)}[m]


def facecam_vf(W, H, cam, gx=None, q=0, t0=0.0, tl=None):
    """Vertical layout: the facecam fills the top, the gameplay fills the rest. gx = None -> automatic slice position.
    A camera with a motion (slow zoom / drift) is scaled a little bigger each frame and the strip is cut out of it at a moving spot."""
    TH = cam_top_h(W, H, cam); GH = H - TH
    if gx is None: gx = auto_gx(cam, W, GH)
    gcrop = f"min(iw\\,ih*{W}/{GH})"
    pre, flags, post = cam_enhance(int(q or 0)); mo = cam_motion(cam, t0, tl)
    if mo:
        z, fx, fy = mo; k = f"max({W}/iw,{TH}/ih)*{z}"
        fit = (f"scale=w='trunc(iw*{k}/2)*2+2':h='trunc(ih*{k}/2)*2+2':eval=frame{flags},crop={W}:{TH}:x='(iw-{W})*{fx}':y='(ih-{TH})*{fy}'{post}")
    else: fit = f"scale={W}:{TH}:force_original_aspect_ratio=increase{flags},crop={W}:{TH}{post}"
    return (f"split[a][b];[a]crop=iw*{cam['w']:.5f}:ih*{cam['h']:.5f}:iw*{cam['x']:.5f}:ih*{cam['y']:.5f},{pre}"
            f"{fit}[top];"
            f"[b]crop={gcrop}:ih:(iw-{gcrop})*{max(0.0, min(1.0, gx)):.3f}:0,scale={W}:{GH}[bot];[top][bot]vstack")


def draw_cam_vf(cam):
    """Filter that outlines the cam box in red (used by the Adjust facecam window)."""
    return f"drawbox=x=iw*{cam['x']:.5f}:y=ih*{cam['y']:.5f}:w=iw*{cam['w']:.5f}:h=ih*{cam['h']:.5f}:color=red:t=4"


BLUR_KEYS, TEXT_KEYS = ("x", "y", "w", "h"), ("x", "y", "size")


def kf_state(it, t, keys):
    """Where an element is at time t: its plain values, or - with keyframes - a straight-line blend between the neighbouring keyframes."""
    base = {k: it.get(k) for k in keys}
    kf = sorted(it.get("kf") or [], key=lambda k: k["t"])
    if not kf: return base
    g = lambda d, k: d.get(k, base[k])
    if t <= kf[0]["t"]: return {k: g(kf[0], k) for k in keys}
    if t >= kf[-1]["t"]: return {k: g(kf[-1], k) for k in keys}
    for a, b in zip(kf, kf[1:]):
        if a["t"] <= t <= b["t"]:
            u = (t - a["t"]) / max(1e-6, b["t"] - a["t"])
            return {k: g(a, k) + (g(b, k) - g(a, k)) * u for k in keys}
    return base


def upsert_kf(it, t, state, keys):
    """Record the element's look at time t as a keyframe (replacing one that's already there)."""
    kf = it.setdefault("kf", [])
    for k in kf:
        if abs(k["t"] - t) < 0.06: k.update({kk: state[kk] for kk in keys}); return
    kf.append(dict(t=round(t, 2), **{kk: state[kk] for kk in keys})); kf.sort(key=lambda k: k["t"])


def map_item_times(it, f):
    """Copy of a timeline item with every time (start, end, keyframes) run through f."""
    n = dict(it)
    for k in ("t0", "t1"):
        if k in it: n[k] = f(it[k])
    if it.get("kf"): n["kf"] = [dict(k, t=f(k["t"])) for k in it["kf"]]
    return n


def kf_expr(pts):
    """ffmpeg expression in t for a value that moves in straight lines through pts = [(t, value)]."""
    pts = sorted(pts)
    if len(pts) == 1: return f"{pts[0][1]:.2f}"
    e = f"{pts[-1][1]:.2f}"
    for (t0, v0), (t1, v1) in reversed(list(zip(pts, pts[1:]))):
        e = f"if(lt(t,{t1:.3f}),{v0:.2f}+({v1 - v0:.2f})*(t-{t0:.3f})/{max(t1 - t0, 0.001):.3f},{e})"
    return f"if(lt(t,{pts[0][0]:.3f}),{pts[0][1]:.2f},{e})"


def seg_cam_boxes(segs):
    """Clips that have their own facecam box (not linked to the others)."""
    return [dict(camseg=True, t0=float(sg["t0"]), t1=float(sg["t1"]), cam=sg["cam"], gx=sg.get("gx"), x=0.0, y=0.0, w=1.0, h=1.0) for sg in segs or [] if sg.get("cam") and not sg.get("nocam")]


def seg_nocam_boxes(segs):
    """Clips whose facecam is switched off (timed markers; they ride along with the blur boxes so cuts move them correctly)."""
    return [dict(nocam=True, t0=float(sg["t0"]), t1=float(sg["t1"]), x=0.0, y=0.0, w=1.0, h=1.0) for sg in segs or [] if sg.get("nocam")]


def seg_fit_boxes(segs):
    """Clip size / position: a segment whose picture was scaled or moved (s = size, px/py = shift as fractions of the frame) becomes a timed 'fit' step in the filter chain."""
    out = []
    for sg in segs or []:
        s_, px, py = float(sg.get("s", 1.0)), float(sg.get("px", 0.0)), float(sg.get("py", 0.0))
        if abs(s_ - 1.0) > 0.005 or abs(px) > 0.002 or abs(py) > 0.002:
            out.append(dict(fit=True, s=s_, px=px, py=py, t0=float(sg["t0"]), t1=float(sg["t1"]), x=0.0, y=0.0, w=1.0, h=1.0))
    return out


def hook_blur_boxes(texts):
    """A hook with 'Blur the background' on becomes a whole-frame blur box that lasts as long as the hook and fades out (the hook words are drawn on top, so they stay sharp)."""
    out = []
    for t in texts or []:
        if t.get("hook") and t.get("blurbg") and (t.get("text") or "").strip():
            t0, t1 = max(0.0, float(t.get("t0", 0.0))), float(t.get("t1", 3.0))
            out.append(dict(x=0.0, y=0.0, w=1.0, h=1.0, t0=t0, t1=t1, fade=max(0.0, min(float(t.get("blurfade", 0.6)), (t1 - t0))), full=True))
    return out


def blur_prefix(blurs, src=None):
    """Filter text (ending in a comma) that blurs rectangles on the ORIGINAL frame, before any layout/crop happens, so a box
    drawn on the original also hides that spot in the vertical version. blurs = [{x,y,w,h}] as fractions of the frame."""
    if not blurs: return ""
    SW, SH = src or (1920, 1080); out = ""
    blurs = sorted(blurs, key=lambda q: 0 if q.get("fit") else 1)      # size / position first, so every other box works on the picture as it will look
    blurs = [b for b in blurs if not b.get("nocam") and not b.get("camseg")]
    for i, b in enumerate(blurs):
        if b.get("fit"):
            FW, FH = max(16, int(SW * b["s"]) // 2 * 2), max(16, int(SH * b["s"]) // 2 * 2)
            fx, fy = int((SW - FW) / 2 + b["px"] * SW), int((SH - FH) / 2 + b["py"] * SH)
            out += (f"split=3[m{i}][r{i}][k{i}];[k{i}]drawbox=x=0:y=0:w=iw:h=ih:color=black:t=fill[kb{i}];[r{i}]scale={FW}:{FH}[sc{i}];"
                    f"[kb{i}][sc{i}]overlay={fx}:{fy}[c{i}];[m{i}][c{i}]overlay=0:0:enable='between(t,{max(0.0, b['t0']):.3f},{b['t1']:.3f})',")
            continue
        kfs = sorted(b.get("kf") or [], key=lambda k: k["t"])
        if len(kfs) >= 2:                                             # a box that moves / changes size: blur a patch covering every keyframe, reveal it through a moving mask
            px = lambda k, v: v * (SW if k in ("x", "w") else SH)
            boxes = [(px("x", k.get("x", b["x"])), px("y", k.get("y", b["y"])), px("w", k.get("w", b["w"])), px("h", k.get("h", b["h"])), k["t"]) for k in kfs]
            ux0, uy0 = max(0, int(min(q[0] for q in boxes)) // 2 * 2), max(0, int(min(q[1] for q in boxes)) // 2 * 2)
            ux1, uy1 = min(SW, int(max(q[0] + q[2] for q in boxes)) // 2 * 2 + 2), min(SH, int(max(q[1] + q[3] for q in boxes)) // 2 * 2 + 2)
            UW, UH = max(4, ux1 - ux0), max(4, uy1 - uy0)
            r = max(1, min(40, int(min(min(q[2], q[3]) for q in boxes)) // 4 - 1))
            xe, ye = kf_expr([(q[4], q[0] - ux0) for q in boxes]), kf_expr([(q[4], q[1] - uy0) for q in boxes])
            we, he = kf_expr([(q[4], max(2.0, q[2])) for q in boxes]), kf_expr([(q[4], max(2.0, q[3])) for q in boxes])
            t0, t1 = max(0.0, b.get("t0", 0.0)), b.get("t1", 1e6)
            out += (f"split[m{i}][r{i}];[r{i}]crop={UW}:{UH}:{ux0}:{uy0},boxblur={r}:3,boxblur={r}:3,format=yuva420p[b{i}];"
                    f"color=c=black:s={UW}x{UH}:r=30,format=gray[k{i}];color=c=white:s=64x64:r=30,format=gray,scale=w='max(2,{we})':h='max(2,{he})':eval=frame[w{i}];"
                    f"[k{i}][w{i}]overlay=x='{xe}':y='{ye}':eval=frame:shortest=1,format=gray[q{i}];[b{i}][q{i}]alphamerge[c{i}];"
                    f"[m{i}][c{i}]overlay={ux0}:{uy0}:enable='between(t,{t0:.2f},{t1:.2f})':shortest=1,")
            continue
        if kfs: b = dict(b, **kf_state(b, 0, BLUR_KEYS))
        x = max(0, min(SW - 4, int(b["x"] * SW) // 2 * 2)); y = max(0, min(SH - 4, int(b["y"] * SH) // 2 * 2))
        w = max(4, min(SW - x, int(b["w"] * SW) // 2 * 2)); h = max(4, min(SH - y, int(b["h"] * SH) // 2 * 2))
        r = max(1, min(40, min(w, h) // 4 - 1))                       # chroma planes are half size, so the radius is capped at size/4
        when = ""
        if "t1" in b or b.get("t0", 0) > 0:                          # shown only between t0 and t1 (seconds into the clip)
            when = f":enable='between(t,{max(0.0, b.get('t0', 0.0)):.2f},{b.get('t1', 1e6):.2f})'"
        if b.get("fade", 0) > 0.04:                                   # the blur melts away over its last `fade` seconds
            F = float(b["fade"]); t1 = float(b.get("t1", 1e6))
            out += (f"split[m{i}][r{i}];[r{i}]crop={w}:{h}:{x}:{y},boxblur={r}:3,boxblur={r}:3,format=yuva420p,fade=t=out:st={max(0.0, t1 - F):.3f}:d={F:.3f}:alpha=1[b{i}];"
                    f"[m{i}][b{i}]overlay={x}:{y}{when},")
            continue
        out += (f"split[m{i}][r{i}];[r{i}]crop={w}:{h}:{x}:{y},boxblur={r}:3,boxblur={r}:3[b{i}];[m{i}][b{i}]overlay={x}:{y}{when},")
    return out


OVL_KEYS = ("x", "y", "w")                                   # centre x, centre y, width - all fractions of the edited picture
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".apng")
ANIM_EXT = (".gif", ".apng", ".webm", ".mp4", ".mov", ".mkv", ".avi", ".m4v")


_ANIM_CACHE = {}


def anim_info(path):
    """(frame count, fps) of a GIF / short video, or None when it's a still picture."""
    if path in _ANIM_CACHE: return _ANIM_CACHE[path]
    res = None
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path], capture_output=True, text=True).stderr
        fps = re.search(r"([\d.]+) fps", r); dm = re.search(r"Duration: (\d+):(\d+):(\d+\.?\d*)", r)
        if fps and dm and os.path.splitext(path)[1].lower() not in (".png", ".jpg", ".jpeg", ".bmp", ".webp"):
            d = int(dm.group(1)) * 3600 + int(dm.group(2)) * 60 + float(dm.group(3)); f = float(fps.group(1))
            if d > 0.05: res = (max(2, int(d * f + 2)), f)
    except Exception: pass
    _ANIM_CACHE[path] = res; return res


def ovl_filter(overlays, OW, OH):
    """Filter text (starts with ';') that lays pictures / GIFs / short videos over the finished picture. Pictures come in through ffmpeg's
    `movie` source, so the preview (one input) and the export use exactly the same graph. x, y = centre as fractions; w = width as a fraction."""
    out, prev = "", None
    for i, o in enumerate(overlays or []):
        path = str(o.get("path", ""))
        if not path or not os.path.exists(path): continue
        esc = path.replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
        t0, t1 = max(0.0, float(o.get("t0", 0.0))), float(o.get("t1", 1e6))
        kfs = sorted(o.get("kf") or [], key=lambda k: k["t"]); st = kf_state(o, 0, OVL_KEYS) if kfs else o
        g = lambda k, d: d.get(k, o.get(k, 0.5))
        if len(kfs) >= 2:
            we = kf_expr([(k["t"], max(8.0, g("w", k) * OW)) for k in kfs])
            xe = kf_expr([(k["t"], g("x", k) * OW) for k in kfs]); ye = kf_expr([(k["t"], g("y", k) * OH) for k in kfs])
            scale = f"scale=w='max(8,trunc(({we})/2)*2)':h=-2:eval=frame"; pos = f"x='({xe})-overlay_w/2':y='({ye})-overlay_h/2':eval=frame"
        else:
            wpx = max(8, int(float(st["w"]) * OW) // 2 * 2)
            scale = f"scale={wpx}:-2"; pos = f"x={float(st['x']) * OW:.1f}-overlay_w/2:y={float(st['y']) * OH:.1f}-overlay_h/2"
        ai = anim_info(path); wmax = max([float(st["w"])] + [g("w", k) for k in kfs]) * OW; wpre = max(16, int(min(wmax, 1000)) // 2 * 2)
        loop = ""
        if ai:                                                # an animation keeps repeating while the bar lasts (small frames are cached for that)
            n = min(ai[0], max(2, int(150e6 / (wpre * wpre * 0.75 * 4))))
            loop = f",scale={wpre}:-2,loop=loop=-1:size={n}:start=0"
        alpha = f",colorchannelmixer=aa={float(o['opacity']):.2f}" if float(o.get("opacity", 1.0)) < 0.99 else ""
        src = "o_in" if prev is None else prev
        span = max(0.2, min(t1 - t0, 900.0)); fin, fout = max(0.0, min(float(o.get("fin", 0) or 0), span)), max(0.0, min(float(o.get("fout", 0) or 0), span))
        fades = ""
        if fin > 0.04 or fout > 0.04:                         # fade in / out: the picture is turned into frames first (a still is just one frame, which can't fade)
            if not ai: loop = f",loop=loop=-1:size=1:start=0,setpts=N/(30*TB)"
            if fin > 0.04: fades += f",fade=t=in:st={t0:.3f}:d={fin:.3f}:alpha=1"
            if fout > 0.04: fades += f",fade=t=out:st={max(t0, t1 - fout):.3f}:d={fout:.3f}:alpha=1"
        spd = max(0.1, min(8.0, float(o.get("speed", 1.0) or 1.0))) if ai else 1.0       # animated pictures / videos can play faster or slower
        stretch = f"(PTS-STARTPTS)/{spd:.3f}+{t0:.3f}/TB" if abs(spd - 1.0) > 0.01 else f"PTS-STARTPTS+{t0:.3f}/TB"
        out += (f";movie='{esc}'{loop},setpts={stretch},format=rgba{alpha},{scale}{fades}[ov{i}];"
                f"[{src}][ov{i}]overlay={pos}:enable='between(t,{t0:.3f},{t1:.3f})'{':shortest=1' if loop else ''}")
        prev = f"o{i}"; out += f"[{prev}]"
    if prev is None: return ""
    return out[: out.rindex("[")]                              # the last overlay's output stays unlabelled: it is the picture


def build_vf(vertical, layout, ass=None, size=None, cam=None, gx=None, blurs=None, src=None, scale_h=None, overlays=None, camq=0, borders=True, dur=None):
    if vertical and src and src[0] / max(1, src[1]) < 0.75: layout = "portrait"       # the video is already tall: no facecam / blur layout, just fit it
    nocam = [(b["t0"], b["t1"]) for b in (blurs or []) if b.get("nocam")]
    camsegs = [(b["t0"], b["t1"], b["cam"], b.get("gx")) for b in (blurs or []) if b.get("camseg")]
    """ffmpeg video-filter chain shared by the final render AND the live preview, so what you see is what you get.
    vertical+'blur' = whole 16:9 frame centred over a blurred copy; vertical+'crop' = centre-crop to 9:16.
    size=(W,H): output size (final vertical = 1080x1920; previews are small so they play in real time)."""
    base = blur_prefix(blurs, src) + _build_vf(vertical, layout, ass, size, cam, gx, scale_h, camq, nocam, borders, camsegs, dur)
    ov = [o for o in (overlays or []) if o.get("path") and os.path.exists(str(o["path"]))]
    if not ov: return base
    if vertical: OW, OH = size or (1080, 1920)
    elif size: OW, OH = size
    else:
        SW, SH = src or (1920, 1080); OW, OH = (int(SW * scale_h / SH) // 2 * 2, scale_h) if scale_h else (SW, SH)
    return base + "[o_in]" + ovl_filter(ov, OW, OH)


def codec_args(out, crf=20):
    """Video + audio encoder settings for the container the file name asks for (.mp4 / .mov / .mkv / .webm / .avi)."""
    ext = os.path.splitext(str(out))[1].lower(); crf = int(crf)
    if ext == ".webm": return ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(min(63, crf + 10)), "-row-mt", "1", "-cpu-used", "5", "-deadline", "good", "-c:a", "libopus"]
    base = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p"]
    if ext == ".avi": return base + ["-c:a", "libmp3lame", "-q:a", "3"]
    return base + ["-c:a", "aac"] + (["-movflags", "+faststart"] if ext in (".mp4", ".mov") else [])


def plain_vertical(W, H, borders=True):
    """A wide picture in a tall frame: blurred copy of the picture fills the bars (borders=True), or no bars at all - the picture is zoomed to fill the frame (False)."""
    if not borders: return f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H}"
    bl = max(2, round(25 * W / 1080))
    return (f"split[a][b];[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur={bl}:4[bg];"
            f"[b]scale={W}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2")


def _build_vf(vertical, layout, ass=None, size=None, cam=None, gx=None, scale_h=None, camq=0, nocam=None, borders=True, camsegs=None, dur=None):
    tail = ""
    if ass:
        a = ass.replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
        tail = f",ass='{a}'{fonts_opt()}"
    if vertical:
        W, H = size or (1080, 1920)
        if layout == "portrait": return f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black" + tail
        if layout == "facecam" and cam and (nocam or camsegs):          # clips with their own facecam box / no facecam: each layout is built, then shown only during its clips
            sfx = lambda txt, k: re.sub(r"\[(\w+)\]", lambda m: f"[{m.group(1)}_{k}]", txt)
            chains = [(facecam_vf(W, H, cam, gx, camq, 0.0, dur), None)]
            for t0, t1, c2, g2 in camsegs or []: chains.append((facecam_vf(W, H, c2, g2, camq, max(0.0, t0), t1 - max(0.0, t0)), f"between(t,{max(0.0, t0):.3f},{t1:.3f})"))
            if nocam: chains.append((plain_vertical(W, H, borders), "+".join(f"between(t,{max(0.0, a):.3f},{b:.3f})" for a, b in nocam)))
            n = len(chains); out = f"split={n}" + "".join(f"[s{k}_s]" for k in range(n)) + ";"
            for k, (ch, _) in enumerate(chains): out += f"[s{k}_s]{sfx(ch, str(k))}[l{k}_s];"
            prev = "l0_s"
            for k in range(1, n):
                out += f"[{prev}][l{k}_s]overlay=0:0:enable='{chains[k][1]}'" + ("" if k == n - 1 else f"[m{k}_s];"); prev = f"m{k}_s"
            return out + tail
        if layout == "facecam" and cam:
            return facecam_vf(W, H, cam, gx, camq, 0.0, dur) + tail
        if layout in ("blur", "facecam"):
            return plain_vertical(W, H, borders) + tail
        return f"crop=ih*9/16:ih,scale={W}:{H}" + tail
    if size:
        W, H = size
        return f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black" + tail
    return (f"scale=-2:{scale_h}" if scale_h else "null") + tail


def fit_vf(W, H):
    """Scale any video to fit inside W x H (letterboxed) - used to play files as-is."""
    return f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:black"


def render(video, start, end, words, out, vertical=False, layout="blur", hook="", credit="", style=None, cam=None, gx=None, blurs=None, voice=None, log=print, music=None, texts=None, crf=20, res=None, clip=None, overlays=None, camq=0, borders=True):
    """layout 'blur' = full 16:9 gameplay centred over a blurred copy (nothing cropped off);
    'crop' = centre-crop to 9:16 (what the reference tool does)."""
    ws = [w for w in words if start <= w["s"] <= end]
    ass = os.path.join(tempfile.gettempdir(), f"clipstudio_{os.getpid()}_{abs(hash(out))}.ass"); make_ass(ws, start, end, ass, vertical, hook, credit, style, texts)
    src = None
    if overlays: overlays = [map_item_times(o, lambda t: t - start) for o in overlays]
    blurs = list(blurs or []) + hook_blur_boxes(texts)
    if blurs or overlays or vertical:
        try: src = video_size(video)
        except Exception: pass
        # box times are relative to the raw clip; an AI trim moves the clip's start, so shift them
        blurs = [map_item_times(b, lambda t: t - start) for b in blurs]
    H = 1280 if res == "720" else 1920                       # export size: "720" = smaller file
    vf = build_vf(vertical, layout, ass, (H * 9 // 16 // 2 * 2, H) if vertical else None, cam, gx, blurs, src, scale_h=(720 if (res == "720" and not vertical) else None), overlays=overlays, camq=camq, borders=borders, dur=end - start)
    wav = None
    if audio_active(voice, music, clip):
        wav = os.path.join(tempfile.gettempdir(), f"clipstudio_voice_{os.getpid()}_{abs(hash(out))}.wav")
        try:
            build_audio(video, start, end, voice, music, wav, log, clip)
        except Exception as e: log(f"Voice focus / music failed ({str(e)[:120]}); keeping the original audio."); wav = None
    if wav:
        sh("ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video, "-i", wav, "-vf", vf, "-map", "0:v", "-map", "1:a",
           *codec_args(out, crf), "-shortest", out)
        safe_remove(wav)
    else:
        sh("ffmpeg", "-y", "-ss", str(start), "-to", str(end), "-i", video, "-vf", vf, *codec_args(out, crf), out)
    safe_remove(ass)


def safe_remove(path, tries=8):
    for _ in range(tries):                       # Windows keeps files locked for a moment after ffmpeg exits
        try:
            os.remove(path); return True
        except FileNotFoundError:
            return True
        except OSError:
            time.sleep(0.25)
    return False


# ---------- voice focus: make the streamer easier to hear over the game, only where you want it ----------
VOICE_STRENGTHS = {"Light": "highpass=f=120,lowpass=f=6000,afftdn=nr=12:nf=-30,acompressor=threshold=0.1:ratio=3:attack=5:release=120:makeup=2",
                   "Medium": "highpass=f=160,lowpass=f=4500,afftdn=nr=20:nf=-35,equalizer=f=2500:t=q:w=1:g=4,acompressor=threshold=0.06:ratio=5:attack=5:release=120:makeup=2",
                   "Strong": "highpass=f=220,lowpass=f=3400,afftdn=nr=30:nf=-40,equalizer=f=2500:t=q:w=1:g=6,acompressor=threshold=0.04:ratio=9:attack=3:release=100:makeup=4,alimiter=limit=0.85"}


def voice_active(v):
    return bool(v and v.get("on") and v.get("ranges"))


def has_demucs():
    import importlib.util
    return importlib.util.find_spec("demucs") is not None


def separate_voice(video, log=print):
    """True AI separation with Demucs (optional install: python -m pip install demucs). Returns the path of the vocals-only audio."""
    if not has_demucs(): raise RuntimeError("Demucs isn't installed (python -m pip install demucs)")
    key = f"{abs(hash((os.path.abspath(video), os.path.getmtime(video), os.path.getsize(video))))}"
    work = os.path.join(tempfile.gettempdir(), "clipstudio_demucs_" + key); vocals = os.path.join(work, "htdemucs", "in", "vocals.wav")
    if os.path.exists(vocals): return vocals
    os.makedirs(work, exist_ok=True); wav = os.path.join(work, "in.wav")
    sh("ffmpeg", "-y", "-v", "error", "-i", video, "-vn", "-ac", "2", "-ar", "44100", wav)
    log("Separating the voice from the game audio with Demucs (can take a minute on CPU)...")
    r = subprocess.run([sys.executable, "-m", "demucs", "--two-stems=vocals", "-n", "htdemucs", "-o", work, wav], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(vocals): raise RuntimeError((r.stderr or "no output")[-200:])
    return vocals


def make_voice_audio(video, start, end, voice, out, log=print):
    """Writes the clip's audio (start..end of `video`) to a wav, with voice focus applied ONLY inside voice['ranges']
    (seconds into the raw clip). Outside the ranges the original audio is untouched."""
    dur = end - start
    ranges = [(max(0.0, r["t0"] - start), min(dur, r["t1"] - start)) for r in voice.get("ranges", [])]
    ranges = [r for r in ranges if r[1] - r[0] > 0.05]
    base = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-to", str(end), "-i", video]
    if not voice.get("on") or not ranges:
        sh(*base, "-vn", "-ac", "2", "-ar", "44100", out); return out
    m = "+".join(f"between(t,{a:.2f},{b:.2f})" for a, b in ranges)
    keep, only = f"volume='if(gt({m},0),0,1)':eval=frame", f"volume='if(gt({m},0),1,0)':eval=frame"
    chain = VOICE_STRENGTHS.get(voice.get("strength", "Medium"), VOICE_STRENGTHS["Medium"])
    mix = "amix=inputs=2:duration=longest:dropout_transition=0,volume=2"   # (amix halves each input; one side is always silent)
    stem = None
    if voice.get("ai"):
        try: stem = separate_voice(video, log)
        except Exception as e: log(f"AI voice separation unavailable ({str(e)[:100]}). Using the filter version instead.")
    if stem:
        sh(*base, "-ss", str(start), "-to", str(end), "-i", stem, "-filter_complex",
           f"[0:a]{keep}[o];[1:a]acompressor=threshold=0.1:ratio=3:makeup=2,apad=whole_dur={dur:.3f},{only}[v];[o][v]{mix}[aout]", "-map", "[aout]", "-ac", "2", "-ar", "44100", out)
    else:
        sh(*base, "-vn", "-af", f"asplit=2[o][v];[o]{keep}[o2];[v]{chain},apad=whole_dur={dur:.3f},{only}[v2];[o2][v2]{mix}", "-ac", "2", "-ar", "44100", out)
    return out


def loudness_curve(path, step=0.1):
    """Loudness (dBFS) of the clip every `step` seconds, for the timeline picture."""
    pcm = sh("ffmpeg", "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "8000", "-f", "s16le", "-").stdout
    x = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768.0; n = int(8000 * step); k = len(x) // n
    if k == 0: return np.zeros(0)
    return 20 * np.log10(np.maximum(np.sqrt((x[:k * n].reshape(k, n) ** 2).mean(1)), 1e-5))


def suggest_loud_parts(db, step=0.1, rise=6.0, min_len=1.0, pad=0.3):
    """Stretches of the clip that are well above its normal loudness: [(t0, t1)]. Could be the game OR the streamer shouting."""
    if len(db) < 10: return []
    hot = db > (float(np.median(db)) + rise)
    runs, i = [], 0
    while i < len(hot):
        if hot[i]:
            j = i
            while j < len(hot) and hot[j]: j += 1
            runs.append([i * step, j * step]); i = j
        else: i += 1
    merged = []
    for a, b in runs:
        if merged and a - merged[-1][1] < 0.5: merged[-1][1] = b
        else: merged.append([a, b])
    total = len(db) * step
    return [(max(0.0, a - pad), min(total, b + pad)) for a, b in merged if b - a >= min_len]


MUSIC_FX = {"None": "", "Muffled (like from another room)": "lowpass=f=900", "Old radio": "highpass=f=400,lowpass=f=3400,acompressor=threshold=0.1:ratio=4",
            "Bass boosted": "bass=g=9,acompressor=threshold=0.2:ratio=3", "Slowed + reverb": "atempo=0.85,aecho=0.8:0.88:60:0.4",
            "Echo": "aecho=0.8:0.8:300:0.4", "Telephone": "highpass=f=500,lowpass=f=2800"}


def music_fx_chain(m):
    """Extra music-only filters from the 'More music options' panel (speed, bass/treble, a sound effect)."""
    ch = []
    sp = float(m.get("speed", 1.0) or 1.0)
    if abs(sp - 1.0) > 0.01: ch.append(f"atempo={min(2.0, max(0.5, sp)):.3f}")
    if m.get("fx") in MUSIC_FX and MUSIC_FX[m["fx"]]: ch.append(MUSIC_FX[m["fx"]])
    if abs(float(m.get("bass", 0) or 0)) > 0.4: ch.append(f"bass=g={float(m['bass']):.1f}")
    if abs(float(m.get("treble", 0) or 0)) > 0.4: ch.append(f"treble=g={float(m['treble']):.1f}")
    return ch


def music_active(m):
    return bool(m and m.get("on") and m.get("path") and os.path.exists(m["path"]))


def clip_active(ca):
    """True when the clip's own sound has been adjusted (volume, mute, fades, loudness, or louder/quieter parts)."""
    return bool(ca and (ca.get("mute") or abs(float(ca.get("vol", 1.0)) - 1.0) > 0.01 or float(ca.get("fin", 0)) > 0.01
                        or float(ca.get("fout", 0)) > 0.01 or ca.get("norm") or ca.get("ranges") or ca.get("fx") or ca.get("tts") or ca.get("sfx")))


def audio_active(voice, music, clip=None):
    return voice_active(voice) or music_active(music) or clip_active(clip)


VOICE_FX = {"Chipmunk": 1.6, "Squeaky mouse": 2.0, "Deep": 0.75, "Super deep": 0.55, "Giant (deepest)": 0.5}


def apply_voice_fx(src, out, parts, dur, start=0.0):
    """Chipmunk / deep voice on chosen parts: each part is pitch-shifted (same speed), and fades in/out against the normal sound so it blends instead of snapping."""
    ps = []
    for r in parts:
        a, b = max(0.0, float(r.get("t0", 0.0)) - start), min(dur, float(r.get("t1", dur)) - start)
        if b - a > 0.1: ps.append((a, b, VOICE_FX.get(r.get("kind", "Chipmunk"), 1.6), min(max(0.0, float(r.get("fade", 0.25))), (b - a) / 2)))
    if not ps:
        shutil.copyfile(src, out); return out
    n = len(ps); g = []
    labels = "".join(f"[s{i}]" for i in range(n))
    g.append(f"[0:a]aresample=44100,asplit={n + 1}[base]{labels}")
    gains = []
    for a, b, f, F in ps:
        F = max(F, 0.005)
        gains.append(f"(1-if(between(t,{a:.3f},{b:.3f}),min(1,min(t-{a:.3f},{b:.3f}-t)/{F:.3f}),0))")
    g.append(f"[base]volume='{'*'.join(gains)}':eval=frame[b0]")
    for i, (a, b, f, F) in enumerate(ps):
        fade = (f",afade=t=in:st=0:d={F:.3f},afade=t=out:st={max(0.0, b - a - F):.3f}:d={F:.3f}" if F > 0.01 else "")
        ms = int(a * 1000)
        g.append(f"[s{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS,asetrate={int(44100 * f)},aresample=44100,atempo={1 / f:.4f}{fade},adelay={ms}|{ms}[e{i}]")
    g.append("[b0]" + "".join(f"[e{i}]" for i in range(n)) + f"amix=inputs={n + 1}:duration=first:normalize=0,alimiter=limit=0.95[o]")
    sh("ffmpeg", "-y", "-v", "error", "-i", src, "-filter_complex", ";".join(g), "-map", "[o]", "-ac", "2", "-ar", "44100", out)
    return out


# ---------- sound effects from MyInstants (the soundboard buttons) ----------
MYI = "https://www.myinstants.com"
MYI_CHIPS = ("Trending", "Vine boom", "Airhorn", "Bruh", "Fail", "Sad violin", "Wow", "Laugh", "Oof", "Applause")


def parse_myinstants(html_text):
    """The sound buttons on a MyInstants page -> [{title, path}]. Each button plays an mp3 (play('/media/sounds/x.mp3', ...)) and has a title link next to it."""
    import html as _html
    hits = list(re.finditer(r"""play\(\s*['"]([^'"]+?\.(?:mp3|wav|ogg|m4a))['"]""", html_text, re.I)); out, seen = [], set()
    for n, m in enumerate(hits):
        path = m.group(1)
        if path in seen: continue
        seen.add(path)
        end = hits[n + 1].start() if n + 1 < len(hits) else len(html_text); chunk = html_text[m.end():min(end, m.end() + 1500)]
        t = re.search(r"""instant-link[^>]*>\s*([^<]+?)\s*</a>""", chunk, re.I) or re.search(r"""title=['"]([^'"]+)['"]""", chunk, re.I)
        title = _html.unescape(t.group(1)).strip() if t else ""
        if not title: title = os.path.splitext(os.path.basename(path))[0].replace("-", " ").replace("_", " ").strip().title() or "Sound"
        out.append(dict(title=title[:60], path=path))
    return out


def myinstants_search(term="", page=1):
    """Search MyInstants (blank = what's trending). Returns [{title, url}]."""
    term = (term or "").strip()
    url = f"{MYI}/en/search/?name={urllib.parse.quote(term)}&page={int(page)}" if term else f"{MYI}/en/index/us/?page={int(page)}"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36", "Accept-Language": "en"})
    r.raise_for_status()
    return [dict(title=x["title"], url=(x["path"] if x["path"].startswith("http") else MYI + (x["path"] if x["path"].startswith("/") else "/" + x["path"]))) for x in parse_myinstants(r.text)]


_SFX_CACHE = {}


def fetch_sfx(entry, max_seconds=30.0):
    """Download one MyInstants sound (once) and return a 44.1k stereo wav of it."""
    url = entry["url"]
    if url in _SFX_CACHE and os.path.exists(_SFX_CACHE[url]): return _SFX_CACHE[url]
    r = requests.get(url, timeout=40, headers={"User-Agent": "Mozilla/5.0", "Referer": MYI + "/"}); r.raise_for_status()
    ext = os.path.splitext(urllib.parse.urlparse(url).path)[1].lower() or ".mp3"
    key = abs(hash(url)); src = os.path.join(tempfile.gettempdir(), f"clipstudio_sfx_{key}{ext}"); wav = os.path.join(tempfile.gettempdir(), f"clipstudio_sfx_{key}.wav")
    open(src, "wb").write(r.content)
    sh("ffmpeg", "-y", "-v", "error", "-i", src, "-t", f"{max_seconds:.1f}", "-ac", "2", "-ar", "44100", wav); safe_remove(src, tries=1)
    _SFX_CACHE[url] = wav
    return wav


# ---------- text to speech: whatever voices this computer has (Windows / Mac / Linux), plus online ones when edge-tts is installed ----------
TTS_STYLES = {"Normal": 1.0, "Higher": 1.25, "Chipmunk": 1.6, "Lower": 0.85, "Deep": 0.72, "Giant": 0.55}
_TTS_VOICES = []


def _quiet():
    return dict(creationflags=0x08000000) if sys.platform == "win32" else {}


def tts_voices(refresh=False):
    """[(label, key)] of every voice found. key = 'sapi:Name' | 'say:Name' | 'espeak:voice' | 'edge:Name'."""
    global _TTS_VOICES
    if _TTS_VOICES and not refresh: return list(_TTS_VOICES)
    out = []
    try:
        if sys.platform == "win32":
            ps = "Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).GetInstalledVoices() | ForEach-Object { $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Gender + '|' + $_.VoiceInfo.Culture.Name }"
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=40, **_quiet())
            for ln in r.stdout.splitlines():
                parts = ln.strip().split("|")
                if parts and parts[0]: out.append((f"{parts[0]}  ({parts[1] if len(parts) > 1 else ''} {parts[2] if len(parts) > 2 else ''})".replace("  ( ", "  (").strip(), "sapi:" + parts[0]))
        elif sys.platform == "darwin":
            r = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=20)
            for ln in r.stdout.splitlines():
                m = re.match(r"^(.+?)\s{2,}([A-Za-z]{2,3}[_-][A-Za-z0-9]+)\s+#", ln)
                if m: out.append((f"{m.group(1).strip()}  ({m.group(2)})", "say:" + m.group(1).strip()))
        else:
            exe = shutil.which("espeak-ng") or shutil.which("espeak")
            if exe:
                for nm, key in (("English - man 1", "en-us+m1"), ("English - man 2", "en-us+m3"), ("English - man 3", "en-us+m7"), ("English - woman 1", "en-us+f1"), ("English - woman 2", "en-us+f3"),
                                ("English - woman 3", "en-us+f5"), ("English (UK) - man", "en-gb+m2"), ("English (UK) - woman", "en-gb+f2"), ("English - whisper", "en-us+whisper"), ("English - croaky", "en-us+croak")):
                    out.append((nm, "espeak:" + key))
    except Exception: pass
    try:
        import edge_tts                                                          # noqa: F401  (optional: pip install edge-tts, needs internet)
        for nm, key in (("Guy - man (online)", "en-US-GuyNeural"), ("Christopher - man (online)", "en-US-ChristopherNeural"), ("Eric - man (online)", "en-US-EricNeural"),
                        ("Jenny - woman (online)", "en-US-JennyNeural"), ("Aria - woman (online)", "en-US-AriaNeural"), ("Michelle - woman (online)", "en-US-MichelleNeural"),
                        ("Ryan - UK man (online)", "en-GB-RyanNeural"), ("Sonia - UK woman (online)", "en-GB-SoniaNeural"), ("Natasha - Australian woman (online)", "en-AU-NatashaNeural")):
            out.append((nm, "edge:" + key))
    except Exception: pass
    _TTS_VOICES = out
    return list(out)


def tts_make(text, voice_key, style, speed, out_wav, log=print):
    """Speak `text` with the chosen voice -> a 44.1k stereo wav. style = a TTS_STYLES name (pitch), speed = 0.5..2 (same pitch). Returns the length in seconds."""
    text = (text or "").strip()
    if not text: raise ValueError("Type something to say first.")
    kind, _, name = (voice_key or "").partition(":")
    tmp = tempfile.mkdtemp(prefix="clipstudio_tts_"); raw = os.path.join(tmp, "raw"); txt = os.path.join(tmp, "t.txt")
    try:
        with open(txt, "w", encoding="utf-8") as f: f.write(text)
        if kind == "sapi":
            raw += ".wav"; ps = ("$ErrorActionPreference='Stop'; Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
                                 "$s.SelectVoice($env:TTS_VOICE); $s.SetOutputToWaveFile($env:TTS_OUT); $s.Speak([IO.File]::ReadAllText($env:TTS_TEXT,[Text.Encoding]::UTF8)); $s.Dispose()")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True, timeout=180, env=dict(os.environ, TTS_VOICE=name, TTS_OUT=raw, TTS_TEXT=txt), **_quiet())
            if r.returncode: raise RuntimeError((r.stderr or "the Windows voice failed").strip()[:200])
        elif kind == "say":
            raw += ".aiff"; r = subprocess.run(["say", "-v", name, "-o", raw, "-f", txt], capture_output=True, text=True, timeout=180)
            if r.returncode: raise RuntimeError((r.stderr or "the Mac voice failed").strip()[:200])
        elif kind == "espeak":
            raw += ".wav"; exe = shutil.which("espeak-ng") or shutil.which("espeak")
            if not exe: raise RuntimeError("espeak isn't installed.")
            r = subprocess.run([exe, "-v", name, "-f", txt, "-w", raw], capture_output=True, text=True, timeout=180)
            if r.returncode: raise RuntimeError((r.stderr or "espeak failed").strip()[:200])
        elif kind == "edge":
            raw += ".mp3"; import asyncio, edge_tts
            asyncio.run(edge_tts.Communicate(text, name).save(raw))
        else:
            raise RuntimeError("No voice picked (or this computer has no text-to-speech voices).")
        f = TTS_STYLES.get(style, 1.0); sp = max(0.5, min(2.5, float(speed or 1.0))); ch = ["aresample=44100"]
        if abs(f - 1.0) > 0.01: ch += [f"asetrate={int(44100 * f)}", "aresample=44100", f"atempo={1 / f:.4f}"]
        if abs(sp - 1.0) > 0.01: ch.append(atempo_chain(sp))
        ch += ["silenceremove=start_periods=1:start_threshold=-50dB", "areverse", "silenceremove=start_periods=1:start_threshold=-50dB", "areverse"]      # trim silence off both ends
        sh("ffmpeg", "-y", "-v", "error", "-i", raw, "-af", ",".join(ch), "-ac", "2", "-ar", "44100", out_wav)
        return duration(out_wav)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def mix_tts(src, out, items, dur, start=0.0):
    """Lay each spoken line (item['wav'], placed at its t0, volume, optional dip of the clip's sound while it speaks) over the clip's sound."""
    its = []
    for it in items or []:
        w = it.get("wav")
        if not w or not os.path.exists(str(w)): continue
        a = float(it.get("t0", 0.0)) - start; skip = max(0.0, -a); a = max(0.0, a)
        b = min(dur, float(it.get("t1", 1e9)) - start)
        if b - a > 0.1: its.append((a, b, skip, str(w), max(0.0, float(it.get("vol", 1.0))), bool(it.get("duck", True))))
    if not its:
        if src != out: shutil.copyfile(src, out)
        return out
    g = []; ducks = [f"if(between(t,{a:.3f},{b:.3f}),0.3,1)" for a, b, _, _, _, d in its if d]
    g.append(f"[0:a]aresample=44100" + (f",volume='{'*'.join(ducks)}':eval=frame" if ducks else "") + "[b0]")
    for k, (a, b, skip, w, vol, d) in enumerate(its, 1):
        L = b - a; ms = int(a * 1000)
        g.append(f"[{k}:a]atrim=start={skip:.3f}:end={skip + L:.3f},asetpts=PTS-STARTPTS,afade=t=out:st={max(0.0, L - 0.05):.3f}:d=0.05,volume={vol:.3f},adelay={ms}|{ms}[e{k}]")
    g.append("[b0]" + "".join(f"[e{k}]" for k in range(1, len(its) + 1)) + f"amix=inputs={len(its) + 1}:duration=first:normalize=0,alimiter=limit=0.95[o]")
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", src]
    for it in its: cmd += ["-i", it[3]]
    sh(*cmd, "-filter_complex", ";".join(g), "-map", "[o]", "-ac", "2", "-ar", "44100", out)
    return out


def apply_clip_audio(src, out, ca, dur, start=0.0):
    """Volume / mute / fades / even-loudness / per-part volume on the clip's own sound (music is mixed in AFTER this, so it isn't affected)."""
    if ca.get("fx"):                                                      # chipmunk / deep voice parts first, then the volume settings on top
        fxtmp = out + ".fx.wav"
        try: apply_voice_fx(src, fxtmp, ca["fx"], dur, start); src = fxtmp
        except Exception: safe_remove(fxtmp, tries=1); raise
    ch = []
    for r in ca.get("ranges") or []:
        a, b = max(0.0, r.get("t0", 0.0) - start), min(dur, r.get("t1", dur) - start)
        if b - a > 0.02: ch.append(f"volume='if(between(t,{a:.2f},{b:.2f}),{max(0.0, float(r.get('gain', 0.5))):.3f},1)':eval=frame")
    if ca.get("norm"): ch += ["loudnorm=I=-16:TP=-1.5:LRA=11", "aresample=44100"]
    ch.append(f"volume={0.0 if ca.get('mute') else max(0.0, float(ca.get('vol', 1.0))):.3f}")
    fin, fout = max(0.0, min(float(ca.get("fin", 0)), dur)), max(0.0, min(float(ca.get("fout", 0)), dur))
    if fin > 0.01: ch.append(f"afade=t=in:st=0:d={fin:.2f}")
    if fout > 0.01: ch.append(f"afade=t=out:st={max(0.0, dur - fout):.2f}:d={fout:.2f}")
    ch.append("alimiter=limit=0.95")
    sh("ffmpeg", "-y", "-v", "error", "-i", src, "-af", ",".join(ch), "-ac", "2", "-ar", "44100", out)
    safe_remove(out + ".fx.wav", tries=1)
    if ca.get("tts") or ca.get("sfx"):                                    # spoken lines + sound effects go on last, so the clip volume / fades don't change them
        mt = out + ".tts.wav"
        try: mix_tts(out, mt, list(ca.get("tts") or []) + list(ca.get("sfx") or []), dur, start); shutil.move(mt, out)
        finally: safe_remove(mt, tries=1)


def build_audio(video, start, end, voice, music, out, log=print, clip=None):
    """The clip's finished audio (start..end of `video`) as a wav: voice focus where chosen, then a music track mixed in with
    volume, fades and (optionally) lowering under speech. All times in `voice`/`music` are seconds into the raw clip."""
    dur = end - start
    base_voice = voice if voice_active(voice) else dict(on=False, ranges=[])
    if not music_active(music):
        if not clip_active(clip): return make_voice_audio(video, start, end, base_voice, out, log)
        tmp = out + ".v.wav"
        try: make_voice_audio(video, start, end, base_voice, tmp, log); apply_clip_audio(tmp, out, clip, dur, start)
        finally: safe_remove(tmp, tries=2)
        return out
    m = music; base = out + ".base.wav"
    make_voice_audio(video, start, end, base_voice, base, log)
    if clip_active(clip):
        b2 = out + ".b2.wav"; apply_clip_audio(base, b2, clip, dur, start); shutil.move(b2, base)
    at, skip = m.get("at", 0.0) - start, float(m.get("ss", 0.0))
    if at < 0: skip, at = skip - at, 0.0                                     # the AI trim cut off the beginning: start the song further in
    until = min(dur, m.get("until", end) - start); mdur = until - at
    try: mlen = duration(m["path"])
    except Exception: mlen = 30.0
    if mdur < 0.3 or (skip >= mlen - 0.2 and not m.get("loop", True)):
        shutil.move(base, out); return out
    fin, fout = max(0.0, min(float(m.get("fin", 0.0)), mdur)), max(0.0, min(float(m.get("fout", 0.0)), mdur))
    ms = int(at * 1000)
    ch = [f"aresample=44100", "aformat=sample_fmts=fltp:channel_layouts=stereo", f"atrim=start={min(skip, max(0.0, mlen - 0.3)):.3f}", "asetpts=PTS-STARTPTS"] + music_fx_chain(m)
    if m.get("loop", True): ch.append("aloop=loop=-1:size=2147483647")
    ch += [f"atrim=duration={mdur:.3f}", "asetpts=PTS-STARTPTS"]
    if fin > 0.01: ch.append(f"afade=t=in:st=0:d={fin:.2f}")
    if fout > 0.01: ch.append(f"afade=t=out:st={max(0.0, mdur - fout):.2f}:d={fout:.2f}")
    ch += [f"volume={float(m.get('vol', 0.4)):.3f}", f"adelay={ms}|{ms}", f"apad=whole_dur={dur:.3f}"]
    orig = f"volume={float(m.get('orig', 1.0)):.3f}"
    if m.get("duck", True):
        g = (f"[1:a]{','.join(ch)}[m0];[0:a]asplit=2[a0][sc];[m0][sc]sidechaincompress=threshold=0.015:ratio={2 + 18 * min(1.0, max(0.0, float(m.get('duck_amt', 0.55)))):.1f}:attack=20:release=500:makeup=1[md];[a0]{orig}[ao];")
    else:
        g = f"[1:a]{','.join(ch)}[md];[0:a]{orig}[ao];"
    tail = ["amix=inputs=2:duration=longest:dropout_transition=0", "volume=2"]
    mi, mo = max(0.0, float(m.get("min", 0.0))), max(0.0, float(m.get("mout", 0.0)))   # fades for the whole mixed sound
    if mi > 0.01: tail.append(f"afade=t=in:st=0:d={min(mi, dur):.2f}")
    if mo > 0.01: tail.append(f"afade=t=out:st={max(0.0, dur - mo):.2f}:d={min(mo, dur):.2f}")
    tail.append("alimiter=limit=0.95")
    g += f"[ao][md]{','.join(tail)}[aout]"
    try:
        sh("ffmpeg", "-y", "-v", "error", "-i", base, "-i", m["path"], "-filter_complex", g, "-map", "[aout]", "-ac", "2", "-ar", "44100", out)
    finally:
        safe_remove(base, tries=2)
    return out


def unversion(path):
    """clip_x_v2_raw.mp4 -> clip_x_raw.mp4 (spliced copies carry a version number while you edit; finished files don't)."""
    return re.sub(r"_v\d+(_raw\.mp4)$", r"\1", str(path))


def has_audio(path):
    r = subprocess.run(["ffmpeg", "-i", path], capture_output=True, text=True)
    return "Audio:" in r.stderr


def kept_segments(cuts, dur):
    """cuts = [(a,b)] parts to throw away -> the parts to keep, as [(a,b)] (merged, in order)."""
    cs = sorted((max(0.0, float(a)), min(float(dur), float(b))) for a, b in cuts if b > a)
    merged = []
    for a, b in cs:
        if merged and a <= merged[-1][1] + 0.02: merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else: merged.append((a, b))
    kept, cur = [], 0.0
    for a, b in merged:
        if a - cur > 0.1: kept.append((cur, a))
        cur = max(cur, b)
    if dur - cur > 0.1: kept.append((cur, float(dur)))
    return kept


def cut_mapper(kept):
    """Returns f(t): a time in the ORIGINAL clip -> the matching time in the clip with the cuts taken out."""
    def f(t):
        acc = 0.0
        for a, b in kept:
            if t < a: return acc
            if t <= b: return acc + (t - a)
            acc += b - a
        return acc
    return f


def cut_clip(raw, kept, out, log=print):
    aud = has_audio(raw); parts = []
    for i, (a, b) in enumerate(kept):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[v{i}]")
        if aud: parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[a{i}]")
    ins = "".join(f"[v{i}]" + (f"[a{i}]" if aud else "") for i in range(len(kept)))
    parts.append(f"{ins}concat=n={len(kept)}:v=1:a={1 if aud else 0}[v]" + ("[a]" if aud else ""))
    sh("ffmpeg", "-y", "-v", "error", "-i", raw, "-filter_complex", ";".join(parts), "-map", "[v]", *(["-map", "[a]"] if aud else []),
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", *(["-c:a", "aac"] if aud else []), out)


def retime_mapper(pieces):
    """pieces = [(a, b, factor)] covering the clip. Returns f(t): time in the OLD clip -> time in the retimed clip."""
    def f(t):
        acc = 0.0
        for a, b, k in pieces:
            if t < a: return acc
            if t <= b: return acc + (t - a) / k
            acc += (b - a) / k
        return acc
    return f


def atempo_chain(k):
    """atempo only accepts 0.5-2 per step, so bigger / smaller speeds are chained."""
    out = []
    while k > 2.0: out.append("atempo=2.0"); k /= 2.0
    while k < 0.5: out.append("atempo=0.5"); k /= 0.5
    out.append(f"atempo={k:.4f}"); return ",".join(out)


def retime_clip(raw, pieces, out, log=print):
    """Plays parts of the clip faster / slower (pitch of the sound is kept). The result is a normal clip, so everything else keeps working on it."""
    aud = has_audio(raw); fps = clip_fps(raw); parts = []
    for i, (a, b, k) in enumerate(pieces):
        parts.append(f"[0:v]trim=start={a:.3f}:end={b:.3f},setpts=(PTS-STARTPTS)/{k:.4f},fps={fps:.3f}[v{i}]")
        if aud: parts.append(f"[0:a]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS" + ("," + atempo_chain(k) if abs(k - 1) > 0.001 else "") + f"[a{i}]")
    ins = "".join(f"[v{i}]" + (f"[a{i}]" if aud else "") for i in range(len(pieces)))
    parts.append(f"{ins}concat=n={len(pieces)}:v=1:a={1 if aud else 0}[v]" + ("[a]" if aud else ""))
    sh("ffmpeg", "-y", "-v", "error", "-i", raw, "-filter_complex", ";".join(parts), "-map", "[v]", *(["-map", "[a]"] if aud else []),
       "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p", *(["-c:a", "aac"] if aud else []), out)


_FPS_CACHE = {}


def clip_fps(path):
    """The video's real frame rate (e.g. 29.97 / 60), 30 if it can't be read."""
    if path not in _FPS_CACHE:
        try:
            r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path], capture_output=True, text=True).stderr
            m = re.search(r"([\d.]+) fps", r); _FPS_CACHE[path] = float(m.group(1)) if m and 1 < float(m.group(1)) < 241 else 30.0
        except Exception: _FPS_CACHE[path] = 30.0
    return _FPS_CACHE[path]


def video_fps(path):
    r = subprocess.run(["ffmpeg", "-hide_banner", "-i", path], capture_output=True, text=True)
    m = re.search(r"([\d.]+) fps", r.stderr)
    return 60 if m and float(m.group(1)) > 45 else 30


def concat_clips(paths, out, size, fps=30, log=print):
    """Join several video files into one, end to end. Every piece is fitted to `size` (black bars if the shape differs), given the same
    frame rate and audio format, and clips with no sound get silence - so any mix of clips joins cleanly."""
    w, h = size; w, h = w - w % 2, h - h % 2; parts, ins = [], ""
    for i, pth in enumerate(paths):
        parts.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1,fps={fps},format=yuv420p[v{i}]")
        if has_audio(pth): parts.append(f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]")
        else: parts.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={duration(pth):.3f},asetpts=PTS-STARTPTS[a{i}]")
        ins += f"[v{i}][a{i}]"
    parts.append(f"{ins}concat=n={len(paths)}:v=1:a=1[v][a]")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    for pth in paths: cmd += ["-i", pth]
    sh(*cmd, "-filter_complex", ";".join(parts), "-map", "[v]", "-map", "[a]", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16",
       "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", out)


def remap_opts(o, mp):
    """Move every timed thing (blur boxes, text, voice parts, music) to where it lands after the cuts."""
    o = dict(o)
    def rng(items):
        out = []
        for it in items or []:
            if "t0" not in it and "t1" not in it: out.append(it); continue
            a, b = mp(it.get("t0", 0.0)), mp(it.get("t1", 1e9))
            if b - a >= 0.05: n = map_item_times(it, mp); n["t0"], n["t1"] = a, b; out.append(n)
        return out
    o["blurs"] = rng(o.get("blurs")); o["texts"] = rng(o.get("texts")); o["overlays"] = rng(o.get("overlays"))
    v = json.loads(json.dumps(o.get("voice") or {})); v["ranges"] = rng(v.get("ranges")); o["voice"] = v
    m = dict(o.get("music") or {})
    if m.get("path"):
        m["at"], m["until"] = mp(m.get("at", 0.0)), mp(m.get("until", 1e9))
        if m["until"] - m["at"] < 0.3: m["on"] = False
    o["music"] = m
    ca = json.loads(json.dumps(o.get("clip_audio") or {})); ca["ranges"] = rng(ca.get("ranges")); ca["fx"] = rng(ca.get("fx")); ca["tts"] = rng(ca.get("tts")); ca["sfx"] = rng(ca.get("sfx")); o["clip_audio"] = ca
    return o


def process_clip(raw, final, opts, chat_msgs=None, log=print, words=None):
    """Applies the cuts you made on the timeline first (into a temporary clip), then does everything else on that."""
    cuts = opts.get("cuts") or []; tmp = None
    if cuts:
        dur0 = duration(raw); kept = kept_segments(cuts, dur0)
        if not kept: raise RuntimeError("Everything was cut out of the clip.")
        if kept != [(0.0, dur0)] and not (len(kept) == 1 and kept[0][0] <= 0.02 and kept[0][1] >= dur0 - 0.02):
            tmp = os.path.join(tempfile.gettempdir(), f"clipstudio_cut_{os.getpid()}_{abs(hash(final))}.mp4")
            log(f"Applying your cuts ({dur0:.1f}s -> {sum(b - a for a, b in kept):.1f}s)...")
            cut_clip(raw, kept, tmp); mp = cut_mapper(kept)
            opts = remap_opts(opts, mp); opts["_cut"] = True
            if words is not None: words = [dict(w, s=mp(w["s"]), e=mp(w["e"])) for w in words if mp(w["e"]) - mp(w["s"]) >= 0.02]
            raw = tmp
    try: return _process_clip(raw, final, opts, chat_msgs, log, words)
    finally:
        if tmp: safe_remove(tmp)


def _process_clip(raw, final, opts, chat_msgs=None, log=print, words=None):
    """The ONE place a clip gets edited (auto mode and the review window both use it).
    opts: vertical, layout ('blur'|'crop'), captions, ai, credit, hook.  Returns dict(final, title, hook, ai, dur)."""
    dur = duration(raw); start, end = 0, dur
    hook, ai = (opts.get("hook") or "").strip(), None
    if words is None and (opts.get("captions") or opts.get("ai")):
        log("Transcribing on this PC (free; the first run downloads the Whisper model)...")
        try: words = transcribe(raw, 0, dur)
        except CaptionError as e: log(f"Captions skipped: {e}"); words = []
    words = words or []
    if opts.get("ai"):
        try:
            ai = judge(raw, {"start": 0, "end": dur}, words, chat_msgs or [])
            start, end = max(0, ai["start"]), min(dur, ai["end"])
            hook = hook or ("" if any(t.get("hook") for t in (opts.get("texts") or [])) else ai.get("hook", ""))     # a hook you typed yourself wins over the AI's
            log(f"AI: {ai['score']}/10 - {ai['reason']}")
        except Exception as e:
            log(f"AI step failed ({str(e)[:120]}). Using the full clip instead; nothing lost.")
            ai = None
    credit = (opts.get("credit") or "").strip()
    layout, cam = opts.get("layout", "blur"), opts.get("cam")
    if cam and "ar" not in cam:                              # a remembered box is stored as plain fractions
        cam = cam_from_fractions(cam["x"], cam["y"], cam["w"], cam["h"], raw)
    try: vw_, vh_ = video_size(raw); portrait = vw_ / vh_ < 0.75
    except Exception: portrait = False
    if layout == "facecam" and opts.get("vertical") and not cam and not portrait:
        try: cam = detect_facecam(raw, log=log)
        except Exception as e: log(f"Facecam detection failed ({str(e)[:100]})."); cam = None
        if cam: log(f"Facecam found ({cam['hits']}/{cam['samples']} frames): placing it on top.")
    if layout == "facecam" and not cam:
        layout = "blur"; log("Using the blurred-background layout instead (no facecam box).")
    trimmed = (start, end) != (0, dur)
    blurs = opts.get("blurs") or []
    if not (opts.get("vertical") or opts.get("captions") or credit or hook or trimmed or blurs or opts.get("overlays") or audio_active(opts.get("voice"), opts.get("music"), opts.get("clip_audio")) or opts.get("texts") or opts.get("res") or opts.get("_cut")
            or os.path.splitext(final)[1].lower() not in ("", ".mp4")):
        return dict(final=raw, title="", hook="", ai=None, dur=dur)       # nothing to do -> keep the original
    render(raw, start, end, words if opts.get("captions") else [], final, bool(opts.get("vertical")),
           layout, hook, credit, opts.get("style"), cam, opts.get("gx"), blurs, opts.get("voice"), log, opts.get("music"),
           opts.get("texts"), opts.get("crf", 20), opts.get("res"), opts.get("clip_audio"), opts.get("overlays"), opts.get("camq", 0), opts.get("borders", True))
    return dict(final=final, title=(ai or {}).get("title", ""), hook=hook, ai=ai, dur=end - start, cam=cam)


class PlayClock:
    """One shared 'where are we in the clip' clock. Every preview that uses the same clock shows the same moment, and pausing the
    clock pauses them all on the same frame. pos() = seconds into the clip; dur > 0 makes it loop (epoch counts the loops);
    gen changes whenever someone seeks."""
    def __init__(self, dur=0.0, paused=False):
        self.lock = threading.Lock(); self.dur = float(dur); self.paused = paused
        self.base_pos, self.base_t, self.gen, self.epoch = 0.0, time.time(), 0, 0

    def _pos(self):
        if self.paused: return self.base_pos
        now = time.time(); p = self.base_pos + (now - self.base_t)
        if self.dur > 0 and p >= self.dur:
            n = int(p // self.dur); self.epoch += n; p -= n * self.dur; self.base_pos, self.base_t = p, now
        return p

    def pos(self):
        with self.lock: return self._pos()

    def pause(self):
        with self.lock: self.base_pos = self._pos(); self.paused = True

    def resume(self):
        with self.lock:
            if self.paused: self.paused = False; self.base_t = time.time()

    def toggle(self):
        self.resume() if self.paused else self.pause()

    def seek(self, t, paused=None):
        with self.lock:
            t = max(0.0, float(t))
            if self.dur > 0: t = min(t, max(0.0, self.dur - 0.05))
            self.base_pos, self.base_t = t, time.time(); self.gen += 1
            if paused is not None: self.paused = paused

    def loop(self):                                       # a single, un-shared preview reached its end
        with self.lock: self.base_pos, self.base_t = 0.0, time.time(); self.epoch += 1


class FramePump:
    """Plays a video through ffmpeg into raw RGB frames; .frame is always the latest one. Frames are shown when the PlayClock says it's
    their time (not when ffmpeg happens to finish them), so several pumps on one clock stay on the same frame, pausing is instant, and
    seeking restarts decoding at the new spot. No tkinter in here (so it can be tested)."""
    def __init__(self, w, h, fps=20, clock=None):
        self.w, self.h, self.fps = w, h, fps
        self.frame, self.count, self.proc, self.failed, self.err, self._gen = None, 0, None, False, "", 0
        self.fsize = None                        # (w, h) the latest frame was really made at
        self.owns_clock = clock is None; self.clock = clock or PlayClock()
        self.ready = False; self._autoresume = False
        self.on_pass = None                      # (unused now; kept so older code keeps working)

    def play(self, path, vf, loop=True, pre=()):
        self.stop(); self.frame, self.count, self.failed, self.ready, self.fsize = None, 0, False, False, None
        if self.owns_clock:                      # a stand-alone preview starts at 0 and only begins running once its first frame is up
            self.clock.seek(0, paused=True); self._autoresume = True
        threading.Thread(target=self._run, args=(path, vf, loop, self._gen, self.w, self.h, tuple(pre)), daemon=True).start()

    def _open(self, path, vf, pre, start):
        cmd = ["ffmpeg", "-loglevel", "error"]
        if start > 0.01: cmd += ["-ss", f"{start:.3f}", "-copyts"]       # -copyts: filters (captions, blur timing) still see the real time
        cmd += [*pre, "-i", path, "-an", "-vf", f"{vf},fps={self.fps}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def _wait(self, ft, my_gen, my_epoch, gen):
        c = self.clock
        while gen == self._gen:
            if c.gen != my_gen: return "seek"
            cp = c.pos()
            if c.epoch != my_epoch: return "wrap"
            if cp >= ft - 0.004: return "ok"
            time.sleep(min(0.02, max(0.002, ft - cp)))
        return "dead"

    @staticmethod
    def _kill(p):
        if p:
            try: p.kill(); p.wait(timeout=2)
            except Exception: pass

    def _run(self, path, vf, loop, gen, w, h, pre=()):
        n, c = w * h * 3, self.clock; nxt = None; p = None
        try:
            while gen == self._gen:
                my_gen, my_epoch = c.gen, c.epoch
                if nxt is not None: p, start, nxt = nxt, 0.0, None
                else: start = c.pos(); p = self._open(path, vf, pre, start)
                self.proc = p; k = got = 0; reason = "eof"
                while gen == self._gen:
                    buf = p.stdout.read(n)
                    if len(buf) < n: reason = "eof"; break
                    ft = start + k / self.fps; k += 1; got += 1
                    st = self._wait(ft, my_gen, my_epoch, gen)
                    if st != "ok": reason = st; break
                    if k == 1 or c.pos() - ft < 0.25:                          # far behind (slow computer)? skip frames to catch up
                        self.frame = buf; self.fsize = (w, h); self.count += 1
                        if not self.ready:
                            self.ready = True
                            if self._autoresume: self._autoresume = False; c.resume()
                if gen != self._gen: break
                if reason == "eof" and got == 0:                                # bad filter / unreadable file: don't loop forever
                    self.failed, self.err = True, p.stderr.read().decode("utf-8", "ignore")[-300:]; break
                self._kill(p)
                if reason != "eof": continue                                    # seek / loop wrap: reopen at the clock's position
                if not loop: break
                if c.dur <= 0:                                                  # stand-alone: restart from 0, run once the first frame is up
                    c.seek(0, paused=True); self._autoresume = True; self.ready = False; continue
                nxt = self._open(path, vf, pre, 0.0)                            # shared clock: have the next loop ready, wait for the wrap
                while gen == self._gen and c.gen == my_gen:
                    c.pos()
                    if c.epoch != my_epoch: break
                    time.sleep(0.02)
                if not (gen == self._gen and c.gen == my_gen): self._kill(nxt); nxt = None
        except Exception as e:
            if gen == self._gen: self.failed, self.err = True, f"{type(e).__name__}: {e}"
        finally:
            self._kill(p); self._kill(nxt)

    def stop(self):
        self._gen += 1
        p, self.proc = self.proc, None
        self._kill(p)


# ---------- fetching only the needed piece ----------
def fetch_piece(vod_id, start, end, out, tdl=None):
    if tdl or shutil.which("TwitchDownloaderCLI"):
        sh(tdl or "TwitchDownloaderCLI", "videodownload", "--id", str(vod_id), "-b", hms(start), "-e", hms(end), "-o", out)
    else:  # yt-dlp can also grab just a section of a VOD
        sh("yt-dlp", "--download-sections", f"*{int(start)}-{int(end)}", "--force-keyframes-at-cuts", "-f", "best[height<=1080]/best",
           "--remux-video", "mp4", "-o", out, f"https://www.twitch.tv/videos/{vod_id}")


def cli_main(argv=None):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(prog='clip_studio.py clip', description=CLI_HELP, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--vod", help="Twitch VOD id or URL"); ap.add_argument("--chat", help="chat.json (TwitchDownloader or ours)")
    ap.add_argument("--video", help="local video file instead of downloading pieces"); ap.add_argument("--tdl", help="path to TwitchDownloaderCLI")
    ap.add_argument("--analyze-only", action="store_true", help="only list the moments")
    ap.add_argument("--top", type=int, default=5); ap.add_argument("--before", type=int, default=30, help="sec before chat STARTS rising")
    ap.add_argument("--after", type=int, default=10); ap.add_argument("--gap", type=int, default=90); ap.add_argument("--bucket", type=int, default=10)
    ap.add_argument("--out", default="clips"); ap.add_argument("--audio", action="store_true", help="add loudness to the score (needs --video)")
    ap.add_argument("--landscape", action="store_true"); ap.add_argument("--layout", choices=["blur", "crop", "facecam"], default="blur", help="facecam = streamer cam on top, gameplay below")
    ap.add_argument("--cam", help="facecam box as fractions x,y,w,h (e.g. 0.74,0.62,0.24,0.34); default: auto-detect")
    ap.add_argument("--no-captions", action="store_true"); ap.add_argument("--ai", action="store_true", help="Claude trims/titles/hooks (needs ANTHROPIC_API_KEY)")
    ap.add_argument("--max-size-mb", type=float, default=0, help="stop cutting once the output folder reaches this size (0 = no limit)")
    ap.add_argument("--credit", default="", help="e.g. @streamer, burned in small on every clip")
    ap.add_argument("--caption-preset", default="Bold Yellow", choices=list(CAPTION_PRESETS), help="caption look")
    ap.add_argument("--whisper-model", default="medium", help="tiny | base | small | medium | large-v3 | distil-large-v3 (bigger = more accurate, slower)")
    ap.add_argument("--language", default="en", help="e.g. en, es ('auto' = auto-detect; auto-detect often guesses wrong on noisy game audio)")
    a = ap.parse_args(argv); os.makedirs(a.out, exist_ok=True)
    WHISPER.update(model=a.whisper_model, language=None if a.language == "auto" else a.language); style = {**CAPTION_DEFAULT, **CAPTION_PRESETS[a.caption_preset]}

    vod_id = re.search(r"(\d{6,})", a.vod).group(1) if a.vod else None
    if a.chat: msgs, length = load_chat(a.chat, with_length=True)
    elif a.vod: msgs, length = get_chat(vod_id, os.path.join(a.out, "chat.json"), a.tdl)
    else: sys.exit("Need --chat chat.json or --vod ID")
    audio = audio_bins(a.video, a.bucket) if (a.audio and a.video) else None
    peaks, cnt = analyze(msgs, length, a.bucket, a.top, a.before, a.after, a.gap, audio)
    report(peaks, cnt, msgs, max(length or 0, max(m["t"] for m in msgs)), a.bucket, vod_id)
    json.dump(peaks, open(os.path.join(a.out, "moments.json"), "w"), indent=2)
    if a.analyze_only or not (vod_id or a.video):
        return print(f"\nSaved {a.out}/moments.json. Add --vod/--video to cut the clips.")

    results = []
    for k, p in enumerate(peaks, 1):
        if a.max_size_mb and folder_mb(a.out) >= a.max_size_mb:
            print(f"\nSize limit reached ({folder_mb(a.out):.1f} / {a.max_size_mb:g} MB). Skipping the remaining clips."); break
        print(f"\n[{k}/{len(peaks)}] {hms(p['start'])}-{hms(p['end'])}")
        piece = os.path.join(a.out, f"piece_{k}.mp4")
        if a.video:
            sh("ffmpeg", "-y", "-ss", str(p["start"]), "-t", str(p["end"] - p["start"]), "-i", a.video, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-c:a", "aac", piece)
        else:
            fetch_piece(vod_id, p["start"], p["end"], piece, a.tdl)
        dur = duration(piece); words = []
        if not (a.no_captions and not a.ai):
            try: words = transcribe(piece, 0, dur)
            except CaptionError as ex: print(f"  Captions skipped: {ex}")
        s, e, title, hook, rank = 0, dur, "clip", "", p["score"]
        if a.ai:
            rel = [{"t": m["t"] - p["start"], "user": m["user"], "text": m["text"]} for m in msgs if p["start"] <= m["t"] <= p["end"]]
            try:
                j = judge(piece, {"start": 0, "end": dur}, words, rel)
                s, e = max(0, j["start"]), min(dur, j["end"]); title, hook, rank = j["title"], j.get("hook", ""), j["score"] + 100
                print(f"  AI {j['score']}/10: {j['reason']}")
            except Exception as ex:
                print("  AI judge failed, using chat timing:", ex)
        tmp = os.path.join(a.out, f"tmp_{k}.mp4")
        cam, layout = None, a.layout
        if layout == "facecam" and not a.landscape:
            try: cam = cam_from_fractions(*[float(v) for v in a.cam.split(",")], piece) if a.cam else detect_facecam(piece)
            except Exception as ex: print(f"  Facecam problem: {ex}")
            print("  Facecam found." if cam else "  No facecam found - using the blurred layout for this clip.")
            if not cam: layout = "blur"
        render(piece, s, e, [] if a.no_captions else words, tmp, not a.landscape, layout, hook, a.credit, style, cam)
        os.remove(piece); results.append((rank, tmp, title, p))
    for n, (rank, tmp, title, p) in enumerate(sorted(results, key=lambda r: -r[0]), 1):
        final = os.path.join(a.out, f"{n:02d}_{re.sub(r'[^a-z0-9]+', '_', title.lower()).strip('_')[:40]}.mp4")
        os.replace(tmp, final); print("wrote", final)
    print(f"\nDone. Streamer credit reminder: only clip creators who allow it, and credit them (--credit @name).")


# ============================================================================
# 6. LIVE GUI
# ============================================================================
BG, PANEL, FG, PURPLE = "#18181b", "#1f1f23", "#efeff1", "#9147ff"
GREEN, RED = "#00c853", "#ff1744"
YELLOW = RED            # no yellow anywhere in the app: everything that used to be yellow is red now
PURPLE_HI = "#a970ff"
DIM = {GREEN: "#0b3d1f", RED: "#4a0a14"}


class Collapsible:
    """A section with a clickable header that shows/hides its contents - keeps screens calm: only the basics are open, the rest is one click away."""
    def __init__(self, parent, title, open=False, bg=None, fg=None, pack=None, body_bg=None):
        self.bg = bg or BG; self.title, self.is_open = title, False
        self.box = tk.Frame(parent, bg=self.bg)
        self.head = tk.Button(self.box, text="", bg=self.bg, fg=fg or "#adadb8", relief="flat", anchor="w", bd=0, activebackground=self.bg,
                              activeforeground=FG, font=("Segoe UI", 9, "bold"), command=self.toggle)
        self.head.pack(fill="x")
        self.body = tk.Frame(self.box, bg=body_bg or self.bg)
        self.set(open)
        if pack is not False: self.box.pack(**(pack or dict(fill="x", padx=12, pady=(4, 0))))

    def set(self, on):
        self.is_open = bool(on)
        self.head.config(text=("▾  " if on else "▸  ") + self.title)
        if on: self.body.pack(fill="x", padx=(10, 0), pady=(2, 4))
        else: self.body.pack_forget()

    def toggle(self): self.set(not self.is_open)


def purple_menu(parent, var, *values, **kw):
    """A drop-down menu (tk.OptionMenu) in the app's purple instead of the default white box."""
    om = tk.OptionMenu(parent, var, *values, **kw)
    try:
        om.config(bg=PURPLE, fg="white", activebackground=PURPLE_HI, activeforeground="white", relief="flat", bd=0,
                  highlightthickness=0, padx=10, pady=3, indicatoron=True)
        om["menu"].config(bg=PURPLE, fg="white", activebackground=PURPLE_HI, activeforeground="white", relief="flat", bd=0)
    except Exception:
        pass
    return om


def purple_scrollbar(parent, command):
    """A purple scrollbar (the normal tk one is always white/gray on Windows). Falls back to the plain one if ttk isn't there."""
    try:
        from tkinter import ttk
        st = ttk.Style(parent)
        try: st.theme_use("clam")
        except Exception: pass
        st.configure("Purple.Vertical.TScrollbar", background=PURPLE, troughcolor=BG, bordercolor=BG, lightcolor=PURPLE, darkcolor=PURPLE,
                     arrowcolor="white", relief="flat")
        st.map("Purple.Vertical.TScrollbar", background=[("active", PURPLE_HI), ("pressed", PURPLE_HI)])
        return ttk.Scrollbar(parent, orient="vertical", style="Purple.Vertical.TScrollbar", command=command)
    except Exception:
        return tk.Scrollbar(parent, command=command)


def purple_scrolled_text(parent, **kw):
    """ScrolledText with the purple scrollbar."""
    st = scrolledtext.ScrolledText(parent, **kw)
    try:
        st.vbar.pack_forget()
        sb = purple_scrollbar(st.frame, st.yview); st.config(yscrollcommand=sb.set); sb.pack(side="right", fill="y", before=st)
    except Exception:
        pass
    return st
SCALE = 1.5  # meter full-scale = 1.5x trigger level
TRIGGER_MODES = ["Either: chat OR streamer gets loud", "Chat peak only", "Streamer loud only", "Both together (strictest)"]


# ---------- destination folder ----------
CLIPS_DIR = "clips"


def set_clips_dir(path):
    """Use `path` for finished clips (creates it; raises OSError if it can't be written to)."""
    global CLIPS_DIR
    os.makedirs(path, exist_ok=True)
    t = os.path.join(path, ".write_test"); open(t, "w").close(); os.remove(t)
    CLIPS_DIR = path
    return path


# ---------- trending Shorts sounds: chart data + 30-second previews ----------
# Starter list: ONLY entries verified from YouTube Charts' "Top Shorts Songs" (global, weekly), week of Nov 6-12 2026,
# as reported by PPC Land. Rank 5 is missing because the report doesn't name it. Import the full chart for the rest.
# Hide-Indian-music filter (for a US audience). The chart file has no language/country column, so this looks for Indic
# scripts plus a list of common Indian labels/artists/genres. It is a best effort: pick "United States" on YouTube Charts for the real fix.
INDIC_RE = re.compile("[\u0900-\u0DFF]")
INDIAN_WORDS = ("diwali", "bollywood", "punjabi", "hindi", "tamil", "telugu", "bhojpuri", "haryanvi", "t-series", "tseries", "zee music",
                "tips official", "sony music india", "saregama", "vishal", "shekhar", "sunidhi", "arijit", "sartaaj", "satinder", "anil rawat",
                "kajal hathrasi", "mohit love", "diljit", "sidhu", "badshah", "honey singh", "neha kakkar", "shreya ghoshal", "atif aslam",
                "pritam", "anirudh", "a.r. rahman", "ar rahman", "jubin", "sonu nigam", "kumar sanu", "alka yagnik", "udit narayan",
                "jalte diye", "khanak", "payal ki", "guzarish", "kesariya", "pushpa", "rahat fateh", "ap dhillon", "karan aujla", "king (")


def is_indian_music(entry):
    text = f"{entry.get('title', '')} {entry.get('artist', '')}"
    return bool(INDIC_RE.search(text)) or any(w in text.lower() for w in INDIAN_WORDS)


def filter_entries(entries, hide_indian=True):
    return [e for e in entries if not (hide_indian and is_indian_music(e))]


STARTER_SOUNDS = dict(
    week="week of Nov 6-12, 2026 (global)", source="YouTube Charts - Top Shorts Songs, as reported by PPC Land (partial: 7 of the top 8)",
    entries=[dict(rank=1, title="Happy Diwali", artist="Vishal-Shekhar, Surthi, Aparna Sen, Sunidhi Chauhan"),
             dict(rank=2, title="PASSO BEM SOLTO (Slowed)", artist="ATLXS"),
             dict(rank=3, title="Payal Ki Khanak", artist="Anil Rawat, Kajal Hathrasi, Mohit Love"),
             dict(rank=4, title="MONTAGEM ZORA (Slowed)", artist="WUYS, KIT"),
             dict(rank=6, title="Jalte Diye", artist=""),
             dict(rank=7, title="Guzarish", artist="Satinder Sartaaj"),
             dict(rank=8, title="NONA KAKA SAYANG", artist="IKI DANO")])


def load_chart():
    c = load_settings().get("shorts_chart")
    return c if isinstance(c, dict) and c.get("entries") else STARTER_SOUNDS


def _find_col(head, names, avoid=()):
    for n in names:
        if n in head: return head.index(n)
    for n in names:
        for i, h in enumerate(head):
            if n in h and not any(a in h for a in avoid): return i
    return None


def parse_chart_csv(path):
    """Read a chart file downloaded from charts.youtube.com (columns are found by name, so layout changes are tolerated)."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096); f.seek(0)
        try: dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error: dialect = csv.excel
        rows = list(csv.reader(f, dialect))
    if len(rows) < 2: raise ValueError("That file has no songs in it.")
    head = [h.strip().lower() for h in rows[0]]
    ti = _find_col(head, ("track name", "title", "song title", "song", "track", "name"), avoid=("artist", "previous", "period"))
    if ti is None: raise ValueError("Couldn't find a song-title column. Columns in the file: " + ", ".join(rows[0]))
    ai = _find_col(head, ("artist names", "artists", "artist"), avoid=("previous",))
    ri = _find_col(head, ("rank", "position"), avoid=("previous", "prev", "last"))
    ui = _find_col(head, ("youtube url", "url", "link"))
    entries = []
    for n, r in enumerate(rows[1:], 1):
        get = lambda i: " ".join(r[i].split()) if i is not None and i < len(r) else ""   # also fixes stray spaces in the files
        if not get(ti): continue
        try: rank = int(float(get(ri))) if ri is not None else n
        except ValueError: rank = n
        e = dict(rank=rank, title=get(ti), artist=get(ai))
        if get(ui).lower().startswith("http"): e["url"] = get(ui)
        entries.append(e)
    if not entries: raise ValueError("No songs found in that file.")
    entries.sort(key=lambda e: e["rank"])
    return dict(week=os.path.basename(path), source="your imported YouTube Charts file", entries=entries)


def youtube_link(entry):
    return entry.get("url") or "https://www.youtube.com/results?search_query=" + urllib.parse.quote_plus(f"{entry['title']} {entry.get('artist', '')}".strip())


def _tokens(s):
    return set(re.findall(r"[a-z0-9]+", re.sub(r"\(.*?\)|\[.*?\]", " ", s.lower())))


def pick_preview(results, title, artist):
    """Choose the best Apple result for a song, or None. Needs most of the title words to match (so we never preview the wrong song)."""
    tt, at = _tokens(title), _tokens(artist)
    best, best_score = None, 0.0
    for r in results:
        if not r.get("previewUrl"): continue
        rt, ra = _tokens(r.get("trackName", "")), _tokens(r.get("artistName", ""))
        t_ov = len(tt & rt) / max(1, len(tt))
        if t_ov < 0.6: continue
        score = 2 * t_ov + (len(at & ra) / max(1, len(at)) if at else 0.5)
        if score > best_score: best, best_score = r, score
    return best


def itunes_preview(title, artist, country="US"):
    """Apple's own free 30-second preview clip for a song (iTunes Search API, no key needed). None if it isn't in their catalog."""
    base = re.sub(r"\(.*?\)", "", title).strip()
    for term in dict.fromkeys(t for t in (f"{title} {artist}".strip(), f"{base} {artist.split(',')[0].strip()}".strip()) if t):
        r = requests.get("https://itunes.apple.com/search", params=dict(term=term, media="music", entity="song", limit=8, country=country), timeout=10)
        r.raise_for_status()
        hit = pick_preview(r.json().get("results", []), title, artist)
        if hit: return dict(url=hit["previewUrl"], name=hit.get("trackName", ""), artist=hit.get("artistName", ""))
    return None


APPLE_GENRES = {"All genres": 0, "Pop": 14, "Hip-Hop/Rap": 18, "Dance": 17, "Electronic": 7, "R&B/Soul": 15, "Rock": 21, "Latin": 12, "Country": 6, "Alternative": 20, "K-Pop": 51}
APPLE_COUNTRIES = {"United States": "us", "United Kingdom": "gb", "Canada": "ca", "Australia": "au", "Germany": "de", "France": "fr", "Brazil": "br", "Mexico": "mx", "Japan": "jp", "Philippines": "ph"}
FREE_MOODS = ("Upbeat", "Hype", "Phonk", "Lo-fi", "Chill", "Epic", "Funny", "Gaming", "Trap", "Electronic", "Cinematic", "Happy")


def apple_chart(country="us", genre=0, limit=30):
    """Apple's free top-songs chart for a country (and optionally a genre). Each entry carries Apple's 30-second preview link."""
    g = f"/genre={genre}" if genre else ""
    r = requests.get(f"https://itunes.apple.com/{country}/rss/topsongs/limit={limit}{g}/json", timeout=15); r.raise_for_status()
    out = []
    for n, e in enumerate((r.json().get("feed") or {}).get("entry") or [], 1):
        links = e.get("link") or []; links = links if isinstance(links, list) else [links]
        prev = next((l["attributes"]["href"] for l in links if (l.get("attributes") or {}).get("type", "").startswith("audio")), "")
        out.append(dict(rank=n, title=(e.get("im:name") or {}).get("label", ""), artist=(e.get("im:artist") or {}).get("label", ""), audio_url=prev, source="Apple chart"))
    return [e for e in out if e["title"]]


def apple_search(term, country="us", limit=25):
    """Search any song in Apple's catalog (30-second previews)."""
    r = requests.get("https://itunes.apple.com/search", params=dict(term=term, media="music", entity="song", limit=limit, country=country), timeout=15); r.raise_for_status()
    return [dict(rank=n, title=x.get("trackName", ""), artist=x.get("artistName", ""), audio_url=x.get("previewUrl", ""), source="Apple search")
            for n, x in enumerate(r.json().get("results", []), 1) if x.get("previewUrl")]


def free_music_search(term, limit=25):
    """Full-length tracks with a free licence that allows commercial use (Openverse, no key needed) - far less likely to get a post muted or claimed."""
    r = requests.get("https://api.openverse.org/v1/audio/", params={"q": term, "page_size": limit, "license_type": "commercial", "category": "music"}, timeout=20,
                     headers={"User-Agent": "ClipStudio/1.0"}); r.raise_for_status()
    out = []
    for n, x in enumerate(r.json().get("results", []), 1):
        url = x.get("url") or ""
        if not url: continue
        lic = str(x.get("license", "")).upper(); lic = (lic if lic in ("CC0", "PDM") else f"CC {lic} {x.get('license_version') or ''}").strip()
        need = str(x.get("license", "")).lower() not in ("cc0", "pdm")
        out.append(dict(rank=n, title=x.get("title") or "Untitled", artist=x.get("creator") or "", audio_url=url, license=lic, source="Free music",
                        seconds=(x.get("duration") or 0) / 1000.0, credit=(f"Music: {x.get('title') or 'Untitled'} by {x.get('creator') or 'unknown'} ({lic})" if need else ""),
                        page=x.get("foreign_landing_url") or ""))
    return out


_PREVIEW_CACHE = {}


def fetch_preview_audio(entry):
    """Download (once) the 30-second sample for a chart entry. Returns (file, info) or None."""
    key = (entry["title"], entry.get("artist", ""), entry.get("audio_url", ""))
    if key in _PREVIEW_CACHE and os.path.exists(_PREVIEW_CACHE[key][0]): return _PREVIEW_CACHE[key]
    if entry.get("audio_url"): hit = dict(url=entry["audio_url"], name=entry["title"], artist=entry.get("artist", ""))
    else: hit = itunes_preview(entry["title"], entry.get("artist", ""))
    if not hit: return None
    r = requests.get(hit["url"], timeout=60, headers={"User-Agent": "ClipStudio/1.0"}); r.raise_for_status()
    ext = os.path.splitext(urllib.parse.urlparse(hit["url"]).path)[1].lower()
    if ext not in (".mp3", ".m4a", ".wav", ".ogg", ".flac", ".aac", ".opus"): ext = ".m4a"
    path = os.path.join(tempfile.gettempdir(), f"clipstudio_sound_{abs(hash(key))}{ext}")
    open(path, "wb").write(r.content)
    _PREVIEW_CACHE[key] = (path, hit)
    return _PREVIEW_CACHE[key]


class AudioPlayer:
    """Plays a short audio file through whatever this computer has (Windows: built in; Mac: afplay; Linux: ffplay/paplay/aplay)."""
    def __init__(self): self.proc = None

    def play_file(self, path):
        self.stop()
        wav = os.path.join(tempfile.gettempdir(), f"clipstudio_play_{os.getpid()}.wav")
        sh("ffmpeg", "-y", "-i", path, "-ac", "2", "-ar", "44100", wav)
        self.play_wav(wav)

    def play_wav(self, wav):
        self.stop()
        if os.name == "nt":
            import winsound
            winsound.PlaySound(wav, winsound.SND_FILENAME | winsound.SND_ASYNC)
        elif sys.platform == "darwin":
            self.proc = subprocess.Popen(["afplay", wav])
        else:
            for cmd in (["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", wav], ["paplay", wav], ["aplay", "-q", wav]):
                if shutil.which(cmd[0]):
                    self.proc = subprocess.Popen(cmd); return
            raise RuntimeError("no audio player found (install ffmpeg's ffplay, or pulseaudio/alsa utils)")

    def stop(self):
        if os.name == "nt":
            try:
                import winsound; winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception: pass
        if self.proc:
            try: self.proc.terminate()
            except Exception: pass
            self.proc = None


def write_sound_note(video_path, entry, chart, mixed=False):
    """Remember the sound you picked, next to the clip. The song is NOT mixed into the video (see the note text)."""
    path = os.path.splitext(video_path)[0] + ".sound.txt"
    open(path, "w", encoding="utf-8").write(
        f"Suggested sound: {entry['title']}" + (f" - {entry['artist']}" if entry.get("artist") else "") + "\n"
        + (f"Chart: YouTube Shorts top songs, {chart.get('week', '')}\n" if entry.get("source") is None else f"Source: {entry['source']}\n")
        + (f"CREDIT (required by the licence - put this in your post description): {entry['credit']}\n" if entry.get("credit") else "")
        + (f"License: {entry['license']}  {entry.get('page', '')}\n" if entry.get("license") else "")
        + f"Listen: {youtube_link(entry)}\n\n"
        + ("This song's 30-second sample WAS mixed into the video file. Commercial music can get a post muted, blocked or claimed.\n"
           "If that happens, re-post using the sound from the platform's own library instead.\n" if mixed else
           "Add this sound inside the YouTube Shorts / TikTok / Instagram app when you post (their sound libraries are licensed for that).\n"
           "It is NOT mixed into the video file.\n"))
    return path


class ClipAudio:
    """Sound for the edited preview. The video frames come from FramePump (silent); this plays the clip's audio and is restarted at
    the first frame of every loop, so picture and sound stay together. muted=True means it never plays."""
    def __init__(self, muted=True):
        self.player, self.muted, self.wav, self.src, self.n, self.part = AudioPlayer(), muted, None, None, 0, None
        self.tag = f"{os.getpid()}_{id(self)}"

    def prepare(self, path, voice=None, log=print, music=None, clip=None):
        """Make the preview wav; with voice focus / music on, it has the same processing the finished clip will get."""
        key = (path, json.dumps(voice, sort_keys=True, default=str) if voice_active(voice) else None,
               json.dumps(music, sort_keys=True, default=str) if music_active(music) else None,
               json.dumps(clip, sort_keys=True, default=str) if clip_active(clip) else None)
        if key == self.src and self.wav and os.path.exists(self.wav): return True
        self.n += 1; wav = os.path.join(tempfile.gettempdir(), f"clipstudio_prev_{self.tag}_{self.n}.wav")   # new name each time: the old one may be playing
        try:
            if audio_active(voice, music, clip): build_audio(path, 0, duration(path), voice, music, wav, log, clip)
            else: sh("ffmpeg", "-y", "-i", path, "-vn", "-ac", "2", "-ar", "44100", wav)
        except Exception as e:
            log(f"Preview sound failed: {str(e)[:120]}"); return False
        old, self.wav, self.src = self.wav, wav, key
        if old: safe_remove(old, tries=1)
        return True

    def start(self):
        self.play_from(0.0)

    def play_from(self, pos):
        """Play the preview sound starting `pos` seconds in (used after pause / seek / loop so the sound lines up with the picture)."""
        if self.muted or not self.wav: return
        try:
            if pos < 0.1: return self.player.play_wav(self.wav)
            self.n += 1; part = os.path.join(tempfile.gettempdir(), f"clipstudio_prev_{self.tag}_s{self.n}.wav")
            sh("ffmpeg", "-y", "-v", "error", "-ss", f"{pos:.3f}", "-i", self.wav, "-c", "copy", part)
            self.player.play_wav(part)
            if self.part: safe_remove(self.part, tries=1)
            self.part = part
        except Exception: pass

    def stop(self):
        try: self.player.stop()
        except Exception: pass

    def destroy(self):
        self.stop()
        if self.wav: safe_remove(self.wav, tries=2); self.wav = None
        if self.part: safe_remove(self.part, tries=1); self.part = None


class VideoPane:
    """A canvas that shows frames from a FramePump (silent, real-time)."""
    def __init__(self, parent, w, h, label, clock=None):
        self.frame = tk.Frame(parent, bg=BG)
        tk.Label(self.frame, text=label, bg=BG, fg="#adadb8", font=("Segoe UI", 9, "bold")).pack()
        self.canvas = tk.Canvas(self.frame, width=w, height=h, bg="black", highlightthickness=0); self.canvas.pack()
        self.pump = FramePump(w, h, clock=clock); self.img = None; self.item = None; self.seen = 0; self.alive = True; self.note = None
        self.audio = None
        self._tick()

    def resize(self, w, h):
        if (w, h) != (self.pump.w, self.pump.h):
            self.pump.w, self.pump.h = w, h; self.canvas.config(width=w, height=h)
            self.canvas.delete("all"); self.item = None; self.note = None; self.seen = -1; self.img = None      # an old-size picture must never linger

    def play(self, path, vf, pre=()):
        if self.note is not None:
            try: self.canvas.delete("err")
            except Exception: pass
            self.note = None
        self.pump.play(path, vf, pre=pre)
        try:
            self.canvas.delete("ld"); self.canvas.create_text(self.pump.w // 2, self.pump.h // 2, text="loading preview...", fill="#adadb8", tags="ld")
        except Exception: pass

    def attach_audio(self, audio):
        self.audio = audio                 # (the review window starts/stops it from the shared clock)

    def stop(self):
        self.pump.stop()
        if self.audio: self.audio.stop()

    def destroy(self):
        self.alive = False; self.pump.stop()
        if self.audio: self.audio.destroy()

    def _tick(self):
        if not self.alive: return
        try:
            p = self.pump
            if p.failed and self.note is None:
                self.note = self.canvas.create_text(10, 10, anchor="nw", fill="#ff6b6b", width=max(100, p.w - 20), tags="err",
                                                    text="Preview failed:\n" + p.err[-200:])
            elif p.frame is not None and p.count != self.seen and p.fsize == (p.w, p.h) and len(p.frame) == p.w * p.h * 3:
                self.seen = p.count
                self.img = tk.PhotoImage(width=p.w, height=p.h, format="PPM", data=b"P6 %d %d 255\n" % (p.w, p.h) + p.frame)
                if self.item is None: self.item = self.canvas.create_image(0, 0, image=self.img, anchor="nw")
                else: self.canvas.itemconfig(self.item, image=self.img)
                self.canvas.delete("ld"); self.canvas.tag_lower(self.item)
        except Exception as e:                       # e.g. a Tk build that can't show PPM frames
            if self.note is None:
                self.note = self.canvas.create_text(10, 10, anchor="nw", fill="#ff6b6b", width=max(100, self.pump.w - 20), tags="err",
                                                    text=f"Can't draw the preview here ({e}).\nUse 'Open in player' instead.")
        finally:
            self.canvas.after(40, self._tick)


class StudioDrop:
    """The 'drop zone' window: drag video files in (or Browse) and they open in the editing studio."""
    def __init__(self, app):
        self.app = app; self.top = tk.Toplevel(app.root); self.top.title("Editing studio"); self.top.configure(bg=BG)
        w, h = 560, 320
        try: self.top.geometry(f"{w}x{h}+{max(0, app.root.winfo_rootx() + 140)}+{max(0, app.root.winfo_rooty() + 120)}")
        except Exception: pass
        zone = tk.Frame(self.top, bg=PANEL, highlightthickness=2, highlightbackground=PURPLE); zone.pack(fill="both", expand=True, padx=18, pady=(18, 8))
        tk.Label(zone, text="🎬", bg=PANEL, fg=FG, font=("Segoe UI", 34)).pack(pady=(28, 0))
        self.ok = enable_drop(self.top, self.got) | enable_drop(zone, self.got)
        tk.Label(zone, text="Drag video files here" if self.ok else "Click Browse to pick your video files", bg=PANEL, fg=FG, font=("Segoe UI", 15, "bold")).pack()
        tk.Label(zone, text="The first one opens in the editor, the others are added after it.\nYour original files are never changed.", bg=PANEL, fg="#adadb8", justify="center").pack(pady=6)
        if not self.ok: tk.Label(zone, text="(To enable dragging, run:  python -m pip install tkinterdnd2   and restart the app)", bg=PANEL, fg=YELLOW, wraplength=480).pack()
        row = tk.Frame(self.top, bg=BG); row.pack(pady=(0, 14))
        tk.Button(row, text="Browse...", bg=PURPLE, fg="white", relief="flat", padx=16, command=self.browse).pack(side="left", padx=6)
        tk.Button(row, text="Close", bg=PANEL, fg=FG, relief="flat", padx=16, command=self.close).pack(side="left", padx=6)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

    def browse(self):
        ps = filedialog.askopenfilenames(parent=self.top, title="Pick video files for the editor", initialdir=os.path.abspath(CLIPS_DIR),
                                         filetypes=[("Video", " ".join("*" + e for e in VIDEO_EXT)), ("All files", "*.*")])
        self.got(list(ps))

    def got(self, paths):
        if paths: self.app.studio_open(paths)

    def close(self):
        self.app.studio_win = None
        try: self.top.destroy()
        except Exception: pass


class JoinDrop:
    """The 'Drop a clip here' pop-up that opens when you press Join clip: drop video files in (or Browse) and they're joined onto the end."""
    def __init__(self, review):
        self.r = review; self.top = tk.Toplevel(review.top); self.top.title("Join a clip"); self.top.configure(bg=BG)
        try:
            self.top.geometry(f"480x290+{max(0, review.top.winfo_rootx() + 220)}+{max(0, review.top.winfo_rooty() + 160)}"); self.top.transient(review.top)
        except Exception: pass
        zone = tk.Frame(self.top, bg=PANEL, highlightthickness=2, highlightbackground=PURPLE); zone.pack(fill="both", expand=True, padx=16, pady=(16, 8))
        tk.Label(zone, text="＋", bg=PANEL, fg=PURPLE, font=("Segoe UI", 34, "bold")).pack(pady=(22, 0))
        self.ok = enable_drop(self.top, self.got) | enable_drop(zone, self.got)
        tk.Label(zone, text="Drop a clip here" if self.ok else "Click Browse to pick a clip", bg=PANEL, fg=FG, font=("Segoe UI", 15, "bold")).pack()
        tk.Label(zone, text="It's joined onto the end of this clip.  Ctrl+Z takes it back off.", bg=PANEL, fg="#adadb8").pack(pady=6)
        if not self.ok: tk.Label(zone, text="(To enable dragging: python -m pip install tkinterdnd2, then restart)", bg=PANEL, fg=YELLOW, wraplength=420).pack()
        row = tk.Frame(self.top, bg=BG); row.pack(pady=(0, 14))
        tk.Button(row, text="Browse...", bg=PURPLE, fg="white", relief="flat", padx=16, command=self.browse).pack(side="left", padx=6)
        tk.Button(row, text="Cancel", bg=PANEL, fg=FG, relief="flat", padx=16, command=self.close).pack(side="left", padx=6)
        self.top.protocol("WM_DELETE_WINDOW", self.close)

    def browse(self):
        ps = filedialog.askopenfilenames(parent=self.top, title="Pick the clip(s) to join onto the end", initialdir=os.path.abspath(CLIPS_DIR),
                                         filetypes=[("Video", " ".join("*" + e for e in VIDEO_EXT)), ("All files", "*.*")])
        self.got(list(ps))

    def got(self, paths):
        if not paths: return
        self.close(); self.r.add_paths(paths)

    def close(self):
        self.r.join_win = None
        try: self.top.destroy()
        except Exception: pass


class Transport:
    """Pause/play button + seek bar + time, for a PlayClock. Every preview on that clock pauses and jumps together."""
    def __init__(self, parent, clock, length=380, on_change=None):
        self.clock, self.drag, self.on_change = clock, False, on_change
        self.frame = tk.Frame(parent, bg=BG)
        self.btn = tk.Button(self.frame, text="⏸ Pause", width=10, bg=PURPLE, fg="white", relief="flat", command=self.toggle); self.btn.pack(side="left")
        self.var = tk.DoubleVar(value=0.0)
        self.scale = tk.Scale(self.frame, from_=0, to=max(1.0, clock.dur), resolution=0.05, orient="horizontal", variable=self.var, showvalue=False,
                              length=length, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE)
        self.scale.pack(side="left", padx=8)
        self.lbl = tk.Label(self.frame, text="0:00.0", bg=BG, fg=FG, font=("Consolas", 10)); self.lbl.pack(side="left")
        self.scale.bind("<ButtonPress-1>", self.press); self.scale.bind("<ButtonRelease-1>", self.release)
        self.tick()

    def toggle(self):
        self.clock.toggle(); self.refresh()

    def press(self, e=None): self.drag = True
    def release(self, e=None):
        self.drag = False; self.clock.seek(self.var.get())          # keeps paused/playing as it was
        if self.on_change: self.on_change()

    def refresh(self):
        self.btn.config(text="▶ Play" if self.clock.paused else "⏸ Pause")

    @staticmethod
    def fmt(t): return f"{int(t // 60)}:{t % 60:04.1f}"

    def tick(self):
        try:
            c = self.clock; pos = c.pos(); d = max(1.0, c.dur)
            self.scale.config(to=d)
            if not self.drag: self.var.set(round(pos, 2))
            self.lbl.config(text=f"{self.fmt(self.var.get() if self.drag else pos)} / {self.fmt(c.dur)}")
            self.refresh(); self.frame.after(100, self.tick)
        except Exception:
            pass                                                     # window closed


SAMPLE_TEXT = "No way, he actually did it!"


class CaptionPreview:
    """Live preview window: shows your caption style animating word by word, and re-draws a split second after any change."""
    BACKGROUNDS = ("Dark", "Light", "Busy colors", "A video file...")

    def __init__(self, app, editor):
        self.app, self.editor, self._after, self.video = app, editor, None, None
        self.top = tk.Toplevel(app.root); self.top.title("Caption preview (live)"); self.top.configure(bg=BG)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=10, pady=(10, 4))
        self.bg = tk.StringVar(value="Dark"); self.orient = tk.StringVar(value="Vertical 9:16"); self.sample = tk.StringVar(value=SAMPLE_TEXT)
        choices = self.BACKGROUNDS + (("This clip (its real words)",) if editor.review else ())
        tk.Label(bar, text="Background", bg=BG, fg=FG).pack(side="left"); purple_menu(bar, self.bg, *choices).pack(side="left", padx=(4, 10))
        tk.Label(bar, text="Shape", bg=BG, fg=FG).pack(side="left"); purple_menu(bar, self.orient, "Vertical 9:16", "Landscape 16:9").pack(side="left", padx=4)
        row = tk.Frame(self.top, bg=BG); row.pack(fill="x", padx=10)
        tk.Label(row, text="Sample text", bg=BG, fg=FG).pack(side="left")
        tk.Entry(row, width=34, textvariable=self.sample, bg=PANEL, fg=FG, insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        self.pane = VideoPane(self.top, 270, 480, "LIVE PREVIEW  -  this is exactly how the burned-in captions will look"); self.pane.frame.pack(padx=10, pady=(8, 2))
        self.pbtn = tk.Button(self.top, text="⏸ Pause / ▶ Play", bg=PURPLE, fg="white", relief="flat", command=self.pane.pump.clock.toggle); self.pbtn.pack(pady=(0, 6))
        self.note = tk.Label(self.top, text="", bg=BG, fg="#adadb8", wraplength=420, justify="left"); self.note.pack(padx=10, pady=(0, 8))
        for v in (self.bg, self.orient, self.sample): v.trace_add("write", lambda *a: self.schedule())
        self.bg.trace_add("write", lambda *a: self.pick_video_if_needed())
        self.refresh()

    def pick_video_if_needed(self):
        if self.bg.get() == "A video file..." and not self.video:
            p = filedialog.askopenfilename(title="Pick a video to preview captions on", filetypes=[("Video", "*.mp4 *.mkv *.mov *.ts *.webm"), ("All", "*.*")])
            if p: self.video = p
            else: self.bg.set("Dark")

    def schedule(self):
        if self._after: self.top.after_cancel(self._after)
        self._after = self.top.after(250, self.refresh)

    def refresh(self):
        S = self.editor.style; vertical = self.orient.get().startswith("Vertical")
        W, H = (270, 480) if vertical else (480, 270); bg = self.bg.get()
        text = self.sample.get().strip() or SAMPLE_TEXT
        words = [{"w": w, "s": 0.3 + i * 0.42, "e": 0.3 + i * 0.42 + 0.38} for i, w in enumerate(text.split())]
        total = max(2.5, 0.3 + len(words) * 0.42 + 1.0)
        src, pre = None, ()
        if bg.startswith("This clip") and self.editor.review:
            rv = self.editor.review; src = rv.item["raw"]
            if rv.words: words, total = rv.words, rv.item["dur"]
        elif bg == "A video file..." and self.video: src = self.video
        ass = os.path.join(tempfile.gettempdir(), f"clipstudio_capprev_{os.getpid()}_{id(self)}.ass")
        make_ass(words, 0, total, ass, vertical, "", "", S)
        self.pane.resize(W, H)
        if src:
            self.pane.play(src, build_vf(vertical, "blur", ass, (W, H)))
        else:
            colour = {"Dark": f"color=c=0x15151f:s={W}x{H}:r=20:d={total:.2f}", "Light": f"color=c=0xf2f2f2:s={W}x{H}:r=20:d={total:.2f}"}.get(
                bg, f"testsrc2=s={W}x{H}:r=20:d={total:.2f}")
            self.pane.play(colour, build_vf(False, "blur", ass, None), pre=("-f", "lavfi"))
        self.note.config(text=f"Font: {S['font']}   |   {S['size']} px   |   {S['words']} word(s) per line   |   highlight: {S['highlight_mode']}")

    def close(self):
        self.destroy(); self.editor.preview = None

    def destroy(self):
        self.pane.destroy(); safe_remove(os.path.join(tempfile.gettempdir(), f"clipstudio_capprev_{os.getpid()}_{id(self)}.ass"))
        try: self.top.destroy()
        except Exception: pass


class SoundsWindow:
    """Find music: the Shorts top 15, Apple's popular charts (any country / genre), a search box, and free-to-use music that is safe to post."""
    SRC = ("Shorts top 15", "Popular songs", "Search", "Free music (safe to post)")

    def __init__(self, app, on_pick=None):
        self.app, self.on_pick, self.player = app, on_pick, AudioPlayer()
        self.chart = load_chart(); self.hide = load_settings().get("hide_indian", True); self.src = tk.StringVar(value=self.SRC[0]); self._rows = []
        self.top = tk.Toplevel(app.root); self.top.title("Find music"); self.top.configure(bg=BG)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(self.top, text="FIND MUSIC", bg=BG, fg=FG, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        tabs = tk.Frame(self.top, bg=BG); tabs.pack(fill="x", padx=12); self.tab_btns = {}
        for name in self.SRC:
            b = tk.Button(tabs, text=name, relief="flat", padx=12, pady=3, command=lambda n=name: self.pick_source(n)); b.pack(side="left", padx=(0, 4)); self.tab_btns[name] = b
        self.ctl = tk.Frame(self.top, bg=BG); self.ctl.pack(fill="x", padx=12, pady=6)
        self.box = tk.Frame(self.top, bg=PANEL); self.box.pack(fill="x", padx=12, pady=4)
        self.status = tk.Label(self.top, text="", bg=BG, fg=FG, wraplength=700, justify="left"); self.status.pack(anchor="w", padx=12, pady=(6, 0))
        self.note = tk.Label(self.top, bg=BG, fg="#adadb8", wraplength=700, justify="left"); self.note.pack(anchor="w", padx=12, pady=(2, 12))
        self.country = tk.StringVar(value="United States"); self.genre = tk.StringVar(value="All genres"); self.q = tk.StringVar(); self.mood = tk.StringVar(value="Upbeat")
        self.pick_source(self.SRC[0])

    # ----- layout per source -----
    def clear(self, frame):
        for c in list(frame.winfo_children()):
            try: c.destroy()
            except Exception: pass

    def pick_source(self, name):
        self.src.set(name); self.player.stop(); self.clear(self.ctl)
        for n, b in self.tab_btns.items(): b.config(bg=PURPLE if n == name else PANEL, fg="white" if n == name else FG)
        c = self.ctl
        if name == self.SRC[0]:
            tk.Button(c, text="Open YouTube Charts", bg=PANEL, fg=FG, relief="flat", command=lambda: webbrowser.open("https://charts.youtube.com")).pack(side="left")
            tk.Button(c, text="Import chart file (.csv)...", bg=PANEL, fg=FG, relief="flat", command=self.import_csv).pack(side="left", padx=8)
            self.hide_var = tk.BooleanVar(value=self.hide)
            tk.Checkbutton(c, text="Hide Indian music", variable=self.hide_var, bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG, command=self.toggle_hide).pack(side="left")
            all_e = self.chart["entries"][:15]; ent = filter_entries(all_e, self.hide)
            self.fill(ent, yt=True)
            self.note.config(text=f"{self.chart['week']} - {self.chart['source']}. Samples are Apple's 30-second previews (slowed/remixed versions may differ; use ↗ YouTube for the exact sound). "
                                  "Commercial music can get a post muted or claimed - the 'Free music' tab is the safe choice.")
        elif name == self.SRC[1]:
            purple_menu(c, self.country, *APPLE_COUNTRIES).pack(side="left"); purple_menu(c, self.genre, *APPLE_GENRES).pack(side="left", padx=6)
            tk.Button(c, text="Show chart", bg=PURPLE, fg="white", relief="flat", command=self.load_apple).pack(side="left", padx=6)
            self.note.config(text="What people are listening to right now, from Apple's free charts. 30-second previews. Popular songs are copyrighted - fine for trying ideas, riskier for monetized posts.")
            self.load_apple()
        elif name == self.SRC[2]:
            e = tk.Entry(c, width=34, textvariable=self.q, bg=PANEL, fg=FG, insertbackground=FG, relief="flat"); e.pack(side="left", ipady=4)
            try: e.bind("<Return>", lambda ev: self.load_search())
            except Exception: pass
            tk.Button(c, text="Search", bg=PURPLE, fg="white", relief="flat", command=self.load_search).pack(side="left", padx=6)
            self.fill([]); self.note.config(text="Type a song or artist. You get Apple's 30-second previews.")
        else:
            for m in FREE_MOODS[:6]: tk.Button(c, text=m, bg=PANEL, fg=FG, relief="flat", command=lambda m=m: self.load_free(m)).pack(side="left", padx=2)
            e = tk.Entry(c, width=16, textvariable=self.q, bg=PANEL, fg=FG, insertbackground=FG, relief="flat"); e.pack(side="left", ipady=4, padx=(10, 2))
            try: e.bind("<Return>", lambda ev: self.load_free(self.q.get()))
            except Exception: pass
            tk.Button(c, text="Search", bg=PURPLE, fg="white", relief="flat", command=lambda: self.load_free(self.q.get())).pack(side="left", padx=4)
            self.note.config(text="Full-length songs with free licences that allow commercial use, so they're much less likely to be muted or claimed. Some need a credit - "
                                  "we add it for you in the clip's .sound.txt note.")
            self.load_free("Upbeat")

    def fill(self, entries, yt=False):
        self.clear(self.box); self.entries = entries
        if not entries:
            tk.Label(self.box, text="Nothing to show yet.", bg=PANEL, fg="#adadb8").pack(padx=10, pady=10); return
        for e in entries:
            row = tk.Frame(self.box, bg=PANEL); row.pack(fill="x", padx=8, pady=2)
            tk.Label(row, text=f"#{e['rank']}", width=4, bg=PANEL, fg=PURPLE, font=("Segoe UI", 10, "bold")).pack(side="left")
            extra = (f"   ({e['license']}, {e['seconds']:.0f}s)" if e.get("license") else "")
            tk.Label(row, text=e["title"] + (f"  -  {e['artist']}" if e.get("artist") else "") + extra, anchor="w", width=60, bg=PANEL, fg=FG).pack(side="left")
            tk.Button(row, text="▶ Preview", bg=BG, fg=FG, relief="flat", command=lambda e=e: self.preview(e)).pack(side="left", padx=2)
            if yt: tk.Button(row, text="↗ YouTube", bg=BG, fg=FG, relief="flat", command=lambda e=e: webbrowser.open(youtube_link(e))).pack(side="left", padx=2)
            if self.on_pick: tk.Button(row, text="✔ Add to clip", bg=PURPLE, fg="white", relief="flat", command=lambda e=e: self.pick(e)).pack(side="left", padx=2)

    # ----- loading (in the background so the window never freezes) -----
    def bg(self, label, fn, then):
        self.say(label); name = self.src.get()
        def work():
            try: res, err = fn(), ""
            except Exception as ex: res, err = None, str(ex)[:120]
            def fin():
                if self.src.get() != name: return
                if res is None: return self.say(f"Couldn't load that ({err}). Check your internet and try again.")
                then(res); self.say("")
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def load_apple(self):
        self.bg("Loading the chart...", lambda: apple_chart(APPLE_COUNTRIES[self.country.get()], APPLE_GENRES[self.genre.get()]),
                lambda r: (self.fill(r), None if r else self.say("That chart came back empty - try another country or genre.")))

    def load_search(self):
        t = self.q.get().strip()
        if t: self.bg(f"Searching for {t}...", lambda: apple_search(t), lambda r: (self.fill(r), None if r else self.say("No songs found.")))

    def load_free(self, term):
        term = (term or "").strip() or "upbeat"; self.q.set(term if term not in FREE_MOODS else "")
        self.bg(f"Finding free music: {term}...", lambda: free_music_search(term), lambda r: (self.fill(r), None if r else self.say("Nothing found - try another word.")))

    def say(self, t): self.status.config(text=t)

    def toggle_hide(self):
        save_settings(hide_indian=bool(self.hide_var.get())); on_pick = self.on_pick; self.close(); self.app.open_sounds(on_pick)

    def preview(self, e):
        self.say(f"Getting {e['title']}...")
        def work():
            try: got, err = fetch_preview_audio(e), ""
            except Exception as ex: got, err = None, str(ex)[:100]
            def fin():
                if not got: return self.say(f"No sample found for this one ({err or 'not in Apple’s catalog'}).")
                path, hit = got
                try:
                    self.player.play_file(path)
                    self.say(f"▶ Playing: {hit['name']} - {hit['artist']}")
                except Exception as ex: self.say(f"Couldn't play audio on this computer: {str(ex)[:120]}")
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def pick(self, e):
        self.on_pick(e); self.say(f"✔ Chosen: {e['title']}." + (f"  Credit to give: {e['credit']}" if e.get("credit") else ""))

    def import_csv(self):
        p = filedialog.askopenfilename(title="Pick the chart file you downloaded from YouTube Charts", filetypes=[("Chart file", "*.csv *.tsv *.txt"), ("All", "*.*")])
        if not p: return
        try: chart = parse_chart_csv(p)
        except Exception as ex: return self.say(f"Couldn't read that file: {ex}")
        save_settings(shorts_chart=chart); on_pick = self.on_pick; self.close(); self.app.open_sounds(on_pick)

    def close(self):
        self.player.stop(); self.top.destroy(); self.app.sounds_win = None


class CaptionEditor:
    """Customize how captions LOOK and fix what Whisper HEARD. In a review window the live preview updates as you change things."""
    def __init__(self, app, style, on_change=None, review=None):
        self.app, self.style, self.on_change, self.review = app, style, on_change, review
        self._muted, self._text_after, self.preview, self.fams = True, None, None, []
        self.top = tk.Toplevel(app.root)
        self.top.title("Customize captions - this clip" if review else "Caption style - your default look")
        self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        S = resolve_style(style); lab = lambda d, v: next((k for k, x in d.items() if x == v), next(iter(d)))
        self.v = dict(font=tk.StringVar(value=S["font"]), size=tk.DoubleVar(value=S["size"]), words=tk.DoubleVar(value=S["words"]),
                      outline=tk.DoubleVar(value=S["outline"]), shadow=tk.DoubleVar(value=S["shadow"]), y_offset=tk.DoubleVar(value=S["y_offset"]),
                      highlight_mode=tk.StringVar(value=lab(HIGHLIGHT_MODES, S["highlight_mode"])), position=tk.StringVar(value=lab(POSITIONS, S["position"])),
                      upper=tk.BooleanVar(value=S["upper"]), bold=tk.BooleanVar(value=S["bold"]),
                      strip_punct=tk.BooleanVar(value=S["strip_punct"]), box=tk.BooleanVar(value=S["box"]), censor=tk.BooleanVar(value=S["censor"]))
        self.colors = {k: S[k] for k in ("color", "highlight", "outline_color")}
        pn = tk.Frame(self.top, bg=PANEL); pn.pack(fill="x", padx=12, pady=10)

        def row():
            f = tk.Frame(pn, bg=PANEL); f.pack(fill="x", padx=8, pady=3); return f
        def label(f, t): tk.Label(f, text=t, bg=PANEL, fg=FG).pack(side="left", padx=(0, 4))
        def scale(f, t, key, lo, hi, res):
            label(f, t); tk.Scale(f, from_=lo, to=hi, resolution=res, orient="horizontal", variable=self.v[key], length=150, bg=PANEL, fg=FG,
                                  troughcolor=BG, highlightthickness=0).pack(side="left", padx=(0, 12))
        r = row(); label(r, "Preset")
        self.preset = tk.StringVar(value="Choose...")
        purple_menu(r, self.preset, *CAPTION_PRESETS, command=self.apply_preset).pack(side="left", padx=(0, 12))
        self.build_gallery(pn)
        r = row(); scale(r, "Size", "size", 40, 220, 2); scale(r, "Words per line", "words", 1, 8, 1)
        r = row(); self.swatch = {}
        for text, key in (("Text color", "color"), ("Highlight color", "highlight"), ("Outline / box color", "outline_color")):
            b = tk.Button(r, text=text, bg=self.colors[key], fg="#000000", relief="flat", command=lambda k=key: self.pick(k)); b.pack(side="left", padx=(0, 8), ipadx=6)
            self.swatch[key] = b
        more = Collapsible(pn, "More looks (position, outline, shadow, options)", bg=PANEL, pack=dict(fill="x", padx=8, pady=(4, 4)))
        def row():
            f = tk.Frame(more.body, bg=PANEL); f.pack(fill="x", padx=0, pady=3); return f
        r = row(); label(r, "Highlight"); purple_menu(r, self.v["highlight_mode"], *HIGHLIGHT_MODES).pack(side="left", padx=(0, 12))
        label(r, "Position"); purple_menu(r, self.v["position"], *POSITIONS).pack(side="left", padx=(0, 12))
        scale(r, "Height (below mode)", "y_offset", 100, 1000, 10)
        r = row(); scale(r, "Outline", "outline", 0, 16, 1); scale(r, "Shadow", "shadow", 0, 8, 1)
        r = row()
        for text, key in (("UPPERCASE", "upper"), ("Bold", "bold"), ("Remove punctuation", "strip_punct"), ("Background box", "box"), ("Hide swear words (****)", "censor")):
            tk.Checkbutton(r, text=text, variable=self.v[key], bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG).pack(side="left", padx=(0, 10))

        wc = Collapsible(self.top, "Speech recognition (accuracy, language)", bg=BG, pack=dict(fill="x", padx=12, pady=(0, 8)))
        wp = tk.Frame(wc.body, bg=PANEL); wp.pack(fill="x")
        f = tk.Frame(wp, bg=PANEL); f.pack(fill="x", padx=8, pady=6)
        tk.Label(f, text="Speech recognition  -  model", bg=PANEL, fg=FG).pack(side="left", padx=(0, 4))
        self.model = tk.StringVar(value=WHISPER["model"]); purple_menu(f, self.model, "tiny", "base", "small", "medium", "large-v3", "distil-large-v3").pack(side="left")
        tk.Label(f, text="  language (en; blank = auto-guess)", bg=PANEL, fg=FG).pack(side="left")
        self.lang = tk.StringVar(value=WHISPER["language"] or "")
        tk.Entry(f, width=6, textvariable=self.lang, bg=BG, fg=FG, insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        tk.Label(f, text="bigger model = more accurate but slower; first use downloads it", bg=PANEL, fg="#adadb8").pack(side="left", padx=8)
        f2 = tk.Frame(wp, bg=PANEL); f2.pack(fill="x", padx=8, pady=(0, 6))
        self.clean = tk.BooleanVar(value=WHISPER.get("clean", True))
        tk.Checkbutton(f2, text="Clean up audio first", variable=self.clean, bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG).pack(side="left")
        tk.Label(f2, text="  Words to expect (names, game terms - helps spelling):", bg=PANEL, fg=FG).pack(side="left")
        self.vocab = tk.StringVar(value=WHISPER.get("vocab", ""))
        tk.Entry(f2, width=34, textvariable=self.vocab, bg=BG, fg=FG, insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        for var in (self.model, self.lang, self.clean, self.vocab): var.trace_add("write", lambda *a: self.whisper_changed())

        self.text = None
        if review:
            tk.Label(self.top, bg=BG, fg=FG, anchor="w", justify="left", wraplength=640,
                     text="TRANSCRIPT - fix any words Whisper got wrong. Timing is kept; words you add or remove are re-timed automatically."
                     ).pack(anchor="w", padx=12)
            self.text = tk.Text(self.top, height=6, width=76, bg=PANEL, fg=FG, insertbackground=FG, relief="flat", wrap="word")
            self.text.pack(fill="x", padx=12, pady=4); self.text.bind("<KeyRelease>", self.text_changed)
            tk.Button(self.top, text="↻ Re-transcribe with the settings above", bg=PANEL, fg=FG, relief="flat",
                      command=self.retranscribe).pack(anchor="w", padx=12, pady=(0, 6))
        else:
            tk.Label(self.top, bg=BG, fg="#adadb8", wraplength=640, justify="left",
                     text="Tip: open a clip (or 'Try a video file...') and press Customize captions in its review window to see changes live. "
                          "This window sets the default look used for every clip, and it is saved automatically."
                     ).pack(anchor="w", padx=12, pady=(0, 6))
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=(2, 12))
        tk.Button(bar, text="Reset to default look", bg=PANEL, fg=FG, relief="flat", command=lambda: self.apply_preset("Bold Yellow")).pack(side="left")
        if review: tk.Button(bar, text="Save as my default look", bg=PANEL, fg=FG, relief="flat", command=self.save_default).pack(side="left", padx=8)
        tk.Button(bar, text="Close", bg=PURPLE, fg="white", relief="flat", command=self.close).pack(side="right", ipadx=10)
        for var in self.v.values(): var.trace_add("write", lambda *a: self.changed())
        self._muted = False
        self.preview = CaptionPreview(app, self)              # live preview window opens alongside

    # ----- style <-> controls -----
    def changed(self):
        if self._muted: return
        v = self.v
        if POSITIONS[v["position"].get()] != self.style.get("position"): self.style["cap_x"] = self.style["cap_y"] = None       # choosing a Position undoes a hand placement
        self.style.update(font=v["font"].get(), size=int(v["size"].get()), words=int(v["words"].get()), outline=float(v["outline"].get()),
                          shadow=float(v["shadow"].get()), y_offset=int(v["y_offset"].get()), highlight_mode=HIGHLIGHT_MODES[v["highlight_mode"].get()],
                          position=POSITIONS[v["position"].get()], upper=bool(v["upper"].get()), bold=bool(v["bold"].get()),
                          strip_punct=bool(v["strip_punct"].get()), box=bool(v["box"].get()), censor=bool(v["censor"].get()), **self.colors)
        if not self.review: save_settings(caption_style=dict(self.style))      # default-look editor saves as you go
        self.highlight_font()
        if self.preview: self.preview.schedule()
        if self.on_change: self.on_change()

    def load(self, S):
        self._muted = True
        for k in ("font", "size", "words", "outline", "shadow", "y_offset", "upper", "bold", "strip_punct", "box", "censor"): self.v[k].set(S[k])
        self.v["highlight_mode"].set(next(k for k, x in HIGHLIGHT_MODES.items() if x == S["highlight_mode"]))
        self.v["position"].set(next(k for k, x in POSITIONS.items() if x == S["position"]))
        for k in self.colors: self.colors[k] = S[k]; self.swatch[k].config(bg=S[k])
        self._muted = False; self.highlight_font()

    def apply_preset(self, name):
        self.load(resolve_style(CAPTION_PRESETS[name])); self.changed()

    def pick(self, key):
        try: _, hexv = colorchooser.askcolor(color=self.colors[key], parent=self.top, title="Pick a color")
        except Exception: return
        if hexv: self.colors[key] = hexv; self.swatch[key].config(bg=hexv); self.changed()

    def save_default(self):
        self.app.caption_style.clear(); self.app.caption_style.update(self.style)
        save_settings(caption_style=dict(self.style)); self.app.log("Saved as your default caption look.")

    # ----- font gallery: every installed font drawn in its own typeface -----
    def build_gallery(self, parent):
        top = tk.Frame(parent, bg=PANEL); top.pack(fill="x", padx=8, pady=(6, 0))
        tk.Label(top, text="Font - click one (or use the Up/Down arrow keys). The live preview changes instantly.", bg=PANEL, fg=FG).pack(side="left")
        self.fsearch = tk.StringVar(value="")
        tk.Entry(top, width=16, textvariable=self.fsearch, bg=BG, fg=FG, insertbackground=FG, relief="flat").pack(side="right", ipady=2)
        tk.Label(top, text="search ", bg=PANEL, fg="#adadb8").pack(side="right")
        tk.Button(top, text="Import font...", bg=PURPLE, fg="white", relief="flat", command=self.import_font).pack(side="right", padx=8)
        box = tk.Frame(parent, bg=PANEL); box.pack(fill="x", padx=8, pady=(2, 4))
        self.gallery = tk.Text(box, height=7, width=70, bg=BG, fg=FG, relief="flat", cursor="hand2", wrap="none", exportselection=False)
        sb = purple_scrollbar(box, self.gallery.yview); self.gallery.config(yscrollcommand=sb.set)
        self.gallery.pack(side="left", fill="x", expand=True); sb.pack(side="right", fill="y")
        self.gallery.bind("<Button-1>", self.gallery_click)
        self.gallery.bind("<Up>", lambda e: self.gallery_step(-1)); self.gallery.bind("<Down>", lambda e: self.gallery_step(1))
        self.gallery.bind("<Key>", lambda e: "break" if e.keysym not in ("Up", "Down", "Prior", "Next") else None)
        self.fsearch.trace_add("write", lambda *a: self.fill_gallery())
        self.fill_gallery()

    def import_font(self):
        paths = filedialog.askopenfilenames(title="Pick font file(s) (.ttf, .otf)", filetypes=[("Font files", "*.ttf *.otf *.ttc"), ("All", "*.*")])
        added, errs = [], []
        for pth in paths:
            try: added.append(import_font_file(pth))
            except Exception as e: errs.append(f"{os.path.basename(pth)}: {e}")
        if added:
            self.fill_gallery(); self.v["font"].set(added[0])
            self.app.log(f"Imported font(s): {', '.join(added)}. They're saved in the '{FONTS_DIR}' folder and are used for the captions.")
        for e in errs: self.app.log(f"Font import failed - {e}")

    def fill_gallery(self):
        try: fams = sorted({f for f in tkfont.families(self.app.root) if not f.startswith("@")}, key=str.lower)
        except Exception: fams = list(FONTS)
        mine = sorted(imported_fonts(), key=str.lower)
        q = self.fsearch.get().strip().lower()
        self.fams = [f for f in mine + [x for x in fams if x not in mine] if q in f.lower()]
        g = self.gallery; g.config(state="normal"); g.delete("1.0", "end")
        for i, fam in enumerate(self.fams):
            g.insert("end", f"{fam}      The quick brown fox 123\n", f"f{i}")
            g.tag_configure(f"f{i}", font=(fam, 15))
        g.tag_configure("pick", background=PURPLE, foreground="white"); g.tag_raise("pick")
        g.config(state="disabled"); self.highlight_font()

    def highlight_font(self):
        if not self.fams: return
        cur = self.v["font"].get()
        g = self.gallery; g.tag_remove("pick", "1.0", "end")
        if cur in self.fams:
            n = self.fams.index(cur) + 1; g.tag_add("pick", f"{n}.0", f"{n}.end+1c"); g.see(f"{n}.0")

    def gallery_click(self, event):
        n = int(self.gallery.index(f"@{event.x},{event.y}").split(".")[0])
        if 1 <= n <= len(self.fams): self.v["font"].set(self.fams[n - 1])
        self.gallery.focus_set(); return "break"

    def gallery_step(self, d):
        if not self.fams: return "break"
        cur = self.v["font"].get(); i = self.fams.index(cur) if cur in self.fams else -1
        self.v["font"].set(self.fams[max(0, min(len(self.fams) - 1, i + d))]); return "break"

    # ----- speech recognition + transcript -----
    def whisper_changed(self):
        WHISPER.update(model=self.model.get(), language=(self.lang.get().strip() or None), clean=bool(self.clean.get()), vocab=self.vocab.get().strip())
        save_settings(whisper_model=WHISPER["model"], whisper_language=WHISPER["language"] or "", whisper_clean=WHISPER["clean"], whisper_vocab=WHISPER["vocab"])

    def set_transcript(self, words):
        if self.text is not None:
            self.text.delete("1.0", "end"); self.text.insert("1.0", words_to_text(words))

    def text_changed(self, event=None):
        if self._text_after: self.top.after_cancel(self._text_after)
        self._text_after = self.top.after(600, lambda: self.review.set_edited_text(self.text.get("1.0", "end")))

    def retranscribe(self):
        self.whisper_changed(); self.review.retranscribe()

    def close(self):
        if self.preview: self.preview.destroy(); self.preview = None
        self.top.destroy()
        if self.review: self.review.editor = None
        else: self.app.style_editor = None

    destroy = close


class FacecamEditor:
    """See the detected facecam box on the real clip, nudge it with sliders, and watch the 9:16 result in the review window."""
    def __init__(self, app, review):
        self.app, self.review, self._muted = app, review, True
        self.top = tk.Toplevel(app.root); self.top.title("Adjust facecam"); self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.seg = review.clip_at_playhead(); self.own = self.seg is not None and bool(review.segs[self.seg].get("cam"))
        cam0, gx0 = review.cam_gx_for(self.seg)
        c = cam0 or dict(x=0.70, y=0.60, w=0.28, h=0.36)
        if self.own: tk.Label(self.top, text="You're placing the facecam for THIS clip only - the other clips keep theirs.", bg=BG, fg=YELLOW).pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(self.top, bg=BG, fg="#adadb8", wraplength=500, justify="left",
                 text="The red box is the part of the screen that goes on TOP of the vertical video. Drag it onto the streamer's camera, and drag its corners "
                      "to resize it. Drag on an empty spot to draw a new box."
                 ).pack(anchor="w", padx=12, pady=(10, 4))
        self.pane = VideoPane(self.top, 480, 270, "ORIGINAL CLIP + facecam box", clock=review.clock); self.pane.frame.pack(padx=12)
        Transport(self.top, review.clock, length=300).frame.pack(padx=12, pady=(4, 0))
        self.v = {k: tk.DoubleVar(value=round(c[k] * 100, 1)) for k in ("x", "y", "w", "h")}
        pn = tk.Frame(self.top, bg=PANEL); pn.pack(fill="x", padx=12, pady=8)
        f = tk.Frame(pn, bg=PANEL); f.pack(fill="x", padx=8, pady=(4, 6))
        self.auto = tk.BooleanVar(value=gx0 is None); self.gx = tk.DoubleVar(value=50 if gx0 is None else gx0 * 100)
        tk.Checkbutton(f, text="Gameplay slice: automatic (avoids the cam)", variable=self.auto, bg=PANEL, fg=FG, selectcolor=BG,
                       activebackground=PANEL, activeforeground=FG).pack(side="left")
        tk.Scale(f, from_=0, to=100, orient="horizontal", variable=self.gx, length=150, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0).pack(side="left", padx=8)
        for var in (self.auto, self.gx): var.trace_add("write", lambda *a: self.changed())
        f = tk.Frame(pn, bg=PANEL); f.pack(fill="x", padx=8, pady=(0, 6))
        tk.Label(f, text="Camera movement", bg=PANEL, fg=FG).pack(side="left")
        self.motion = tk.StringVar(value=next((k for k, v in CAM_MOTIONS.items() if v == (c.get("motion") or "none")), "Still")); purple_menu(f, self.motion, *CAM_MOTIONS).pack(side="left", padx=8)
        tk.Label(f, text="How much", bg=PANEL, fg=FG).pack(side="left")
        self.amt = tk.DoubleVar(value=c.get("amt", 20)); tk.Scale(f, from_=5, to=60, resolution=5, orient="horizontal", variable=self.amt, length=110, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE).pack(side="left")
        for var in (self.motion, self.amt): var.trace_add("write", lambda *a: self.changed())
        self.drag = None; self.alive = True
        cv = self.pane.canvas
        for ev, fn in (("<ButtonPress-1>", self.press), ("<B1-Motion>", self.move), ("<ButtonRelease-1>", self.release)):
            try: cv.bind(ev, fn)
            except Exception: pass
        self.msg = tk.Label(self.top, text="", bg=BG, fg=FG, wraplength=500, justify="left"); self.msg.pack(anchor="w", padx=12)
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="↻ Auto-detect again", bg=PANEL, fg=FG, relief="flat", command=review.redetect).pack(side="left")
        tk.Button(bar, text="Remember for this stream", bg=PANEL, fg=FG, relief="flat", command=self.remember).pack(side="left", padx=8)
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.close).pack(side="right", ipadx=10)
        self._muted = False; self.pane.play(review.item["raw"], fit_vf(480, 270)); self.draw(); self.loop()

    # ----- the box on the picture: drag to move, drag a corner to resize, drag on empty space to draw a new one -----
    def geom(self):
        SW, SH = self.review.size; W, H = self.pane.pump.w, self.pane.pump.h; sc = min(W / SW, H / SH); dw, dh = SW * sc, SH * sc
        return (W - dw) / 2, (H - dh) / 2, dw, dh

    def rect(self):
        ox, oy, dw, dh = self.geom(); c = self.current()
        return ox + c["x"] * dw, oy + c["y"] * dh, ox + (c["x"] + c["w"]) * dw, oy + (c["y"] + c["h"]) * dh

    def set_rect(self, r):
        ox, oy, dw, dh = self.geom(); cl = lambda v: max(0.0, min(1.0, v))
        x0, y0, x1, y1 = cl((r[0] - ox) / dw), cl((r[1] - oy) / dh), cl((r[2] - ox) / dw), cl((r[3] - oy) / dh)
        self._muted = True
        for k, v in (("x", x0), ("y", y0), ("w", max(0.05, x1 - x0)), ("h", max(0.05, y1 - y0))): self.v[k].set(round(v * 100, 1))
        self._muted = False

    def press(self, e):
        r = self.rect(); cor = {"nw": (r[0], r[1]), "ne": (r[2], r[1]), "sw": (r[0], r[3]), "se": (r[2], r[3])}
        for h, (hx, hy) in cor.items():
            if abs(e.x - hx) <= 11 and abs(e.y - hy) <= 11: self.drag = dict(mode=h, r0=r, sx=e.x, sy=e.y); return
        if r[0] <= e.x <= r[2] and r[1] <= e.y <= r[3]: self.drag = dict(mode="move", r0=r, sx=e.x, sy=e.y)
        else: self.drag = dict(mode="new", r0=r, sx=e.x, sy=e.y)

    def move(self, e):
        d = self.drag
        if not d: return
        r0, m = d["r0"], d["mode"]; ox, oy, dw, dh = self.geom()
        if m == "move":
            dx, dy = e.x - d["sx"], e.y - d["sy"]; w, h = r0[2] - r0[0], r0[3] - r0[1]
            x0 = max(ox, min(ox + dw - w, r0[0] + dx)); y0 = max(oy, min(oy + dh - h, r0[1] + dy)); r = (x0, y0, x0 + w, y0 + h)
        elif m == "new": r = (min(d["sx"], e.x), min(d["sy"], e.y), max(d["sx"], e.x), max(d["sy"], e.y))
        else:
            fx, fy = {"nw": (r0[2], r0[3]), "ne": (r0[0], r0[3]), "sw": (r0[2], r0[1]), "se": (r0[0], r0[1])}[m]
            r = (min(fx, e.x), min(fy, e.y), max(fx, e.x), max(fy, e.y))
        if r[2] - r[0] < 8 or r[3] - r[1] < 8: return
        self.set_rect(r); self.draw()

    def release(self, e):
        if self.drag: self.drag = None; self.changed()

    def loop(self):
        if not self.alive: return
        self.draw()
        try: self.top.after(120, self.loop)
        except Exception: pass

    def current(self):
        r = self.review; vw, vh = r.size
        x, y = max(0.0, min(0.95, self.v["x"].get() / 100)), max(0.0, min(0.95, self.v["y"].get() / 100))
        w, h = max(0.05, min(1 - x, self.v["w"].get() / 100)), max(0.05, min(1 - y, self.v["h"].get() / 100))
        return dict(x=x, y=y, w=w, h=h, ar=(w * vw) / (h * vh), src_ar=vw / vh, motion=CAM_MOTIONS.get(self.motion.get(), "none"), amt=int(self.amt.get()))

    def changed(self):
        if self._muted: return
        gx = None if self.auto.get() else self.gx.get() / 100
        if self.own and self.seg is not None and self.seg < len(self.review.segs): self.review.segs[self.seg]["cam"] = self.current(); self.review.segs[self.seg]["gx"] = gx
        else: self.review.cam = self.current(); self.review.gx = gx
        self.review.layout.set("Facecam on top (auto-detect)"); self.draw(); self.review.schedule_refresh()

    def load(self, cam):                                  # called when auto-detect finishes
        self._muted = True
        for k in ("x", "y", "w", "h"): self.v[k].set(round(cam[k] * 100, 1))
        self._muted = False; self.draw()

    def draw(self):
        cv = self.pane.canvas; cv.delete("cam")
        try: r = self.rect()
        except Exception: return
        cv.create_rectangle(*r, outline=RED, width=2, tags="cam")
        cv.create_text(r[0] + 5, r[1] + 3, text="FACECAM", anchor="nw", fill="white", font=("Segoe UI", 8, "bold"), tags="cam")
        for hx, hy in ((r[0], r[1]), (r[2], r[1]), (r[0], r[3]), (r[2], r[3])): cv.create_rectangle(hx - 5, hy - 5, hx + 5, hy + 5, fill="white", outline=RED, tags="cam")
        cv.tag_raise("cam")

    def remember(self):
        self.app.remember_cam(self.review.item.get("channel", ""), self.current()); self.msg.config(text="Saved. Clips from this stream will start with this box.")

    def close(self):
        self.alive = False; self.pane.destroy(); self.top.destroy(); self.review.cam_editor = None


class Timeline:
    """A timeline strip: drag to make a range, drag its ends to resize, drag its middle to move.
    lanes=False: one lane, you create ranges by dragging on empty space (voice focus).
    lanes=True : one lane per item (blur boxes - the boxes themselves are drawn on the video).
    items are dicts with t0/t1 in seconds (edited in place). `wave` = loudness in dB, drawn behind the ranges."""
    LH, EDGE, MIN = 22, 6, 0.3

    def __init__(self, canvas, dur, items, lanes=False, wave=None, step=0.1, on_change=None, width=640, color="#9147ff"):
        self.c, self.dur, self.items, self.lanes, self.wave, self.step = canvas, max(0.5, float(dur)), items, lanes, wave, step
        self.on_change, self.W, self.color = on_change, width, color
        self.x0, self.creatable, self.no_move = 0, True, False
        self.snap_fn, self.snap_t = None, None             # snap_fn(index) -> times (the playhead, cuts, other bars' edges) that a dragged edge sticks to
        self.sel, self.mode, self.grab, self.anchor, self.new = None, None, 0.0, 0.0, False
        self.wh = 40 if wave is not None and len(wave) else 0
        self.y0 = self.wh + 4
        self.lane_idx, self.nlanes = [], 1; self.pack_lanes()

    def pack_lanes(self):
        """Bars share a row unless they overlap in time; only an overlapping bar gets a new row."""
        n = len(self.items)
        if not self.lanes: self.lane_idx, self.nlanes = [0] * n, 1; return
        ends, idx = [], [0] * n
        for i in sorted(range(n), key=lambda k: self.get(k)):
            a, b = self.get(i)
            for L, e in enumerate(ends):
                if e <= a + 0.001: ends[L] = b; idx[i] = L; break
            else: ends.append(b); idx[i] = len(ends) - 1
        self.lane_idx, self.nlanes = idx, max(1, len(ends))

    def lane_of(self, i): return self.lane_idx[i] if self.lanes and i < len(self.lane_idx) else 0

    def item_at(self, x, y):
        lane = self.lane_at(y)
        for i in range(len(self.items)):
            if self.lane_of(i) == lane:
                a, b = self.get(i)
                if self.t2x(a) - 2 <= x <= self.t2x(b) + 2: return i

    def height(self): return self.y0 + (self.nlanes if self.lanes else 1) * self.LH + 16
    def x2t(self, x): return max(0.0, min(self.dur, (x - self.x0) / self.W * self.dur))
    def t2x(self, t): return self.x0 + t / self.dur * self.W
    def get(self, i): it = self.items[i]; return it.get("t0", 0.0), it.get("t1", self.dur)
    def put(self, i, a, b): self.items[i]["t0"], self.items[i]["t1"] = round(a, 2), round(b, 2)
    def lane_at(self, y): return int((y - self.y0) // self.LH) if self.lanes else 0

    def press(self, x, y):
        t, lane = self.x2t(x), self.lane_at(y)
        self.new = False; self.pack_lanes()
        for i in range(len(self.items)):
            if self.lanes and self.lane_of(i) != lane: continue
            a, b = self.get(i); xa, xb = self.t2x(a), self.t2x(b)
            if abs(x - xa) <= self.EDGE: self.sel, self.mode = i, "l"; return
            if abs(x - xb) <= self.EDGE: self.sel, self.mode = i, "r"; return
            if xa < x < xb: self.sel, self.mode, self.grab = i, (None if self.no_move else "m"), t - a; return
        if self.lanes:
            self.sel, self.mode = None, None
        elif self.creatable:
            self.items.append(dict(t0=round(t, 2), t1=round(t, 2))); self.sel, self.mode, self.anchor, self.new = len(self.items) - 1, "new", t, True
        else:
            self.sel, self.mode = None, None

    def snap(self, v, pts):
        th = 8.0 / max(1.0, self.W) * self.dur                    # about 8 pixels
        best = min(pts, key=lambda p: abs(p - v)) if pts else None
        return best if best is not None and abs(best - v) <= th else None

    def drag(self, x, y=0):
        if self.sel is None or self.mode is None: return
        t = self.x2t(x); a, b = self.get(self.sel); pts = self.snap_fn(self.sel) if self.snap_fn else []; self.snap_t = None
        if self.mode == "l":
            sn = self.snap(t, pts); t = sn if sn is not None else t; self.snap_t = sn; a = min(t, b - self.MIN)
        elif self.mode == "r":
            sn = self.snap(t, pts); t = sn if sn is not None else t; self.snap_t = sn; b = max(t, a + self.MIN)
        elif self.mode == "m":
            w = b - a; a = max(0.0, min(self.dur - w, t - self.grab)); b = a + w
            sa, sb = self.snap(a, pts), self.snap(b, pts)
            if sa is not None and (sb is None or abs(sa - a) <= abs(sb - b)): a, b, self.snap_t = sa, sa + w, sa
            elif sb is not None: a, b, self.snap_t = sb - w, sb, sb
        elif self.mode == "new":
            sn = self.snap(t, pts); t = sn if sn is not None else t; self.snap_t = sn; a, b = min(self.anchor, t), max(self.anchor, t)
        self.put(self.sel, max(0.0, a), min(self.dur, b)); self.pack_lanes()

    def release(self, x=0, y=0):
        if self.sel is not None and self.new:
            a, b = self.get(self.sel)
            if b - a < self.MIN:
                self.items.pop(self.sel); self.sel = None
        self.mode, self.new, self.snap_t = None, False, None
        if self.on_change: self.on_change()

    def delete_selected(self):
        if self.sel is not None and not self.lanes and 0 <= self.sel < len(self.items):
            self.items.pop(self.sel); self.sel = None
            if self.on_change: self.on_change()

    def draw(self, playhead=None):
        c = self.c; c.delete("all"); c.config(height=self.height(), width=self.W)
        c.create_rectangle(0, 0, self.W, self.height(), fill=PANEL, outline="")
        if self.wh:
            n = len(self.wave); bw = self.W / n
            for i, d in enumerate(self.wave):
                h = max(0.0, min(1.0, (float(d) + 60) / 60)) * (self.wh - 2)
                c.create_rectangle(i * bw, self.wh - h, (i + 1) * bw + 0.5, self.wh, fill="#3a3a44", outline="")
        self.pack_lanes(); n_lanes = self.nlanes
        for k in range(n_lanes):
            c.create_rectangle(0, self.y0 + k * self.LH + 1, self.W, self.y0 + (k + 1) * self.LH - 1, fill=BG, outline="")
        for i in range(len(self.items)):
            a, b = self.get(i); k = self.lane_of(i); ya = self.y0 + k * self.LH
            c.create_rectangle(self.t2x(a), ya + 2, self.t2x(b), ya + self.LH - 2, fill=self.color,
                               outline="white" if i == self.sel else "", width=2 if i == self.sel else 1)
            if self.lanes: c.create_text(self.t2x(a) + 4, ya + self.LH / 2, text=f"box {i + 1}", anchor="w", fill="white", font=("Segoe UI", 8))
        step = 1 if self.dur <= 20 else 5 if self.dur <= 90 else 10
        for tt in range(0, int(self.dur) + 1, step):
            c.create_text(self.t2x(tt), self.height() - 7, text=f"{tt}s", fill="#adadb8", font=("Segoe UI", 7))
        if playhead is not None:
            c.create_line(self.t2x(playhead), 0, self.t2x(playhead), self.height(), fill="#00e5ff", width=2)

    def bind(self):
        self.c.bind("<ButtonPress-1>", lambda e: (self.press(e.x, e.y), self.draw()))
        self.c.bind("<B1-Motion>", lambda e: (self.drag(e.x, e.y), self.draw()))
        self.c.bind("<ButtonRelease-1>", lambda e: self.release(e.x, e.y))


def line_text(parent, var, width=16, height=2, **kw):
    """A small multi-line box tied to a StringVar: Enter starts a new row (the hook's rows stack on top of each other)."""
    t = tk.Text(parent, width=width, height=height, bg=kw.get("bg", PANEL), fg=FG, insertbackground=FG, relief="flat", wrap="word", undo=True)
    state = {"busy": False}
    def to_var(e=None):
        if state["busy"]: return
        state["busy"] = True
        try:
            txt = t.get("1.0", "end-1c")
            if var.get() != txt: var.set(txt)
        finally: state["busy"] = False
    def to_box(*a):
        if state["busy"]: return
        state["busy"] = True
        try:
            if t.get("1.0", "end-1c") != var.get(): t.delete("1.0", "end"); t.insert("1.0", var.get())
        finally: state["busy"] = False
    to_box(); t.bind("<KeyRelease>", to_var); t.bind("<<Paste>>", lambda e: t.after(10, to_var)); t.bind("<FocusOut>", to_var)
    var.trace_add("write", to_box)
    return t


class TextDialog:
    """Add or edit one text overlay or the HOOK: the words, size, font, colours, outline, box and (for the hook) how long it stays. Changes show on the preview straight away."""
    POS = {"Top": "top", "Middle": "middle", "Bottom": "bottom"}

    def __init__(self, review, item, is_new):
        self.r, self.item, self.is_new = review, item, is_new; self.backup = dict(item); self.hook = bool(item.get("hook")); self._mute = True
        self.top = tk.Toplevel(review.top); self.top.title("Hook" if self.hook else "Text overlay"); self.top.configure(bg=BG)
        if is_new and item not in review.texts: review.texts.append(item)
        tk.Label(self.top, text=("The hook: big words shown at the very start of the clip." if self.hook else "Text to show on the video (its bar on the Text row sets when it appears):"),
                 bg=BG, fg=FG, wraplength=380, justify="left").pack(anchor="w", padx=12, pady=(12, 4))
        self.text = tk.StringVar(value=item.get("text", ""))
        e = line_text(self.top, self.text, width=46, height=3 if self.hook else 2); e.pack(padx=12, ipady=2)
        tk.Label(self.top, text="Press Enter to start a new row - the rows stack on top of each other.", bg=BG, fg="#adadb8").pack(anchor="w", padx=12)
        box = tk.Frame(self.top, bg=PANEL); box.pack(fill="x", padx=12, pady=8)
        def scale(label, var, lo, hi, res):
            f = tk.Frame(box, bg=PANEL); f.pack(fill="x", padx=8, pady=1); tk.Label(f, text=label, bg=PANEL, fg=FG, width=12, anchor="w").pack(side="left")
            tk.Scale(f, from_=lo, to=hi, resolution=res, orient="horizontal", variable=var, length=240, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE).pack(side="left")
        d = float(review.item["dur"])
        self.size = tk.DoubleVar(value=round(float(item.get("size", 1.0)) * 100)); scale("Size %", self.size, 30, 300, 5)
        self.outline = tk.DoubleVar(value=float(item.get("outline", 6 if not self.hook else 7))); scale("Outline", self.outline, 0, 16, 1)
        self.length = tk.DoubleVar(value=round(max(0.5, float(item.get("t1", 3.0)) - float(item.get("t0", 0.0))), 1))
        if self.hook: scale("Shown for (s)", self.length, 0.5, 15, 0.5)
        self.blurbg = tk.BooleanVar(value=bool(item.get("blurbg"))); self.blurfade = tk.DoubleVar(value=float(item.get("blurfade", 0.6)))
        if self.hook:
            f0 = tk.Frame(box, bg=PANEL); f0.pack(fill="x", padx=8, pady=(4, 0))
            tk.Checkbutton(f0, text="Blur the whole background behind the hook (the words stay sharp)", variable=self.blurbg, bg=PANEL, fg=FG, selectcolor=BG,
                           activebackground=PANEL, activeforeground=FG).pack(side="left")
            scale("Blur fades out (s)", self.blurfade, 0, 3, 0.1)
        f = tk.Frame(box, bg=PANEL); f.pack(fill="x", padx=8, pady=2); tk.Label(f, text="Font", bg=PANEL, fg=FG, width=12, anchor="w").pack(side="left")
        self.font = tk.StringVar(value=item.get("font", "Impact")); purple_menu(f, self.font, *dict.fromkeys(FONTS + list(imported_fonts() or []) + [self.font.get()])).pack(side="left")
        self.colors = dict(color=item.get("color") or review.style.get("hook_color", "#FF1F1F"), outline_color=item.get("outline_color") or "#000000", box_color=item.get("box_color") or "#000000")
        self.sw = {}
        for key, label in (("color", "Text colour"), ("outline_color", "Outline colour"), ("box_color", "Box colour")):
            f = tk.Frame(box, bg=PANEL); f.pack(fill="x", padx=8, pady=2); tk.Label(f, text=label, bg=PANEL, fg=FG, width=12, anchor="w").pack(side="left")
            self.sw[key] = tk.Button(f, text="   ", bg=self.colors[key], relief="flat", width=6, command=lambda k=key: self.pick(k)); self.sw[key].pack(side="left")
        f = tk.Frame(box, bg=PANEL); f.pack(fill="x", padx=8, pady=(2, 6))
        self.bold, self.boxon, self.pop = tk.BooleanVar(value=bool(item.get("bold"))), tk.BooleanVar(value=bool(item.get("box"))), tk.BooleanVar(value=bool(item.get("pop")))
        for text, var in (("Bold", self.bold), ("Background box", self.boxon), ("Pop in", self.pop)):
            tk.Checkbutton(f, text=text, variable=var, bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG).pack(side="left", padx=(0, 10))
        row = tk.Frame(self.top, bg=BG); row.pack(fill="x", padx=12, pady=(0, 4))
        tk.Label(row, text="Move to", bg=BG, fg=FG).pack(side="left")
        self.pos = tk.StringVar(value=next((k for k, v in self.POS.items() if v == item.get("pos", "top")), "Top")); self.pos_btn = purple_menu(row, self.pos, *self.POS); self.pos_btn.pack(side="left", padx=8)
        tk.Label(row, text="(or just drag it on the preview)", bg=BG, fg="#adadb8").pack(side="left")
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=(6, 12))
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.done).pack(side="right", ipadx=10)
        tk.Button(bar, text="Cancel", bg=PANEL, fg=FG, relief="flat", command=self.cancel).pack(side="right", padx=8)
        tk.Button(bar, text="Delete", bg=PANEL, fg=RED, relief="flat", command=self.delete).pack(side="left")
        for v in (self.text, self.size, self.outline, self.length, self.font, self.bold, self.boxon, self.pop, self.blurbg, self.blurfade): v.trace_add("write", lambda *a: self.apply())
        self.pos.trace_add("write", lambda *a: self.move_preset())
        self.top.protocol("WM_DELETE_WINDOW", self.done); self._mute = False
        try: e.focus_set()
        except Exception: pass
        self.apply()

    def pick(self, key):
        try: _, hexv = colorchooser.askcolor(color=self.colors[key], parent=self.top, title="Pick a color")
        except Exception: return
        if hexv:
            self.colors[key] = hexv; self.sw[key].config(bg=hexv)
            if key == "box_color": self.boxon.set(True)
            self.apply()

    def move_preset(self):
        if self._mute: return
        p = self.POS.get(self.pos.get(), "top"); self.item.update(pos=p, x=0.5, y=TEXT_PRESET_Y[p][0 if self.r.vertical.get() else 1]); self.r.texts_changed()

    def apply(self):
        if self._mute: return
        it = self.item; t0 = float(it.get("t0", 0.0)); d = float(self.r.item["dur"])
        it.update(text=self.text.get(), size=round(float(self.size.get()) / 100.0, 3), outline=float(self.outline.get()), font=self.font.get(), bold=bool(self.bold.get()),
                  box=bool(self.boxon.get()), pop=bool(self.pop.get()), **self.colors)
        if self.hook: it["t1"] = round(min(d, t0 + float(self.length.get())), 2); it.update(blurbg=bool(self.blurbg.get()), blurfade=round(float(self.blurfade.get()), 2))
        if self.hook: self.r.hook_text_from_item(); self.r.hook_blur.set(bool(self.blurbg.get())); self.r.hook_len_sync()
        self.r.texts_changed()

    def done(self):
        if not self.text.get().strip() and self.item in self.r.texts: self.r.texts.remove(self.item)
        self.r.dialog = None; self.top.destroy(); self.r.texts_changed(); self.r.hook_entry_sync()

    def cancel(self):
        if self.is_new:
            if self.item in self.r.texts: self.r.texts.remove(self.item)
        else: self.item.clear(); self.item.update(self.backup)
        self.top.destroy(); self.r.texts_changed(); self.r.hook_entry_sync()

    def delete(self):
        if self.item in self.r.texts: self.r.texts.remove(self.item)
        self.top.destroy(); self.r.texts_changed(); self.r.hook_entry_sync()


class TrackTimeline:
    """The big timeline at the bottom of the review window: a ruler with the clip's loudness (click it to jump; the green line is the clip's
    volume - drag it down to turn the sound down), then rows for the clip, captions, blur boxes, text, music, voice-focus and volume parts.
    Bars that don't overlap share a row. Drag bars to move them, drag their ends to resize, click a diamond for a keyframe."""
    LW, RH, LH, LV = 100, 30, 24, 52                          # LV = height of the Audio level row
    COLORS = dict(volume="#10b981", sfx="#fb923c", tts="#38bdf8", vfx="#f472b6", clip="#64748b", caption="#3b82f6", blur="#ff3b3b", text="#a970ff", overlay="#f59e0b", music="#9147ff", voice="#2dd4bf")
    HINTS = dict(sfx="none - press + Add > Sound effect", tts="none - press + Add > Text to speech", vfx="none - press + Add > Chipmunk voice / Deep voice", volume="none - press + Volume part (louder / quieter / silent for a stretch)", clip="", caption="none yet - tick Captions and Whisper will fill this in live", blur="none - press + Blur box", text="none - press + Text", overlay="none - drag a picture / GIF here, or press + Overlay", music="none - press + Music", voice="none - drag here, or press + Voice part")
    NAMES = dict(sfx="Sound effect", tts="Speech", vfx="Voice effect", clip="Clip", caption="Captions", blur="Blur", text="Text", overlay="Overlay", music="Music", voice="Voice focus", volume="Volume")

    def __init__(self, canvas, review):
        self.c, self.r = canvas, review; self.W, self.yoff, self.sel, self.active, self.scrub, self.wave, self.H = 900, 0, None, None, False, None, 300
        self.sel_kf, self.vol_drag, self._m0 = None, False, None
        self.multi, self.marq, self.scrub_t = [], None, None                         # multi = bars picked by dragging a box; marq = the box being dragged (x0, y0, x1, y1)
        try: self.wave = loudness_curve(review.item["raw"])
        except Exception: self.wave = None
        self.tracks = []; self.refresh()
        c = canvas
        c.bind("<ButtonPress-1>", self.down); c.bind("<B1-Motion>", self.move); c.bind("<ButtonRelease-1>", self.up)
        c.bind("<Double-Button-1>", self.double); c.bind("<MouseWheel>", self.wheel)
        c.bind("<Button-4>", lambda e: self.scroll(-1)); c.bind("<Button-5>", lambda e: self.scroll(1))

    def dur(self): return max(0.5, float(self.r.item["dur"]))

    def snap_points(self, key=None, idx=None):
        """Times a dragged bar sticks to: the playhead, the start / end, every cut and every other bar's start and end."""
        r = self.r; pts = {0.0, round(self.dur(), 3)}
        try: pts.add(round(r.clock.pos(), 3))
        except Exception: pass
        for sg in r.segs: pts.update((round(sg["t0"], 3), round(sg["t1"], 3)))
        for t in self.tracks:
            if t["key"] in ("caption", "level", "clip"): continue
            for j, it in enumerate(t["items"]):
                if t["key"] == key and j == idx: continue
                pts.update((round(it.get("t0", 0.0), 3), round(it.get("t1", self.dur()), 3)))
        return sorted(pts)

    def scrub_snap(self, t):
        """Moving the playhead along the ruler clicks onto cuts and bar edges that are close."""
        th = 6.0 / max(100, self.W - self.LW - 10) * self.dur(); pts = self.snap_points()
        best = min(pts, key=lambda p: abs(p - t)); self.scrub_t = best if abs(best - t) <= th else None
        return best if self.scrub_t is not None else t

    def set_size(self, w, h):
        self.W, self.H = max(300, w), max(60, h); self.layout()

    def refresh(self):
        r, m = self.r, self.r.music
        music_items = [dict(t0=m["at"], t1=m["until"])] if m.get("path") else []
        self.tracks = []
        cap_items = [dict(t0=w["s"], t1=max(w["e"], w["s"] + 0.08), text=w["w"]) for w in (r.words or [])]
        for key, items, lanes, creatable in (("clip", r.segs, False, False), ("level", [], False, False), ("caption", cap_items, False, False), ("blur", r.blurs, True, False), ("text", r.texts, True, False), ("overlay", r.overlays, True, False),
                                             ("music", music_items, False, False), ("voice", r.voice["ranges"], False, True),
                                             ("vfx", r.clip_audio.setdefault("fx", []), True, False), ("tts", r.clip_audio.setdefault("tts", []), True, False), ("sfx", r.clip_audio.setdefault("sfx", []), True, False), ("volume", r.clip_audio["ranges"], True, False)):
            tl = Timeline(None, self.dur(), items, lanes=lanes, width=100); tl.x0, tl.creatable, tl.LH, tl.no_move = self.LW, creatable, self.LH, (key == "clip")
            tl.snap_fn = (lambda i, _k=key: self.snap_points(_k, i)) if key not in ("clip", "caption", "level") else None
            self.tracks.append(dict(key=key, items=items, lanes=lanes, tl=tl))
        self.layout()

    def layout(self):
        y = self.RH
        for t in self.tracks:
            t["rows"] = t["tl"].nlanes if t["lanes"] else 1; t["hpx"] = self.LV if t["key"] == "level" else t["rows"] * self.LH
            t["tl"].y0, t["tl"].W = y, max(100, self.W - self.LW - 10); y += t["hpx"]
        self.total = y + 6

    def _t(self, x): return max(0.0, min(self.dur(), (x - self.LW) / max(100, self.W - self.LW - 10) * self.dur()))

    def _find(self, y):
        for t in self.tracks:
            if t["tl"].y0 <= y < t["tl"].y0 + t.get("hpx", t["rows"] * self.LH): return t

    # --- the volume line across the ruler ---
    def level_top(self):
        lv = next((t for t in self.tracks if t["key"] == "level"), None)
        return lv["tl"].y0 if lv else self.RH

    def vol_y(self):
        ca = self.r.clip_audio; v = 0.0 if ca.get("mute") else max(0.0, min(1.0, float(ca.get("vol", 1.0))))
        return self.level_top() + 8 + (1.0 - v) * (self.LV - 16)

    def down(self, e):
        try: self.c.focus_set()
        except Exception: pass
        if self.r.splicing: return
        y = e.y + self.yoff; self.sel_kf = None; self.marq = None
        shift = bool(getattr(e, "state", 0) & 1)
        if self.multi and not shift: self.multi = []
        if y < self.RH:                                                                             # the ruler only ever jumps - it can't change the volume
            self.scrub = True; self.r.clock.seek(self.scrub_snap(self._t(e.x))); return
        t = self._find(y); self.active = t
        if not t: return
        if t["key"] == "level":                                                                     # the Audio row: grab the green line (or its dot) to change the volume
            self.active = None
            if e.x >= self.LW and abs(y - self.vol_y()) <= 8: self.vol_drag = True
            elif e.x < self.LW + 24 and abs(y - self.vol_y()) <= 12: self.vol_drag = True
            else: self.scrub = True; self.r.clock.seek(self._t(e.x))
            return
        tl = t["tl"]
        if e.x >= self.LW and self.empty_spot(t, e.x, y) and (shift or not t["tl"].creatable) and t["key"] != "caption":
            self.active = None; self.sel = None; self.marq = (e.x, y, e.x, y); return              # empty space: start dragging a selection box
        if t["key"] == "caption":                                                                    # click a word: select it (Delete removes it) and jump there
            self.active = None; i = self.word_at(t, e.x)
            if i is None: self.sel = None; self.marq = (e.x, y, e.x, y)
            else: self.sel = ("caption", i)
            self.r.clock.seek(self._t(e.x)); return
        if t["key"] in ("blur", "text", "overlay"):                                                 # a keyframe diamond?
            for i, it in enumerate(t["items"]):
                for j, k in enumerate(it.get("kf") or []):
                    if abs(e.x - tl.t2x(k["t"])) <= 7 and abs(y - (tl.y0 + tl.lane_of(i) * self.LH + self.LH / 2)) <= 9:
                        self.sel, self.sel_kf, self.active = (t["key"], i), (t["key"], i, j), None
                        self.r.clock.seek(k["t"]); self.r.say("Keyframe selected - press Delete to remove it, or drag the element on the preview to change it."); return
        tl.press(e.x, y)
        self.sel = (t["key"], tl.sel) if tl.sel is not None else None
        self._m0 = (t["items"][tl.sel], t["items"][tl.sel].get("t0", 0.0)) if (tl.mode == "m" and tl.sel is not None) else None

    def word_at(self, t, x):
        tl = t["tl"]
        for i, it in enumerate(t["items"]):
            if tl.t2x(it["t0"]) - 1 <= x <= tl.t2x(it["t1"]) + 1: return i

    def empty_spot(self, t, x, y):
        tl = t["tl"]
        if t["key"] == "caption": return self.word_at(t, x) is None
        if t["lanes"]: return tl.item_at(x, y) is None
        return not any(tl.t2x(tl.get(i)[0]) - 2 <= x <= tl.t2x(tl.get(i)[1]) + 2 for i in range(len(t["items"])))

    def box_pick(self, x0, y0, x1, y1):
        """Everything whose bar touches the dragged box."""
        xa, xb, ya, yb = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1); out = []
        for t in self.tracks:
            if t["key"] == "level": continue
            tl = t["tl"]
            for i in range(len(t["items"])):
                a, b = tl.get(i); ry = tl.y0 + (tl.lane_of(i) if t["lanes"] else 0) * self.LH
                if tl.t2x(b) >= xa and tl.t2x(a) <= xb and ry + self.LH > ya and ry < yb: out.append((t["key"], i))
        return out

    def move(self, e):
        y = e.y + self.yoff
        if self.marq: self.marq = (self.marq[0], self.marq[1], e.x, y)
        elif self.vol_drag:
            v = 1.0 - (y - self.level_top() - 8) / (self.LV - 16); v = max(0.0, min(1.0, v)); v = 1.0 if v > 0.95 else (0.0 if v < 0.03 else v)
            self.r.clip_audio.update(vol=v, mute=False)
        elif self.scrub: self.r.clock.seek(self.scrub_snap(self._t(e.x)))
        elif self.active: self.active["tl"].drag(e.x, y)

    def up(self, e):
        if self.vol_drag:
            self.vol_drag = False; self.r.audio_changed(); self.r.sync_vol_editor(); return
        if self.scrub: self.scrub = False; self.scrub_t = None; return
        if self.marq:
            x0, y0, x1, y1 = self.marq; self.marq = None
            if abs(x1 - x0) < 4 and abs(y1 - y0) < 4:                                  # just a click on empty space
                self.multi = []; self.r.clock.seek(self._t(x0)); return
            self.multi = self.box_pick(x0, y0, x1, y1); self.sel = None
            n = len(self.multi)
            self.r.say(f"{n} selected - press Delete to remove them (or drag a new box / click elsewhere to pick again)." if n else "Nothing inside that box.")
            return
        t, self.active = self.active, None
        if not t: return
        t["tl"].release()
        if self._m0:                                            # the bar was moved: its keyframes move with it
            it, t0 = self._m0; dt = it.get("t0", 0.0) - t0
            if abs(dt) > 1e-6 and it.get("kf"):
                for k in it["kf"]: k["t"] = round(k["t"] + dt, 2)
        self._m0 = None; self.commit(t["key"])

    def commit(self, key):
        r = self.r
        if key == "music":
            it = next(t for t in self.tracks if t["key"] == "music")["items"]
            if it: r.music.update(at=it[0]["t0"], until=max(it[0]["t1"], it[0]["t0"] + 0.3))
            r.music_changed()
        elif key == "voice": r.voice_changed()
        elif key == "blur": r.blurs_changed()
        elif key == "text": r.texts_changed()
        elif key == "overlay": r.overlays_changed()
        elif key == "clip": r.cuts_changed()
        elif key in ("volume", "vfx", "tts", "sfx"): r.audio_changed()
        self.refresh()

    def double(self, e):
        y = e.y + self.yoff; t = self._find(y)
        if not t or y < self.RH: return
        i = t["tl"].item_at(e.x, y) if t["lanes"] else 0
        if t["key"] == "text" and i is not None and i < len(self.r.texts): self.r.edit_text(i)
        elif t["key"] == "music": self.r.open_music_editor()
        elif t["key"] == "voice": self.r.open_voice_editor()
        elif t["key"] == "caption": self.r.open_editor()
        elif t["key"] == "volume" and i is not None and i < len(self.r.clip_audio["ranges"]): self.r.edit_volume_part(i)
        elif t["key"] == "overlay" and i is not None and i < len(self.r.overlays): OverlayDialog(self.r, i)
        elif t["key"] == "clip" and i is not None and i < len(self.r.segs): SpeedDialog(self.r, i)
        elif t["key"] == "vfx" and i is not None and i < len(self.r.clip_audio.get("fx", [])): self.r.edit_voice_fx(i)
        elif t["key"] == "tts" and i is not None and i < len(self.r.clip_audio.get("tts", [])): self.r.open_tts(i)
        elif t["key"] == "sfx" and i is not None and i < len(self.r.clip_audio.get("sfx", [])): self.r.edit_sfx(i)

    def delete_selected(self):
        r = self.r
        if self.sel_kf:                                         # a keyframe is selected: remove just that
            key, i, j = self.sel_kf; self.sel_kf = None
            try:
                it = r.items_of(key)[i]; it["kf"].pop(j)
                if not it["kf"]: it.pop("kf")
            except (IndexError, KeyError): pass
            return self.commit(key)
        if self.multi: return self.delete_many()
        if not self.sel: return r.say("Click a bar or a caption word on the timeline (or drag a box around several) first, then press Delete.")
        key, i = self.sel; self.sel = None
        if key == "caption": return self.delete_words([i])
        try:
            if key == "blur": r.blurs.pop(i)
            elif key == "text": r.texts.pop(i)
            elif key == "overlay": r.overlays.pop(i)
            elif key == "voice": r.voice["ranges"].pop(i)
            elif key == "volume": r.clip_audio["ranges"].pop(i)
            elif key == "vfx": r.clip_audio["fx"].pop(i)
            elif key == "tts": r.clip_audio["tts"].pop(i)
            elif key == "sfx": r.clip_audio["sfx"].pop(i)
            elif key == "clip":
                if len(r.segs) <= 1: return r.say("That's the whole clip - drag its ends to trim it, or press Ctrl+B to split it first.")
                r.segs.pop(i)
            elif key == "music": r.music.update(on=False, path=None, title="")
        except IndexError: return
        self.commit(key)

    def delete_words(self, idxs):
        """Remove caption words (from what is shown and from the transcript, so editing the text later doesn't bring them back)."""
        r = self.r; cap = next(t for t in self.tracks if t["key"] == "caption")["items"]
        if not r.words: return
        gone = []
        for i in idxs:
            if 0 <= i < len(cap): gone.append((round(cap[i]["t0"], 3), cap[i].get("text", "")))
        gone_set = set(gone); keep = lambda w: (round(w["s"], 3), w["w"]) not in gone_set
        r.words = [w for w in r.words if keep(w)]
        if r.base_words: r.base_words = [w for w in r.base_words if keep(w)]
        try:
            if r.editor: r.editor.set_transcript(r.words)
        except Exception: pass
        self.refresh(); r.schedule_refresh(); r.say(f"Removed {len(gone)} caption word(s). Ctrl+Z brings them back.")

    def delete_many(self):
        r = self.r; picks, self.multi, self.sel = list(self.multi), [], None
        by = {}
        for key, i in picks: by.setdefault(key, []).append(i)
        words = by.pop("caption", [])
        for key, idx in by.items():
            idx = sorted(set(idx), reverse=True)
            try:
                if key == "blur":
                    for i in idx: r.blurs.pop(i)
                elif key == "text":
                    for i in idx: r.texts.pop(i)
                elif key == "overlay":
                    for i in idx: r.overlays.pop(i)
                elif key == "voice":
                    for i in idx: r.voice["ranges"].pop(i)
                elif key == "volume":
                    for i in idx: r.clip_audio["ranges"].pop(i)
                elif key == "vfx":
                    for i in idx: r.clip_audio["fx"].pop(i)
                elif key == "tts":
                    for i in idx: r.clip_audio["tts"].pop(i)
                elif key == "sfx":
                    for i in idx: r.clip_audio["sfx"].pop(i)
                elif key == "clip":
                    if len(idx) >= len(r.segs): r.say("That would remove the whole clip - the clip bars were left alone.")
                    else:
                        for i in idx: r.segs.pop(i)
                elif key == "music": r.music.update(on=False, path=None, title="")
            except IndexError: pass
            self.commit(key)
        if words: self.delete_words(words)
        r.say(f"Deleted {len(picks)} item(s). Ctrl+Z brings them back.")

    def lists(self):
        r = self.r
        return dict(volume=r.clip_audio["ranges"], sfx=r.clip_audio.setdefault("sfx", []), tts=r.clip_audio.setdefault("tts", []), vfx=r.clip_audio.setdefault("fx", []), clip=r.segs, blur=r.blurs, text=r.texts, overlay=r.overlays, voice=r.voice["ranges"])

    def split_selected(self):
        """Ctrl+B: cut the selected bar in two at the playhead. With nothing selected, cut the clip itself there."""
        r = self.r; pos = r.clock.pos(); dur = self.dur(); key, i = self.sel if self.sel else (None, None)
        if key is None:
            i = next((k for k, sg in enumerate(r.segs) if sg["t0"] + 0.1 < pos < sg["t1"] - 0.1), None)
            if i is None: return r.say("Move the playhead onto the clip (not onto a cut part) and press Ctrl+B again.")
            key = "clip"
        if key == "music":
            if pos > r.music.get("at", 0.0) + 0.3:
                r.music.update(until=round(pos, 2)); r.music_changed(); self.refresh(); r.say("Music cut at the playhead (music is one piece, so everything after it is dropped).")
            else: r.say("Put the playhead inside the music bar first.")
            return
        items = self.lists().get(key)
        if items is None or not (0 <= i < len(items)): return
        it = items[i]; a, b = it.get("t0", 0.0), it.get("t1", dur)
        if not (a + 0.15 < pos < b - 0.15): return r.say("Put the playhead inside the selected bar, then press Ctrl+B.")
        new = json.loads(json.dumps(it)); new.pop("join", None); it["t0"], it["t1"], new["t0"], new["t1"] = round(a, 2), round(pos, 2), round(pos, 2), round(b, 2)
        items.insert(i + 1, new); self.sel = (key, i + 1); self.commit(key)
        r.say(f"Cut at {pos:.1f}s." + (" The right-hand piece is selected - press Delete to remove it from the clip." if key == "clip" else ""))

    # --- copy / paste (blur boxes, text, voice parts, volume parts) ---
    def copy_selected(self):
        r = self.r
        if not self.sel: return r.say("Click a blur box, text or bar first, then press Ctrl+C.")
        key, i = self.sel; items = self.lists().get(key)
        if key in ("clip",) or items is None or not (0 <= i < len(items)): return r.say("Blur boxes, text, voice parts and volume parts can be copied.")
        r.clipboard = (key, json.loads(json.dumps(items[i])))
        r.say("Copied. Move the playhead to where you want it and press Ctrl+V."); return True

    def paste(self):
        r = self.r
        if not r.clipboard: return r.say("Nothing copied yet - select something and press Ctrl+C.")
        key, it = r.clipboard; items = self.lists()[key]; new = json.loads(json.dumps(it)); dur = self.dur()
        a, b = it.get("t0", 0.0), it.get("t1", dur); L = max(0.3, b - a); pos = max(0.0, min(r.clock.pos(), dur - 0.3))
        na, nb = pos, min(dur, pos + L); dt = na - a
        new["t0"], new["t1"] = round(na, 2), round(nb, 2)
        for k in new.get("kf") or []: k["t"] = round(k["t"] + dt, 2)
        def nudge(d):                                           # a pasted copy is shifted a little so you can see it's a copy
            if "x" in d: d["x"] = round(min(max(0.0, 1.0 - d.get("w", 0.0)), d["x"] + 0.03), 4)
            if "y" in d: d["y"] = round(min(max(0.0, 1.0 - d.get("h", 0.0)), d["y"] + 0.03), 4)
        if key in ("blur", "text", "overlay"):
            nudge(new)
            for k in new.get("kf") or []: nudge(k)
        items.append(new); self.sel = (key, len(items) - 1); self.commit(key)
        r.say("Pasted at the playhead.")

    def scroll(self, d):
        self.yoff = max(0, min(max(0, self.total - self.H), self.yoff + d * self.LH))

    def wheel(self, e): self.scroll(-1 if e.delta > 0 else 1)

    def draw_level(self, t, Y, span):
        """The Audio row under the clip: the clip's loudness, and the green volume line you drag up / down."""
        c, W, y0 = self.c, self.W, t["tl"].y0; x1 = W - 10
        c.create_rectangle(0, Y(y0), W, Y(y0 + self.LV - 1), fill=PANEL, outline="")
        c.create_text(6, Y(y0 + 11), text="Audio", anchor="w", fill=FG, font=("Segoe UI", 8, "bold"))
        c.create_text(6, Y(y0 + 25), text="volume line", anchor="w", fill="#adadb8", font=("Segoe UI", 7))
        if self.wave is not None and len(self.wave):
            n = min(len(self.wave), 300); idx = np.linspace(0, len(self.wave) - 1, n).astype(int); bw = span / n
            for k, j in enumerate(idx):
                h = max(0.0, min(1.0, (float(self.wave[j]) + 60) / 60)) * (self.LV - 12)
                c.create_rectangle(self.LW + k * bw, Y(y0 + self.LV - 5 - h), self.LW + (k + 1) * bw + 0.5, Y(y0 + self.LV - 5), fill="#3a3a44", outline="")
        ca = self.r.clip_audio; vy = self.vol_y(); vcol = "#10b981"
        c.create_line(self.LW, Y(vy), x1, Y(vy), fill=vcol, width=2, dash=(7, 3))
        c.create_oval(self.LW + 3, Y(vy) - 6, self.LW + 15, Y(vy) + 6, fill=vcol, outline="white")
        c.create_text(self.LW + 22, Y(min(vy + 10, y0 + self.LV - 8) if vy < y0 + 20 else vy - 10), anchor="w", fill=vcol, font=("Segoe UI", 8, "bold"),
                      text="muted" if ca.get("mute") else f"Volume {int(round(float(ca.get('vol', 1.0)) * 100))}%  (drag the green line up / down)")

    def draw(self):
        c, W, dur, Y = self.c, self.W, self.dur(), (lambda y: y - self.yoff)
        c.delete("all"); self.layout()
        c.create_rectangle(0, 0, W, max(self.H, self.total), fill=PANEL, outline="")
        x1 = W - 10; span = x1 - self.LW
        c.create_rectangle(0, Y(0), W, Y(self.RH), fill=BG, outline="")
        c.create_text(6, Y(10), text="Click here to jump", anchor="w", fill="#adadb8", font=("Segoe UI", 8))
        step = 1 if dur <= 20 else 5 if dur <= 90 else 10
        for tt in range(0, int(dur) + 1, step): c.create_text(self.LW + tt / dur * span, Y(self.RH - 6), text=f"{tt}s", fill="#adadb8", font=("Segoe UI", 7))
        for t in self.tracks:
            key, tl = t["key"], t["tl"]
            if key == "level": self.draw_level(t, Y, span); continue
            col = self.COLORS[key]
            for k in range(t["rows"]):
                y = tl.y0 + k * self.LH
                c.create_rectangle(0, Y(y), W, Y(y + self.LH - 1), fill=BG if (k + len(key)) % 2 else PANEL, outline="")
                nm = self.NAMES[key] + (f" {k + 1}" if t["rows"] > 1 else "")
                if key == "caption" and self.r.transcribing: nm += f" {int(self.r.trans_pct * 100)}%"
                c.create_text(6, Y(y + self.LH / 2), text=nm, anchor="w", fill=FG, font=("Segoe UI", 8, "bold"))
            if key == "clip":                                           # red = the part you cut out; grey = what stays
                c.create_rectangle(self.LW, Y(tl.y0 + 3), self.LW + span, Y(tl.y0 + self.LH - 4), fill="#5b1f24", outline="")
                for ca_, cb_ in self.r.cuts():
                    if tl.t2x(cb_) - tl.t2x(ca_) > 34: c.create_text((tl.t2x(ca_) + tl.t2x(cb_)) / 2, Y(tl.y0 + self.LH / 2), text="✂ cut", fill="#ffb4b4", font=("Segoe UI", 8))
            if not t["items"]: c.create_text(self.LW + 8, Y(tl.y0 + self.LH / 2), text=("Whisper is listening..." if (key == "caption" and self.r.transcribing) else self.HINTS[key]), anchor="w", fill="#6b6b78", font=("Segoe UI", 8))
            for i in range(len(t["items"])):
                a, b = tl.get(i); y = tl.y0 + tl.lane_of(i) * self.LH; picked = self.sel == (key, i) or (key, i) in self.multi
                c.create_rectangle(tl.t2x(a), Y(y + 3), tl.t2x(b), Y(y + self.LH - 4), fill=col, outline="white" if picked else (BG if key == "caption" else ""), width=2 if picked else 1)
                lab = {"sfx": "🔊 " + t["items"][i].get("text", ""), "tts": "🗨 " + t["items"][i].get("text", ""), "vfx": t["items"][i].get("kind", ""), "volume": f"{int(round(float(t['items'][i].get('gain', 0.5)) * 100))}%", "clip": ("clip" if abs(float(t["items"][i].get("speed", 1.0)) - 1) < 0.01 else f"clip  {float(t['items'][i]['speed']):g}x"), "caption": t["items"][i].get("text", ""), "blur": "", "text": ("★ " if t["items"][i].get("hook") else "") + t["items"][i].get("text", ""), "overlay": t["items"][i].get("name", "picture"), "music": self.r.music.get("title", "") or "music", "voice": "voice"}[key]
                if key == "caption" and tl.t2x(b) - tl.t2x(a) < 6.5 * len(lab): lab = ""        # word too narrow to label
                if lab: c.create_text(tl.t2x(a) + 5, Y(y + self.LH / 2), text=lab[:30], anchor="w", fill="white", font=("Segoe UI", 8))
                for j, kf in enumerate(t["items"][i].get("kf") or []):                          # keyframe diamonds
                    kx, ky = tl.t2x(kf["t"]), Y(y + self.LH / 2); on = self.sel_kf == (key, i, j)
                    c.create_polygon(kx, ky - 7, kx + 6, ky, kx, ky + 7, kx - 6, ky, fill="#00e5ff" if on else "white", outline="#18181b")
        for sn in [t["tl"].snap_t for t in self.tracks if t["tl"].snap_t is not None] + ([self.scrub_t] if self.scrub_t is not None else []):
            gx_ = self.LW + sn / dur * span; c.create_line(gx_, 0, gx_, max(self.H, self.total), fill="#ffe14d", width=1, dash=(3, 2))
        ct = next((t for t in self.tracks if t["key"] == "clip"), None)
        if ct:                                                          # a line at every cut (white) and at the start of every joined clip (orange)
            tl = ct["tl"]; ya, yb = Y(tl.y0 + 1), Y(tl.y0 + self.LH - 2); segs = sorted(self.r.segs, key=lambda q: q["t0"]); n_join = 0
            for k, sg in enumerate(segs):
                if sg.get("join"): n_join += 1
                if sg["t0"] > 0.05:
                    x = tl.t2x(sg["t0"]); j = bool(sg.get("join"))
                    c.create_line(x, ya, x, yb, fill="#ffb020" if j else "#ffffff", width=3 if j else 2)
                    if j: c.create_text(x + 4, ya + 6, text=f"clip {n_join + 1}", anchor="w", fill="#ffb020", font=("Segoe UI", 7, "bold"))
                if k + 1 < len(segs) and segs[k + 1]["t0"] - sg["t1"] > 0.04:       # a removed part sits after this bar: line at its far edge too
                    x = tl.t2x(sg["t1"]); c.create_line(x, ya, x, yb, fill="#ffffff", width=2)
        try: pos = self.r.clock.pos()
        except Exception: pos = 0.0
        px = self.LW + pos / dur * span
        c.create_line(px, 0, px, max(self.H, self.total), fill="#00e5ff", width=2)
        c.create_polygon(px - 6, 0, px + 6, 0, px, 9, fill="#00e5ff", outline="")
        if self.marq:
            x0, y0, x1, y1 = self.marq; c.create_rectangle(x0, Y(y0), x1, Y(y1), outline="#00e5ff", dash=(4, 3), width=2)


class ExportDialog:
    """The Export pop-up (centre of the screen): tabs with every export setting, joined to a live preview of how the clip will look and sound."""
    TABS = ("File", "Video", "Sound")

    def __init__(self, review):
        r = self.r = review; o = r.opts(); self.vert = bool(o["vertical"])
        self.top = tk.Toplevel(r.top); self.top.title("Export"); self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.pw, self.ph = (252, 448) if self.vert else (560, 315)
        W, H = 460 + self.pw + 60, (690 if self.vert else 640)
        try:
            self.top.geometry(f"{W}x{H}+{max(0, (self.top.winfo_screenwidth() - W) // 2)}+{max(0, (self.top.winfo_screenheight() - H) // 2 - 20)}")
            self.top.transient(r.top); self.top.grab_set()
        except Exception: pass
        r.orig.pump.stop(); r.edit.pump.stop()                    # the pop-up has its own preview; free the CPU
        body = tk.Frame(self.top, bg=BG); body.pack(fill="both", expand=True, padx=14, pady=(14, 6))
        left = tk.Frame(body, bg=PANEL, width=460); left.pack(side="left", fill="y"); left.pack_propagate(False)
        tk.Frame(body, bg=PURPLE, width=3).pack(side="left", fill="y")                         # the joint between settings and preview
        right = tk.Frame(body, bg=PANEL); right.pack(side="left", fill="both", expand=True)
        strip = tk.Frame(left, bg=BG); strip.pack(fill="x")
        self.tabbtn = {}
        for name in self.TABS:
            b = tk.Button(strip, text=name, relief="flat", bg=BG, fg=FG, font=("Segoe UI", 10, "bold"), command=lambda n=name: self.show(n)); b.pack(side="left", ipadx=16, ipady=6)
            self.tabbtn[name] = b
        self.content = tk.Frame(left, bg=PANEL); self.content.pack(fill="both", expand=True, padx=14, pady=10)
        tk.Label(right, text="PREVIEW - how it will look and sound", bg=PANEL, fg="#adadb8", font=("Segoe UI", 9, "bold")).pack(pady=(10, 4))
        self.pane = VideoPane(right, self.pw, self.ph, "", clock=r.clock); self.pane.frame.pack()
        ctl = tk.Frame(right, bg=PANEL); ctl.pack(pady=8)
        Transport(ctl, r.clock, length=max(120, self.pw - 190)).frame.pack(side="left")
        self.snd = tk.Button(ctl, text="", bg=BG, fg=FG, relief="flat", command=self.toggle); self.snd.pack(side="left", padx=8)
        tk.Label(right, text="Quality and size change the file, not this picture. Press space to pause.", bg=PANEL, fg="#adadb8", wraplength=self.pw).pack()
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=14, pady=(4, 14))
        tk.Button(bar, text="🗑 Discard clip", bg=PANEL, fg=RED, relief="flat", command=self.discard).pack(side="left", ipadx=6, ipady=4)
        tk.Button(bar, text="Save raw only", bg=PANEL, fg=FG, relief="flat", command=self.raw).pack(side="left", padx=8, ipady=4)
        tk.Button(bar, text="🔊 Open in player", bg=PANEL, fg=FG, relief="flat", command=r.open_player).pack(side="left", ipady=4)
        tk.Button(bar, text="✔  Export", bg=PURPLE, fg="white", relief="flat", font=("Segoe UI", 12, "bold"), command=self.export).pack(side="right", ipadx=22, ipady=6)
        tk.Button(bar, text="Cancel", bg=PANEL, fg=FG, relief="flat", command=self.close).pack(side="right", padx=8, ipady=4)
        self.top.bind("<space>", r.toggle_pause); self.top.bind("<Escape>", lambda e: self.close())
        self.pane.play(r.item["raw"], r.edit_vf((self.pw, self.ph), o))
        self.show("File"); self.sync()

    def sync(self):
        try:
            self.snd.config(text="🔇 Sound OFF (click to hear)" if self.r.audio.muted else "🔊 Sound ON (click to mute)")
            self.top.after(250, self.sync)
        except Exception: pass

    def toggle(self): self.r.toggle_sound()

    def row(self, text):
        tk.Label(self.content, text=text, bg=PANEL, fg="#adadb8", font=("Segoe UI", 9, "bold")).pack(anchor="w", pady=(10, 2))

    def show(self, name):
        r = self.r
        for n, b in self.tabbtn.items(): b.config(bg=PURPLE if n == name else BG, fg="white" if n == name else FG)
        for w in list(self.content.winfo_children()): w.destroy()
        if name == "File":
            self.row("FILE TYPE"); purple_menu(self.content, r.fmt, *FORMATS).pack(fill="x")
            self.note = tk.Label(self.content, text="", bg=PANEL, fg=FG, wraplength=400, justify="left"); self.note.pack(anchor="w", pady=(4, 0))
            self.row("FILE NAME (blank = automatic)")
            tk.Entry(self.content, textvariable=r.fname, bg=BG, fg=FG, insertbackground=FG, relief="flat").pack(fill="x", ipady=4)
            self.row("SAVES TO")
            self.dest = tk.Label(self.content, text=os.path.abspath(CLIPS_DIR), bg=PANEL, fg=FG, wraplength=400, justify="left"); self.dest.pack(anchor="w")
            tk.Button(self.content, text="Change folder...", bg=BG, fg=FG, relief="flat", command=self.folder).pack(anchor="w", pady=4)
            self.path = tk.Label(self.content, text="", bg=PANEL, fg=PURPLE_HI, wraplength=400, justify="left", font=("Segoe UI", 9, "bold")); self.path.pack(anchor="w", pady=(8, 0))
            for v in (r.fmt, r.fname):
                try: v.trace_add("write", lambda *a: self.refresh_texts())
                except Exception: pass
            self.refresh_texts()
        elif name == "Video":
            self.row("QUALITY"); purple_menu(self.content, r.quality, *QUALITIES).pack(fill="x")
            self.row("SIZE"); purple_menu(self.content, r.resolution, *RESOLUTIONS).pack(fill="x")
            self.dims = tk.Label(self.content, text="", bg=PANEL, fg=FG, wraplength=400, justify="left"); self.dims.pack(anchor="w", pady=(10, 0))
            for v in (r.resolution, r.quality):
                try: v.trace_add("write", lambda *a: self.refresh_texts())
                except Exception: pass
            self.refresh_texts()
        else:
            self.row("PREVIEW SOUND"); tk.Button(self.content, text="Turn the preview sound on / off", bg=BG, fg=FG, relief="flat", command=self.toggle).pack(anchor="w")
            self.row("EDIT THE SOUND")
            for text, cmd in (("🔊 Clip volume & fades...", r.open_clip_audio), ("🎙 Voice focus...", r.open_voice_editor), ("🎚 Music & fades...", r.open_music_editor),
                              ("🎵 Pick a trending sound...", lambda: r.app.open_sounds(r.set_sound))):
                tk.Button(self.content, text=text, bg=BG, fg=FG, relief="flat", anchor="w", command=cmd).pack(fill="x", pady=2)
            m = r.music
            tk.Label(self.content, text=("♪ " + (m.get("title") or "music") + " is mixed in" if music_active(m) else "No music added.") + "   " +
                     (f"Voice focus: {len(r.voice['ranges'])} part(s)." if voice_active(r.voice) else "Voice focus off."),
                     bg=PANEL, fg=FG, wraplength=400, justify="left").pack(anchor="w", pady=(12, 0))

    def refresh_texts(self):
        r = self.r
        try:
            if hasattr(self, "note"): self.note.config(text=FORMAT_NOTES.get(FORMATS.get(r.fmt.get(), ".mp4"), ""))
            if hasattr(self, "path"): self.path.config(text="Will save as:  " + r.export_path().replace(os.path.dirname(r.item["raw"]), os.path.abspath(CLIPS_DIR)))
            if hasattr(self, "dims"):
                SW, SH = r.size; small = RESOLUTIONS.get(r.resolution.get()) == "720"
                dims = (f"{720 if small else 1080} x {1280 if small else 1920}") if self.vert else (f"{int(720 * SW / SH) // 2 * 2} x 720" if small else f"{SW} x {SH}")
                self.dims.config(text=f"Output picture: {dims}.  Smaller size and 'Small file' make files that upload faster.")
        except Exception: pass

    def folder(self):
        self.r.change_dest()
        try: self.dest.config(text=os.path.abspath(CLIPS_DIR))
        except Exception: pass

    def close(self, restart=True):
        try: self.pane.destroy(); self.top.destroy()
        except Exception: pass
        self.r.export_dlg = None
        if restart and not self.r.closed: self.r.restart_panes()

    def export(self): self.close(restart=False); self.r.process()
    def discard(self): self.close(restart=False); self.r.discard()
    def raw(self): self.close(restart=False); self.r.save_raw()


class BlurEditor:
    """Drag a rectangle on the clip to blur that spot (donation alerts, usernames, private info...). The video plays with the blur
    already applied. Boxes are in the ORIGINAL frame, so they also hide the spot in the vertical version."""
    PW, PH = 640, 360

    def __init__(self, app, review):
        self.app, self.review, self.drag = app, review, None
        self.top = tk.Toplevel(app.root); self.top.title("Blur box"); self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(self.top, bg=BG, fg="#adadb8", wraplength=640, justify="left",
                 text="Click and drag on the video to draw a box (the video pauses while you draw; press Play to resume). Draw as many boxes as you like. Use the pause button and bar to find the exact frame."
                 ).pack(anchor="w", padx=12, pady=(10, 4))
        self.pane = VideoPane(self.top, self.PW, self.PH, "CLIP WITH BLUR (live)", clock=review.clock); self.pane.frame.pack(padx=12)
        Transport(self.top, review.clock, length=440).frame.pack(padx=12, pady=(4, 0))
        SW, SH = review.size; sc = min(self.PW / SW, self.PH / SH)
        self.dw, self.dh = SW * sc, SH * sc; self.ox, self.oy = (self.PW - self.dw) / 2, (self.PH - self.dh) / 2
        c = self.pane.canvas
        c.bind("<ButtonPress-1>", self.down); c.bind("<B1-Motion>", self.move); c.bind("<ButtonRelease-1>", self.up)
        tk.Label(self.top, text="TIMELINE - each box has its own bar. Drag a bar's ends to choose when the blur is on; drag its middle to move it. Cyan line = now playing.",
                 bg=BG, fg="#adadb8", wraplength=640, justify="left").pack(anchor="w", padx=12, pady=(8, 2))
        self.tl_canvas = tk.Canvas(self.top, width=self.PW, height=60, bg=PANEL, highlightthickness=0); self.tl_canvas.pack(padx=12)
        self.tl = Timeline(self.tl_canvas, review.item["dur"], review.blurs, lanes=True, width=self.PW, on_change=self.tl_changed, color="#ff3b3b")
        self.tl.bind()
        self.msg = tk.Label(self.top, text="", bg=BG, fg=FG, wraplength=640, justify="left"); self.msg.pack(anchor="w", padx=12, pady=(6, 0))
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="Undo last box", bg=PANEL, fg=FG, relief="flat", command=self.undo).pack(side="left")
        tk.Button(bar, text="Clear all", bg=PANEL, fg=FG, relief="flat", command=self.clear).pack(side="left", padx=8)
        tk.Button(bar, text="Remember for this stream", bg=PANEL, fg=FG, relief="flat", command=self.remember).pack(side="left")
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.close).pack(side="right", ipadx=10)
        self.restart(); self.outline()

    def restart(self):
        r = self.review
        self.pane.play(r.item["raw"], blur_prefix(r.blurs, r.size) + fit_vf(self.PW, self.PH))
        self.msg.config(text=f"{len(r.blurs)} blur box(es).")

    def to_frac(self, px, py):
        return (max(0.0, min(1.0, (px - self.ox) / self.dw)), max(0.0, min(1.0, (py - self.oy) / self.dh)))

    def down(self, e):
        self.review.clock.pause()                      # drawing on a moving picture is hard: pause when you start a box (press Play to resume)
        self.drag = (e.x, e.y, e.x, e.y)
    def move(self, e):
        if self.drag: self.drag = (self.drag[0], self.drag[1], e.x, e.y)

    def up(self, e):
        if not self.drag: return
        x0, y0 = self.to_frac(self.drag[0], self.drag[1]); x1, y1 = self.to_frac(e.x, e.y); self.drag = None
        x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
        if w < 0.01 or h < 0.01: return self.msg.config(text="That box was too small - drag a bigger rectangle.")
        self.review.blurs.append(dict(x=x, y=y, w=w, h=h)); self.tl.sel = len(self.review.blurs) - 1; self.changed()

    def undo(self):
        if self.review.blurs: self.review.blurs.pop(); self.changed()

    def clear(self):
        self.review.blurs.clear(); self.changed()

    def changed(self):
        self.restart(); self.review.schedule_refresh()

    def tl_changed(self):
        self.restart(); self.review.schedule_refresh()

    def outline(self):
        """Red outlines on top of the video (the video image is created lazily, so lift the outlines every time)."""
        try:
            c = self.pane.canvas; c.delete("bb")
            for b in self.review.blurs:
                c.create_rectangle(self.ox + b["x"] * self.dw, self.oy + b["y"] * self.dh, self.ox + (b["x"] + b["w"]) * self.dw,
                                   self.oy + (b["y"] + b["h"]) * self.dh, outline="#ff3b3b", width=2, tags="bb")
            if self.drag:
                c.create_rectangle(*self.drag, outline=RED, dash=(4, 3), width=2, tags="bb")
            c.tag_raise("bb")
            self.tl.items = self.review.blurs
            self.tl.draw(playhead=self.review.clock.pos())
            self.top.after(100, self.outline)
        except Exception:
            pass                                             # window was closed

    def remember(self):
        self.app.remember_blurs(self.review.item.get("channel", ""), self.review.blurs)
        self.msg.config(text="Saved. New clips from this stream will start with these blur boxes.")

    def close(self):
        self.pane.destroy(); self.top.destroy(); self.review.blur_editor = None


class MusicEditor:
    """Mix a song (or your own audio file) into the clip: volume, where it starts/ends, fade in/out, lowering it under speech."""
    W = 640

    def __init__(self, app, review):
        self.app, self.review, self._muted = app, review, True
        d = float(review.item["dur"]); self.dur = d; m = review.music
        self.top = tk.Toplevel(app.root); self.top.title("Music & fades"); self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.title = tk.Label(self.top, text="", bg=BG, fg=FG, font=("Segoe UI", 10, "bold"), wraplength=self.W, justify="left"); self.title.pack(anchor="w", padx=12, pady=(10, 0))
        tk.Label(self.top, bg=BG, fg="#adadb8", wraplength=self.W, justify="left",
                 text="Pick a trending sound (30-second sample) or use your own audio file. Commercial music can get a post muted or claimed."
                 ).pack(anchor="w", padx=12, pady=(0, 6))
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12)
        tk.Button(bar, text="Use my own audio file...", bg=PURPLE, fg="white", relief="flat", command=self.pick_file).pack(side="left")
        tk.Button(bar, text="Remove music", bg=PANEL, fg=FG, relief="flat", command=self.remove).pack(side="left", padx=8)
        tk.Button(bar, text="🎵 Pick a trending sound...", bg=PANEL, fg=FG, relief="flat", command=lambda: self.app.open_sounds(self.review.set_sound)).pack(side="left")
        self.on = tk.BooleanVar(value=m["on"]); self.duck = tk.BooleanVar(value=m["duck"]); self.loop = tk.BooleanVar(value=m["loop"])
        cb = tk.Frame(self.top, bg=BG); cb.pack(fill="x", padx=12, pady=(6, 0))
        for v, t in ((self.on, "Music ON"), (self.duck, "Lower the music while someone talks"), (self.loop, "Loop the music if it's shorter than the clip")):
            tk.Checkbutton(cb, text=t, variable=v, bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(side="left", padx=(0, 12))
        self.v = {}; self.scales = {}
        def sliders(parent, rows):
            for key, text, lo, hi, res, val in rows:
                f = tk.Frame(parent, bg=PANEL); f.pack(fill="x", padx=8, pady=1)
                tk.Label(f, text=text, width=24, anchor="w", bg=PANEL, fg=FG).pack(side="left")
                self.v[key] = tk.DoubleVar(value=val)
                self.scales[key] = tk.Scale(f, from_=lo, to=hi, resolution=res, orient="horizontal", variable=self.v[key], length=360, bg=PANEL, fg=FG,
                                            troughcolor=BG, highlightthickness=0, activebackground=PURPLE)
                self.scales[key].pack(side="left")
        pn = tk.Frame(self.top, bg=PANEL); pn.pack(fill="x", padx=12, pady=8)
        sliders(pn, (("vol", "Music volume %", 0, 100, 1, m["vol"] * 100), ("orig", "Clip's own sound %", 0, 150, 1, m["orig"] * 100)))
        tm = Collapsible(self.top, "When it plays + fades", bg=BG, pack=dict(fill="x", padx=12)); tp = tk.Frame(tm.body, bg=PANEL); tp.pack(fill="x")
        sliders(tp, (("at", "Music starts at (s)", 0, d, 0.1, m["at"]), ("until", "Music ends at (s)", 0, d, 0.1, m["until"]),
                     ("fin", "Music fade IN (s)", 0, min(10, d), 0.1, m["fin"]), ("fout", "Music fade OUT (s)", 0, min(10, d), 0.1, m["fout"]),
                     ("min", "Whole sound fade IN (s)", 0, min(10, d), 0.1, m["min"]), ("mout", "Whole sound fade OUT (s)", 0, min(10, d), 0.1, m["mout"])))
        mo = Collapsible(self.top, "More music options (speed, start point, bass, effects)", bg=BG, pack=dict(fill="x", padx=12, pady=(2, 0))); mp_ = tk.Frame(mo.body, bg=PANEL); mp_.pack(fill="x")
        fr = tk.Frame(mp_, bg=PANEL); fr.pack(fill="x", padx=8, pady=3)
        tk.Label(fr, text="Sound effect", width=24, anchor="w", bg=PANEL, fg=FG).pack(side="left")
        self.fx = tk.StringVar(value=m.get("fx", "None")); purple_menu(fr, self.fx, *MUSIC_FX).pack(side="left")
        sliders(mp_, (("ss", "Start the song from (s)", 0, max(30, float(m.get("ss", 0))), 0.5, m.get("ss", 0.0)),
                      ("speed", "Speed (1 = normal)", 0.5, 2.0, 0.05, m.get("speed", 1.0)),
                      ("bass", "Bass (dB)", -10, 12, 1, m.get("bass", 0.0)), ("treble", "Treble (dB)", -10, 12, 1, m.get("treble", 0.0)),
                      ("duck_amt", "How much to lower it when someone talks %", 0, 100, 5, m.get("duck_amt", 0.55) * 100)))
        tk.Label(self.top, text="MUSIC VOLUME OVER TIME (purple = the music; fades show as ramps)", bg=BG, fg="#adadb8", font=("Segoe UI", 8, "bold")).pack(anchor="w", padx=12)
        self.env = tk.Canvas(self.top, width=self.W, height=70, bg=PANEL, highlightthickness=0); self.env.pack(padx=12, pady=(2, 6))
        self.msg = tk.Label(self.top, text="", bg=BG, fg=FG, wraplength=self.W, justify="left"); self.msg.pack(anchor="w", padx=12)
        b2 = tk.Frame(self.top, bg=BG); b2.pack(fill="x", padx=12, pady=10)
        tk.Button(b2, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.close).pack(side="right", ipadx=10)
        for var in (self.on, self.duck, self.loop, self.fx, *self.v.values()): var.trace_add("write", lambda *a: self.changed())
        self._muted = False; self.load()

    def load(self):
        """Refresh the widgets from review.music (e.g. after a song was picked)."""
        m = self.review.music; self._muted = True
        self.on.set(m["on"]); self.duck.set(m["duck"]); self.loop.set(m["loop"])
        for k, sc in (("vol", 100), ("orig", 100)): self.v[k].set(round(m[k] * sc))
        for k in ("at", "until", "fin", "fout", "min", "mout"): self.v[k].set(m[k])
        self.fx.set(m.get("fx", "None") if m.get("fx") in MUSIC_FX else "None")
        for k, dflt in (("speed", 1.0), ("bass", 0.0), ("treble", 0.0), ("ss", 0.0)): self.v[k].set(m.get(k, dflt))
        self.v["duck_amt"].set(round(m.get("duck_amt", 0.55) * 100))
        try:
            ln = duration(m["path"]) if m.get("path") else 30.0
            self.scales["ss"].config(to=max(1.0, ln - 1))
        except Exception: pass
        self._muted = False; self.draw()
        self.title.config(text=(f"♪ {m['title']}" if m.get("path") else "No music yet - pick a trending sound or use your own audio file."))

    def changed(self):
        if self._muted: return
        m = self.review.music
        m.update(on=bool(self.on.get()) and bool(m.get("path")), duck=bool(self.duck.get()), loop=bool(self.loop.get()),
                 vol=self.v["vol"].get() / 100, orig=self.v["orig"].get() / 100, at=self.v["at"].get(), until=max(self.v["until"].get(), self.v["at"].get() + 0.3),
                 fin=self.v["fin"].get(), fout=self.v["fout"].get(), min=self.v["min"].get(), mout=self.v["mout"].get(),
                 fx=self.fx.get(), ss=self.v["ss"].get(), speed=self.v["speed"].get(), bass=self.v["bass"].get(), treble=self.v["treble"].get(),
                 duck_amt=self.v["duck_amt"].get() / 100)
        self.draw(); self.review.music_changed()

    def draw(self):
        c, W, H, d, m = self.env, self.W, 70, self.dur, self.review.music
        c.delete("all"); c.create_rectangle(0, 0, W, H, fill=PANEL, outline="")
        x = lambda t: t / d * W
        a, u = m["at"], max(m["until"], m["at"] + 0.3); fi, fo = min(m["fin"], u - a), min(m["fout"], u - a); top, bot = 8, H - 8
        pts = [(x(a), bot), (x(a + fi), top), (x(u - fo), top), (x(u), bot)]
        c.create_polygon(*[v for pt in pts for v in pt], fill=PURPLE, outline="")
        c.create_line(0, bot, W, bot, fill="#3a3a44")
        mi, mo = m["min"], m["mout"]
        if mi > 0.01: c.create_line(x(0), top - 4, x(mi), top - 4, fill=RED, width=3)
        if mo > 0.01: c.create_line(x(d - mo), top - 4, x(d), top - 4, fill=RED, width=3)
        for tt in range(0, int(d) + 1, 1 if d <= 20 else 5 if d <= 90 else 10): c.create_text(x(tt), H - 2, text=f"{tt}s", fill="#adadb8", font=("Segoe UI", 7))

    def pick_file(self):
        pth = filedialog.askopenfilename(title="Pick an audio file", filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"), ("All", "*.*")])
        if not pth: return
        try: ln = duration(pth)
        except Exception: return self.msg.config(text="Couldn't read that audio file.")
        m = self.review.music; m.update(path=pth, title=os.path.basename(pth), on=True, ss=0.0, loop=ln < self.dur, until=self.dur)
        self.load(); self.review.music_changed()
        self.msg.config(text=f"Loaded {os.path.basename(pth)} ({ln:.0f} s).")

    def remove(self):
        m = self.review.music; m.update(on=False, path=None, title=""); self.load(); self.review.music_changed()

    def close(self):
        self.top.destroy(); self.review.music_editor = None


class ClipFitDialog:
    """Size and position of one clip inside the frame (you can also just click the clip on the preview and drag it)."""
    def __init__(self, review, i):
        self.r, self.i = review, i; sg = review.segs[i]
        self.top = tk.Toplevel(review.top); self.top.title("Clip size & position"); self.top.configure(bg=BG)
        tk.Label(self.top, text="Click the clip on the preview to drag it around or pull its corners - or set it here.", bg=BG, fg="#adadb8", wraplength=360, justify="left").pack(anchor="w", padx=12, pady=(12, 6))
        row = tk.Frame(self.top, bg=BG); row.pack(fill="x", padx=12)
        tk.Button(row, text="Fit (whole clip, bars if needed)", bg=PANEL, fg=FG, relief="flat", command=self.fit).pack(side="left")
        tk.Button(row, text="Fill the frame (crops)", bg=PANEL, fg=FG, relief="flat", command=self.fill).pack(side="left", padx=6)
        f = tk.Frame(self.top, bg=PANEL); f.pack(fill="x", padx=12, pady=8); self.v = {}
        for key, text, lo, hi, res, val in (("s", "Size %", 20, 400, 5, float(sg.get("s", 1.0)) * 100), ("px", "Left / right", -100, 100, 1, float(sg.get("px", 0.0)) * 100),
                                            ("py", "Up / down", -100, 100, 1, float(sg.get("py", 0.0)) * 100)):
            r = tk.Frame(f, bg=PANEL); r.pack(fill="x", padx=8, pady=2); tk.Label(r, text=text, bg=PANEL, fg=FG, width=11, anchor="w").pack(side="left")
            self.v[key] = tk.DoubleVar(value=val)
            tk.Scale(r, from_=lo, to=hi, resolution=res, orient="horizontal", variable=self.v[key], length=240, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE).pack(side="left")
            self.v[key].trace_add("write", lambda *a: self.apply())
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.top.destroy).pack(side="right", ipadx=10)

    def apply(self):
        try: sg = self.r.segs[self.i]
        except IndexError: return
        sg.update(s=round(float(self.v["s"].get()) / 100.0, 3), px=round(float(self.v["px"].get()) / 100.0, 3), py=round(float(self.v["py"].get()) / 100.0, 3))
        self.r.fit_changed()

    def setv(self, s, px=0.0, py=0.0):
        self.v["s"].set(round(s * 100)); self.v["px"].set(px * 100); self.v["py"].set(py * 100)

    def fit(self): self.setv(1.0)

    def fill(self):
        sg = self.r.segs[self.i]; cw, ch = float(sg.get("cw", 1.0)), float(sg.get("ch", 1.0))
        self.setv(max(1.0 / max(0.05, cw), 1.0 / max(0.05, ch)))


class SpeedDialog:
    """Speed of one clip bar: pick a preset or type a number, then press Apply (the clip is re-made at that speed; Ctrl+Z undoes it)."""
    PRESETS = (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0)

    def __init__(self, review, i):
        self.r, self.i = review, i; cur = float(review.segs[i].get("speed", 1.0))
        self.top = tk.Toplevel(review.top); self.top.title("Clip speed"); self.top.configure(bg=BG)
        tk.Label(self.top, text="How fast should this part of the clip play?", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        row = tk.Frame(self.top, bg=BG); row.pack(fill="x", padx=12)
        self.v = tk.DoubleVar(value=cur)
        for p in self.PRESETS:
            tk.Button(row, text=f"{p:g}x", bg=PURPLE if abs(p - cur) < 0.01 else PANEL, fg="white" if abs(p - cur) < 0.01 else FG, relief="flat", width=5,
                      command=lambda p=p: self.v.set(p)).pack(side="left", padx=2)
        f = tk.Frame(self.top, bg=PANEL); f.pack(fill="x", padx=12, pady=8)
        tk.Label(f, text="Speed", bg=PANEL, fg=FG, width=8, anchor="w").pack(side="left", padx=6)
        tk.Scale(f, from_=0.1, to=4.0, resolution=0.05, orient="horizontal", variable=self.v, length=300, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE).pack(side="left")
        tk.Label(self.top, text="Under 1x = slow motion, over 1x = sped up. The sound keeps its pitch.\nCaptions, text, pictures and music move with it.", bg=BG, fg="#adadb8", justify="left").pack(anchor="w", padx=12)
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="Cancel", bg=PANEL, fg=FG, relief="flat", command=self.top.destroy).pack(side="left")
        tk.Button(bar, text="Apply", bg=PURPLE, fg="white", relief="flat", command=self.apply).pack(side="right", ipadx=14)

    def apply(self):
        t = float(self.v.get()); self.top.destroy()
        if self.i < len(self.r.segs): self.r.retime(self.i, t)


class OverlayDialog:
    """Fade a picture / GIF / video in and out (and make it see-through)."""
    def __init__(self, review, i):
        self.r, self.i = review, i; it = review.overlays[i]
        self.top = tk.Toplevel(review.top); self.top.title("Picture options"); self.top.configure(bg=BG)
        tk.Label(self.top, text=os.path.basename(str(it.get("path", ""))) or "Picture", bg=BG, fg=FG, font=("Segoe UI", 10, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        f = tk.Frame(self.top, bg=PANEL); f.pack(fill="x", padx=12, pady=6); self.v = {}
        for key, text, lo, hi, res, val in (("fin", "Fade in (s)", 0, 5, 0.1, it.get("fin", 0.0)), ("fout", "Fade out (s)", 0, 5, 0.1, it.get("fout", 0.0)),
                                            ("opacity", "Opacity %", 10, 100, 5, float(it.get("opacity", 1.0)) * 100),
                                            ("speed", "Speed (GIF / video)", 0.25, 4, 0.05, float(it.get("speed", 1.0)))):
            r = tk.Frame(f, bg=PANEL); r.pack(fill="x", padx=8, pady=2); tk.Label(r, text=text, bg=PANEL, fg=FG, width=12, anchor="w").pack(side="left")
            self.v[key] = tk.DoubleVar(value=val)
            tk.Scale(r, from_=lo, to=hi, resolution=res, orient="horizontal", variable=self.v[key], length=240, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE).pack(side="left")
            self.v[key].trace_add("write", lambda *a: self.apply())
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.top.destroy).pack(side="right", ipadx=10)

    def apply(self):
        try: it = self.r.overlays[self.i]
        except IndexError: return
        it.update(fin=round(float(self.v["fin"].get()), 2), fout=round(float(self.v["fout"].get()), 2), opacity=round(float(self.v["opacity"].get()) / 100.0, 2),
                  speed=round(float(self.v["speed"].get()), 2))
        self.r.overlays_changed()


class VoiceFxDialog:
    """Pick the voice (chipmunk / deep ...) and how softly it fades in and out for one part of the clip."""
    def __init__(self, review, i):
        self.r, self.i = review, i; it = review.clip_audio["fx"][i]
        self.top = tk.Toplevel(review.top); self.top.title("Voice effect"); self.top.configure(bg=BG)
        tk.Label(self.top, text="Voice for this part", bg=BG, fg=FG).pack(anchor="w", padx=12, pady=(12, 2))
        self.kind = tk.StringVar(value=it.get("kind", "Chipmunk")); purple_menu(self.top, self.kind, *VOICE_FX).pack(fill="x", padx=12)
        f = tk.Frame(self.top, bg=PANEL); f.pack(fill="x", padx=12, pady=10)
        tk.Label(f, text="Fade in/out (s)", bg=PANEL, fg=FG, width=14, anchor="w").pack(side="left", padx=6)
        self.fade = tk.DoubleVar(value=float(it.get("fade", 0.25)))
        tk.Scale(f, from_=0, to=2, resolution=0.05, orient="horizontal", variable=self.fade, length=220, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE).pack(side="left")
        tk.Label(self.top, text="The voice blends in and out of the normal sound over this many seconds. 0 = instant.", bg=BG, fg="#adadb8", wraplength=330, justify="left").pack(anchor="w", padx=12)
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="Remove", bg=PANEL, fg=RED, relief="flat", command=self.remove).pack(side="left")
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.top.destroy).pack(side="right", ipadx=10)
        for v in (self.kind, self.fade): v.trace_add("write", lambda *a: self.apply())

    def apply(self):
        try: it = self.r.clip_audio["fx"][self.i]
        except IndexError: return
        it.update(kind=self.kind.get(), fade=round(float(self.fade.get()), 2)); self.r.audio_changed()

    def remove(self):
        try: self.r.clip_audio["fx"].pop(self.i)
        except IndexError: pass
        self.r.audio_changed(); self.r.tt.refresh(); self.top.destroy()


class SfxWindow:
    """Soundboard: search MyInstants, click a button to hear it, press ＋ to drop it on the clip at the playhead."""
    COLS = 3

    def __init__(self, app, review):
        self.app, self.r, self.player = app, review, AudioPlayer(); self.q = tk.StringVar(); self.page = 1; self.term = ""; self.busy = False
        self.top = tk.Toplevel(review.top); self.top.title("Sound effects"); self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(self.top, text="SOUND EFFECTS  (from myinstants.com)", bg=BG, fg=FG, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=12, pady=(10, 4))
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12)
        e = tk.Entry(bar, width=30, textvariable=self.q, bg=PANEL, fg=FG, insertbackground=FG, relief="flat"); e.pack(side="left", ipady=4)
        try: e.bind("<Return>", lambda ev: self.search(self.q.get()))
        except Exception: pass
        tk.Button(bar, text="Search", bg=PURPLE, fg="white", relief="flat", command=lambda: self.search(self.q.get())).pack(side="left", padx=6)
        chips = tk.Frame(self.top, bg=BG); chips.pack(fill="x", padx=12, pady=6)
        for c in MYI_CHIPS: tk.Button(chips, text=c, bg=PANEL, fg=FG, relief="flat", padx=6, command=lambda c=c: self.search("" if c == "Trending" else c)).pack(side="left", padx=2)
        self.grid = tk.Frame(self.top, bg=BG); self.grid.pack(fill="x", padx=12, pady=4)
        nav = tk.Frame(self.top, bg=BG); nav.pack(fill="x", padx=12)
        tk.Button(nav, text="◀ Back", bg=PANEL, fg=FG, relief="flat", command=lambda: self.go(-1)).pack(side="left")
        tk.Button(nav, text="More ▶", bg=PANEL, fg=FG, relief="flat", command=lambda: self.go(1)).pack(side="left", padx=6)
        self.status = tk.Label(self.top, text="", bg=BG, fg=FG, wraplength=640, justify="left"); self.status.pack(anchor="w", padx=12, pady=(8, 0))
        tk.Label(self.top, text="Click a button to hear it. Press ＋ to put it on the clip where the playhead is. These are community-uploaded sounds - many are fine for memes, "
                                "but some come from copyrighted shows, games or songs, so use them at your own risk on monetized posts.", bg=BG, fg="#adadb8", wraplength=640, justify="left").pack(anchor="w", padx=12, pady=(2, 12))
        self.search("")

    def search(self, term):
        self.term, self.page = (term or "").strip(), 1; self.load()

    def go(self, d):
        if self.page + d >= 1: self.page += d; self.load()

    def load(self):
        if self.busy: return
        self.busy = True; term, page = self.term, self.page; self.status.config(text="Loading sounds...")
        def work():
            try: res, err = myinstants_search(term, page), ""
            except Exception as ex: res, err = None, str(ex)[:120]
            def fin():
                self.busy = False
                if res is None: return self.status.config(text=f"Couldn't reach MyInstants ({err}). Check your internet and try again.")
                self.fill(res[:24]); self.status.config(text="" if res else "Nothing found - try another word.")
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def fill(self, items):
        for c in list(self.grid.winfo_children()):
            try: c.destroy()
            except Exception: pass
        for n, it in enumerate(items):
            cell = tk.Frame(self.grid, bg=PANEL); cell.grid(row=n // self.COLS, column=n % self.COLS, padx=3, pady=3, sticky="nsew")
            tk.Button(cell, text="▶ " + it["title"], width=22, wraplength=150, height=2, bg=PURPLE, fg="white", relief="flat", command=lambda it=it: self.hear(it)).pack(side="left", fill="both", expand=True)
            tk.Button(cell, text="＋", width=3, bg=BG, fg=FG, relief="flat", command=lambda it=it: self.add(it)).pack(side="left", fill="y")

    def _get(self, it, then):
        self.status.config(text=f"Getting {it['title']}...")
        def work():
            try: wav, err = fetch_sfx(it), ""
            except Exception as ex: wav, err = None, str(ex)[:120]
            def fin():
                if not wav: return self.status.config(text=f"Couldn't get that sound ({err}).")
                then(wav)
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def hear(self, it):
        def play(wav):
            try: self.player.play_wav(wav); self.status.config(text=f"▶ {it['title']}")
            except Exception as ex: self.status.config(text=f"Couldn't play audio on this computer: {str(ex)[:100]}")
        self._get(it, play)

    def add(self, it):
        def put(wav): self.r.add_sfx(it["title"], wav); self.status.config(text=f"✔ Added '{it['title']}' to the clip.")
        self._get(it, put)

    def close(self):
        self.player.stop()
        try: self.r.sfx_win = None
        except Exception: pass
        self.top.destroy()


class TTSDialog:
    """Type something, pick a voice, and it is spoken at the playhead (a bar on the Speech row). Add as many as you like, each with its own voice."""
    def __init__(self, review, i=None):
        self.r, self.i = review, i; it = review.clip_audio.setdefault("tts", [])[i] if i is not None else {}
        self.top = tk.Toplevel(review.top); self.top.title("Text to speech"); self.top.configure(bg=BG); self.busy = False; self.voices = []
        tk.Label(self.top, text="What should it say?", bg=BG, fg=FG).pack(anchor="w", padx=12, pady=(12, 2))
        self.text = tk.StringVar(value=it.get("text", "")); line_text(self.top, self.text, width=46, height=4).pack(padx=12)
        f = tk.Frame(self.top, bg=BG); f.pack(fill="x", padx=12, pady=(8, 0)); tk.Label(f, text="Voice", bg=BG, fg=FG, width=8, anchor="w").pack(side="left")
        self.voice = tk.StringVar(value="Loading voices..."); self.vmenu = purple_menu(f, self.voice, "Loading voices..."); self.vmenu.pack(side="left")
        f = tk.Frame(self.top, bg=BG); f.pack(fill="x", padx=12, pady=(4, 0)); tk.Label(f, text="Style", bg=BG, fg=FG, width=8, anchor="w").pack(side="left")
        self.style = tk.StringVar(value=it.get("style", "Normal") if it.get("style") in TTS_STYLES else "Normal"); purple_menu(f, self.style, *TTS_STYLES).pack(side="left")
        tk.Label(f, text="(changes the pitch - gives you more voices)", bg=BG, fg="#adadb8").pack(side="left", padx=8)
        box = tk.Frame(self.top, bg=PANEL); box.pack(fill="x", padx=12, pady=8)
        def scale(label, var, lo, hi, res):
            g = tk.Frame(box, bg=PANEL); g.pack(fill="x", padx=8, pady=1); tk.Label(g, text=label, bg=PANEL, fg=FG, width=10, anchor="w").pack(side="left")
            tk.Scale(g, from_=lo, to=hi, resolution=res, orient="horizontal", variable=var, length=240, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0, activebackground=PURPLE).pack(side="left")
        self.speed = tk.DoubleVar(value=round(float(it.get("speed", 1.0)) * 100)); scale("Speed %", self.speed, 60, 180, 5)
        self.vol = tk.DoubleVar(value=round(float(it.get("vol", 1.0)) * 100)); scale("Volume %", self.vol, 20, 250, 5)
        self.duck = tk.BooleanVar(value=bool(it.get("duck", True)))
        tk.Checkbutton(box, text="Turn the clip's own sound down while it speaks", variable=self.duck, bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG).pack(anchor="w", padx=8, pady=(0, 6))
        self.msg = tk.Label(self.top, text="", bg=BG, fg=YELLOW, wraplength=420, justify="left"); self.msg.pack(anchor="w", padx=12)
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        if i is not None: tk.Button(bar, text="Remove", bg=PANEL, fg=RED, relief="flat", command=self.remove).pack(side="left")
        tk.Button(bar, text="▶ Hear it", bg=PANEL, fg=FG, relief="flat", command=self.hear).pack(side="left", padx=(0 if i is None else 8, 0))
        tk.Button(bar, text="Update" if i is not None else "Add to the clip", bg=PURPLE, fg="white", relief="flat", command=self.go).pack(side="right", ipadx=10)
        tk.Button(bar, text="Close", bg=PANEL, fg=FG, relief="flat", command=self.close).pack(side="right", padx=8)
        self.top.protocol("WM_DELETE_WINDOW", self.close)
        self.want = it.get("vid")
        def work():
            v = tts_voices()
            self.r.app.q.put(("call", lambda: self.loaded(v)))
        threading.Thread(target=work, daemon=True).start()

    def loaded(self, voices):
        self.voices = voices
        if not voices:
            self.voice.set("(no voices found)")
            return self.msg.config(text=("No text-to-speech voices were found on this computer. On Windows they come with the system; on Linux install espeak-ng; "
                                         "or run  pip install edge-tts  for online voices."))
        labels = [l for l, _ in voices]
        try:
            m = self.vmenu["menu"]; m.delete(0, "end")
            for l in labels: m.add_command(label=l, command=lambda x=l: self.voice.set(x))
        except Exception: pass
        self.voice.set(next((l for l, k in voices if k == self.want), labels[0]))

    def key(self): return next((k for l, k in self.voices if l == self.voice.get()), None)

    def close(self):
        try: self.player.stop()
        except Exception: pass
        self.top.destroy()

    def synth(self, out, done):
        if self.busy: return
        key = self.key(); txt = self.text.get().strip()
        if not key: return self.msg.config(text="Pick a voice first.")
        if not txt: return self.msg.config(text="Type something to say first.")
        self.busy = True; self.msg.config(text="Making the voice...")
        st, sp = self.style.get(), float(self.speed.get()) / 100.0
        def work():
            try: n, err = tts_make(txt, key, st, sp, out, self.r.app.log), ""
            except Exception as e: n, err = None, str(e)[:160]
            def fin():
                self.busy = False
                if n is None: return self.msg.config(text=f"Couldn't make that voice: {err}")
                self.msg.config(text=""); done(n, key)
            self.r.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def hear(self):
        out = os.path.join(tempfile.gettempdir(), f"clipstudio_tts_hear_{os.getpid()}.wav")
        def done(n, key):
            try:
                if not hasattr(self, "player"): self.player = AudioPlayer()
                self.player.play_wav(out); self.msg.config(text="▶ Playing...")
            except Exception as ex: self.msg.config(text=f"Couldn't play it on this computer ({str(ex)[:90]}). Press Add to the clip and play the clip instead.")
        self.synth(out, done)

    def go(self):
        r = self.r; d = float(r.item["dur"]); lst = r.clip_audio.setdefault("tts", [])
        out = os.path.join(tempfile.gettempdir(), f"clipstudio_tts_{os.getpid()}_{int(time.time() * 1000)}.wav")
        def done(n, key):
            base = dict(text=self.text.get().strip(), vid=key, style=self.style.get(), speed=round(float(self.speed.get()) / 100.0, 2), vol=round(float(self.vol.get()) / 100.0, 2), duck=bool(self.duck.get()), wav=out)
            if self.i is not None and self.i < len(lst):
                t0 = float(lst[self.i].get("t0", 0.0)); lst[self.i].update(base, t1=round(min(d, t0 + n), 2))
            else:
                t0 = round(min(max(0.0, d - 0.5), max(0.0, r.clock.pos())), 2); lst.append(dict(base, t0=t0, t1=round(min(d, t0 + n), 2)))
            r.audio_changed(); r.tt.refresh(); r.say("Speech added on the Speech row. Drag it to move it; double-click it to change the words or the voice.")
            self.top.destroy()
        self.synth(out, done)

    def remove(self):
        try: self.r.clip_audio["tts"].pop(self.i)
        except (IndexError, KeyError): pass
        self.r.audio_changed(); self.r.tt.refresh(); self.top.destroy()


class ClipAudioEditor:
    """Adjust the clip's own sound: volume, mute, fade in/out, even-out loudness. (Louder/quieter parts live on the timeline: + Volume part.)"""
    def __init__(self, app, review):
        self.app, self.r = app, review; c = review.clip_audio
        self.top = tk.Toplevel(app.root); self.top.title("Clip volume & fades"); self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(self.top, bg=BG, fg="#adadb8", wraplength=460, justify="left",
                 text="Turn the whole clip's own sound up or down. For just one stretch, use ＋ Add ▾ → Volume part on the timeline."
                 ).pack(anchor="w", padx=12, pady=(10, 6))
        self.mute = tk.BooleanVar(value=c["mute"]); self.norm = tk.BooleanVar(value=c["norm"])
        for v, t in ((self.mute, "Mute the clip's sound"), (self.norm, "Even out loudness (quiet parts up, loud parts down)")):
            tk.Checkbutton(self.top, text=t, variable=v, bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG, command=self.changed).pack(anchor="w", padx=12)
        self.vars = {}
        pn = tk.Frame(self.top, bg=PANEL); pn.pack(fill="x", padx=12, pady=10)
        fadebox = Collapsible(self.top, "Fades", bg=BG, pack=False)
        for key, label, lo, hi, res, val in (("vol", "Volume %", 0, 300, 1, c["vol"] * 100), ("fin", "Fade in (s)", 0, 5, 0.1, c["fin"]), ("fout", "Fade out (s)", 0, 5, 0.1, c["fout"])):
            f = tk.Frame(pn if key == "vol" else fadebox.body, bg=PANEL); f.pack(fill="x", padx=8, pady=3)
            tk.Label(f, text=label, width=12, anchor="w", bg=PANEL, fg=FG).pack(side="left")
            v = tk.DoubleVar(value=val); self.vars[key] = v
            tk.Scale(f, from_=lo, to=hi, resolution=res, orient="horizontal", variable=v, length=300, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0,
                     activebackground=PURPLE, command=lambda x: self.changed()).pack(side="left", fill="x", expand=True)
        fadebox.box.pack(fill="x", padx=12, pady=(0, 6))
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=(0, 10))
        tk.Button(bar, text="Reset", bg=PANEL, fg=FG, relief="flat", command=self.reset).pack(side="left")
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.close).pack(side="right", ipadx=10)

    def changed(self):
        c = self.r.clip_audio
        c.update(mute=bool(self.mute.get()), norm=bool(self.norm.get()), vol=float(self.vars["vol"].get()) / 100.0,
                 fin=float(self.vars["fin"].get()), fout=float(self.vars["fout"].get()))
        self.r.audio_changed()

    def reset(self):
        self.mute.set(False); self.norm.set(False); self.vars["vol"].set(100); self.vars["fin"].set(0); self.vars["fout"].set(0); self.changed()

    def close(self):
        self.top.destroy(); self.r.clip_editor = None


class VoiceEditor:
    """Voice focus: make the streamer's voice stand out over the game, only on the parts of the clip you choose."""
    W = 640

    def __init__(self, app, review):
        self.app, self.review = app, review; v = review.voice; self._muted = True
        self.top = tk.Toplevel(app.root); self.top.title("Voice focus"); self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.close)
        tk.Label(self.top, bg=BG, fg="#adadb8", wraplength=self.W, justify="left",
                 text="Makes the streamer easier to hear over the game. Drag on the bar to pick the parts, or press Whole clip. To take a bit out, put the playhead there, press ✂ Cut here, then Delete selected."
                 ).pack(anchor="w", padx=12, pady=(10, 4))
        row = tk.Frame(self.top, bg=BG); row.pack(fill="x", padx=12)
        self.on = tk.BooleanVar(value=v["on"]); self.strength = tk.StringVar(value=v["strength"]); self.ai = tk.BooleanVar(value=v["ai"])
        tk.Checkbutton(row, text="Voice focus ON", variable=self.on, bg=BG, fg=YELLOW, selectcolor=PANEL, activebackground=BG, activeforeground=YELLOW).pack(side="left")
        tk.Label(row, text="  strength", bg=BG, fg=FG).pack(side="left"); purple_menu(row, self.strength, *VOICE_STRENGTHS).pack(side="left", padx=4)
        ok = has_demucs()
        tk.Checkbutton(row, text="AI separation (Demucs)" + ("" if ok else " - not installed"), variable=self.ai, state="normal" if ok else "disabled",
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG).pack(side="left", padx=10)
        self.canvas = tk.Canvas(self.top, width=self.W, height=90, bg=PANEL, highlightthickness=0); self.canvas.pack(padx=12, pady=8)
        try: wave = loudness_curve(review.item["raw"])
        except Exception: wave = None
        self.wave = wave
        self.tl = Timeline(self.canvas, review.item["dur"], v["ranges"], lanes=False, wave=wave, width=self.W, on_change=self.changed)
        self.tl.bind(); self.tl.draw()
        self.msg = tk.Label(self.top, text="", bg=BG, fg=FG, wraplength=self.W, justify="left"); self.msg.pack(anchor="w", padx=12)
        bar = tk.Frame(self.top, bg=BG); bar.pack(fill="x", padx=12, pady=10)
        tk.Button(bar, text="Whole clip", bg=PANEL, fg=FG, relief="flat", command=self.whole).pack(side="left")
        tk.Button(bar, text="Suggest loud parts", bg=PANEL, fg=FG, relief="flat", command=self.suggest).pack(side="left", padx=6)
        tk.Button(bar, text="✂ Cut here", bg=PURPLE, fg="white", relief="flat", command=self.cut).pack(side="left", padx=(6, 0))
        tk.Button(bar, text="Delete selected", bg=PANEL, fg=FG, relief="flat", command=self.tl.delete_selected).pack(side="left", padx=6)
        tk.Button(bar, text="Clear", bg=PANEL, fg=FG, relief="flat", command=self.clear).pack(side="left", padx=6)
        tk.Button(bar, text="Done", bg=PURPLE, fg="white", relief="flat", command=self.close).pack(side="right", ipadx=10)
        for var in (self.on, self.strength, self.ai): var.trace_add("write", lambda *a: self.changed())
        self._muted = False; self.changed(); self.follow()

    def follow(self):
        """Keep a cyan line on the strip at the review window's playhead, so you can see where ✂ Cut here will cut."""
        try:
            if self.review.voice_editor is not self: return
            self.tl.draw(self.review.clock.pos()); self.top.after(150, self.follow)
        except Exception: pass

    def cut(self):
        """Like cutting a clip: split the voice-focus part at the playhead. The right-hand piece is selected - press Delete selected to cut it out of voice focus."""
        pos = float(self.review.clock.pos()); rs = self.review.voice["ranges"]
        i = self.tl.sel if self.tl.sel is not None and 0 <= self.tl.sel < len(rs) and rs[self.tl.sel]["t0"] + 0.1 < pos < rs[self.tl.sel]["t1"] - 0.1 else \
            next((k for k, r in enumerate(rs) if r["t0"] + 0.1 < pos < r["t1"] - 0.1), None)
        if i is None: return self.msg.config(text="Move the playhead (in the review window) onto a voice-focus part first, then press ✂ Cut here.")
        a, b = rs[i]["t0"], rs[i]["t1"]
        rs[i]["t1"] = round(pos, 2); rs.insert(i + 1, dict(t0=round(pos, 2), t1=b)); self.tl.sel = i + 1
        self.changed(); self.msg.config(text=f"Cut at {pos:.1f}s. The right-hand piece is selected - press Delete selected to take it out of voice focus (or drag its ends to trim).")

    def changed(self):
        if self._muted: return
        v = self.review.voice; v.update(on=bool(self.on.get()), strength=self.strength.get(), ai=bool(self.ai.get()))
        self.tl.draw(self.review.clock.pos()); n = len(v["ranges"])
        self.msg.config(text=("No parts chosen yet - drag on the timeline, or press Whole clip / Suggest loud parts." if not n else
                              f"{n} part(s) selected." + ("" if v["on"] else "  (Voice focus is OFF)")))
        self.review.voice_changed()

    def whole(self):
        self.review.voice["ranges"][:] = [dict(t0=0.0, t1=round(float(self.review.item["dur"]), 2))]; self.on.set(True); self.changed()

    def suggest(self):
        if self.wave is None or not len(self.wave): return self.msg.config(text="Couldn't read this clip's loudness.")
        found = suggest_loud_parts(self.wave)
        if not found: return self.msg.config(text="Nothing stands out as much louder than the rest of the clip.")
        self.review.voice["ranges"][:] = [dict(t0=round(a, 2), t1=round(b, 2)) for a, b in found]; self.on.set(True)
        self.changed(); self.msg.config(text=f"Found {len(found)} loud part(s). Loud could be the game OR the streamer shouting - adjust the bars to taste.")

    def clear(self):
        self.review.voice["ranges"][:] = []; self.changed()

    def close(self):
        self.top.destroy(); self.review.voice_editor = None


class ReviewWindow:
    """Opens after a clip is cut: watch the original and a LIVE edited preview, pick options, then decide.
    Nothing is sent to the AI (no credits) unless AI is ticked and you press Process & Save."""
    PW, PH, VW, VH = 480, 270, 270, 480

    def __init__(self, app, item, waiting):
        self.app, self.item, self.words = app, item, None
        self.base_words, self.style, self.editor, self.sound = None, dict(app.caption_style), None, None
        self.cam, self.gx, self.detecting, self.cam_editor, self.size = None, None, False, None, (1920, 1080)
        self.blurs, self.blur_editor = list(app.recall_blurs(item.get("channel", ""))), None
        self.voice = dict(on=False, strength="Medium", ai=False, ranges=[]); self.voice_editor = None; self._aafter = None; self._agen = 0
        self.music = dict(on=False, path=None, title="", vol=0.4, orig=1.0, at=0.0, until=float(item["dur"]), fin=1.0, fout=1.5,
                          min=0.0, mout=0.0, duck=True, loop=True, ss=0.0, speed=1.0, bass=0.0, treble=0.0, fx="None", duck_amt=0.55); self.music_editor = None
        try: self.size = video_size(item['raw'])
        except Exception: pass
        self.busy = self.decided = self.transcribing = False
        self._after = None; self.final = None
        self.top = tk.Toplevel(app.root)
        self.top.title(f"Review clip {item['stamp']}  (chat score {item['score']:.1f})" + (f"  -  {waiting} more waiting" if waiting else ""))
        self.top.configure(bg=BG); self.top.protocol("WM_DELETE_WINDOW", self.on_close)
        self.top.geometry("1500x940"); self.top.minsize(1000, 640)
        self.texts, self.draw_mode, self._drag, self._lay_after, self._pv_size = [], None, None, None, (0, 0)
        self.overlays = []
        self._drag_pane = "orig"; self.clipboard = None; self._stage = None
        self.splicing, self._ver, self._raw_gen, self.versions, self.hist, self._seen, self._seen_t = False, 0, 0, [item["raw"]], [], None, 0.0
        self.PW, self.PH, self.VW, self.VH = 480, 270, 270, 480
        self.clock = PlayClock(dur=item["dur"], paused=True); self.autostart, self.autostart_t, self.closed, self._ast = True, time.time(), False, None
        self.vertical, self.captions, self.ai = tk.BooleanVar(value=app.vert.get()), tk.BooleanVar(value=app.cap.get()), tk.BooleanVar(value=app.ai.get())
        self.layout = tk.StringVar(value=next((k for k, v in LAYOUTS.items() if v == app.layout_value()), "Blurred background"))
        self.credit, self.hook = tk.StringVar(value=app.credit.get().strip()), tk.StringVar(value="")
        self.hook_blur, self.hook_len = tk.BooleanVar(value=False), tk.DoubleVar(value=3.0); self.cam_on = tk.BooleanVar(value=True); self.cam_linked = tk.BooleanVar(value=True); self.borders = tk.BooleanVar(value=True)
        self.quality, self.resolution, self.fname = tk.StringVar(value=list(QUALITIES)[1]), tk.StringVar(value=list(RESOLUTIONS)[0]), tk.StringVar(value="")
        self.clip_audio = dict(vol=1.0, mute=False, fin=0.0, fout=0.0, norm=False, ranges=[], fx=[], tts=[], sfx=[]); self.clip_editor = None
        self.segs = [dict(t0=0.0, t1=round(float(item["dur"]), 2))]          # the parts of the clip that are kept (gaps between them are cut out)
        self.fmt, self.export_dlg, self.trans_pct = tk.StringVar(value=list(FORMATS)[0]), None, 0.0

        # the whole window: [ Tools | previews | Export ] on top, a draggable divider (the purple bar), the Timeline below
        pw = tk.PanedWindow(self.top, orient="vertical", sashwidth=12, sashrelief="raised", bg=PURPLE, bd=0, opaqueresize=True); pw.pack(fill="both", expand=True)
        main = tk.Frame(pw, bg=BG, height=620); main.grid_propagate(False)
        main.grid_columnconfigure(1, weight=1); main.grid_rowconfigure(0, weight=1)
        tools_box = tk.Frame(main, bg=PANEL, width=300); tools_box.grid(row=0, column=0, sticky="ns", padx=(8, 4), pady=8); tools_box.pack_propagate(False)
        tcv = tk.Canvas(tools_box, bg=PANEL, highlightthickness=0); tsb = purple_scrollbar(tools_box, tcv.yview); tcv.configure(yscrollcommand=tsb.set)
        tsb.pack(side="right", fill="y"); tcv.pack(side="left", fill="both", expand=True)
        tools = tk.Frame(tcv, bg=PANEL); tcv.create_window((0, 0), window=tools, anchor="nw", width=272)
        tools.bind("<Configure>", lambda e: tcv.configure(scrollregion=tcv.bbox("all")))
        tcv.bind("<Enter>", lambda e: tcv.bind_all("<MouseWheel>", lambda ev: tcv.yview_scroll(-1 if ev.delta > 0 else 1, "units")))
        tcv.bind("<Leave>", lambda e: tcv.unbind_all("<MouseWheel>"))
        self.pv = tk.Frame(main, bg=BG); self.pv.grid(row=0, column=1, sticky="nsew"); self.pv.pack_propagate(False)
        tlp = tk.Frame(pw, bg=PANEL, height=300)
        pw.add(main, stretch="always", minsize=330); pw.add(tlp, stretch="never", minsize=150)

        # ---- previews (centre) ----
        bar = self.pv_bar = tk.Frame(self.pv, bg=BG); bar.pack(fill="x", padx=12, pady=(8, 0))
        self.export_btn = tk.Button(bar, text="⬆  Export...", bg=PURPLE, fg="white", relief="flat", font=("Segoe UI", 11, "bold"), command=self.open_export)
        self.export_btn.pack(side="right", ipadx=14, ipady=4)
        self.btn_player = tk.Button(bar, text="🔊 Open in player", bg=PANEL, fg=FG, relief="flat", command=self.open_player); self.btn_player.pack(side="right", padx=8, ipady=4)
        tk.Label(bar, text="Space = pause / play", bg=BG, fg="#adadb8").pack(side="left")
        row = tk.Frame(self.pv, bg=BG); row.pack(pady=(8, 0))
        self.orig = VideoPane(row, self.PW, self.PH, "ORIGINAL (silent)", clock=self.clock); self.orig.frame.pack(side="left", padx=8, anchor="n")
        self.edit = VideoPane(row, self.VW, self.VH, "EDITED PREVIEW (live)", clock=self.clock); self.edit.frame.pack(side="left", padx=8, anchor="n")
        self.audio = ClipAudio(muted=load_settings().get("preview_muted", True)); self.edit.attach_audio(self.audio)
        self.audio.prepare(item["raw"])
        self.show_orig = tk.BooleanVar(value=bool(load_settings().get("show_orig", True)))
        self.sync_panes = [self.orig, self.edit] if self.show_orig.get() else [self.edit]
        if not self.show_orig.get(): self.orig.frame.pack_forget()
        ctl = self.pv_ctl = tk.Frame(self.pv, bg=BG); ctl.pack(pady=(6, 0))
        self.transport = Transport(ctl, self.clock, length=360, on_change=lambda: None); self.transport.frame.pack(side="left")
        self.replay_btn = tk.Button(ctl, text="↻ Replay", bg=PANEL, fg=FG, relief="flat", command=self.replay); self.replay_btn.pack(side="left", padx=(10, 4))
        self.mute_btn = tk.Button(ctl, text="", bg=PANEL, fg=FG, relief="flat", command=self.toggle_sound); self.mute_btn.pack(side="left", padx=4)
        self.buttons = [self.replay_btn, self.export_btn, self.btn_player]
        self.status = tk.Label(self.pv, text="Watch the clip first. Captions use free local Whisper. No AI credits are spent unless you tick AI and press Process & Save.",
                               bg=BG, fg=FG, wraplength=640, justify="left", height=2, anchor="nw"); self.status.pack(anchor="w", padx=12, pady=(6, 0))
        self.pv.bind("<Configure>", self.on_pv_configure)
        for pane, name in ((self.orig, "orig"), (self.edit, "edit")):            # you can draw a blur box on either picture
            for ev, fn in (("<ButtonPress-1>", self.pv_down), ("<B1-Motion>", self.pv_move), ("<ButtonRelease-1>", self.pv_up)):
                pane.canvas.bind(ev, lambda e, fn=fn, name=name: fn(e, name))

        # ---- TOOLS (left) ----
        def head(parent, t): tk.Label(parent, text=t, bg=PANEL, fg="#adadb8", font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=10, pady=(10, 2))
        def cb(var, text, parent=tools): return tk.Checkbutton(parent, text=text, variable=var, bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG, anchor="w", justify="left", wraplength=260)
        def btn(text, cmd, parent=tools): return tk.Button(parent, text=text, bg=BG, fg=FG, relief="flat", anchor="w", command=cmd)
        tk.Label(tools, text="TOOLS", bg=PANEL, fg=FG, font=("Segoe UI", 11, "bold")).pack(anchor="w", padx=10, pady=(10, 4))
        # the two things most people touch are always visible; everything else is one click away inside a folded section
        cb(self.vertical, "Vertical 9:16").pack(anchor="w", padx=8)
        purple_menu(tools, self.layout, *LAYOUTS).pack(fill="x", padx=10, pady=2)
        cb(self.captions, "Captions").pack(anchor="w", padx=8)
        btn("Customize captions...", self.open_editor).pack(fill="x", padx=10, pady=2)
        btn("＋ Join another clip onto this one...", self.add_clips).pack(fill="x", padx=10, pady=(8, 2))
        def sect(title):
            c = Collapsible(tools, title, bg=PANEL, fg=FG, pack=dict(fill="x", padx=6, pady=(6, 0))); return c.body
        t = sect("Facecam")
        btn("Adjust facecam...", self.open_cam_editor, t).pack(fill="x", padx=4, pady=2)
        qf = tk.Frame(t, bg=PANEL); qf.pack(fill="x", padx=4, pady=2); tk.Label(qf, text="Quality", bg=PANEL, fg=FG).pack(side="left")
        self.camq = tk.StringVar(value=next((k for k, v in CAM_QUALITY.items() if v == int(load_settings().get("camq", 1))), "Enhanced (sharper)"))
        purple_menu(qf, self.camq, *CAM_QUALITY).pack(side="left", padx=6)
        self.camq.trace_add("write", lambda *a: (save_settings(camq=CAM_QUALITY.get(self.camq.get(), 0)), self.schedule_refresh()))
        cb(self.cam_on, "Facecam on for the selected clip", t).pack(anchor="w")
        cb(self.cam_linked, "Same facecam box as the other clips (untick = this clip has its own)", t).pack(anchor="w")
        self.cam_linked.trace_add("write", lambda *a: self.cam_linked_toggled())
        self.cam_on.trace_add("write", lambda *a: self.cam_toggled())
        cb(self.show_orig, "Show the original preview", t).pack(anchor="w")
        self.show_orig.trace_add("write", lambda *a: self.toggle_orig())
        t = sect("Text, hook and pictures")
        for text, var in (("Hook", self.hook), ("Credit", self.credit)):
            f = tk.Frame(t, bg=PANEL); f.pack(fill="x", padx=4, pady=1)
            tk.Label(f, text=text, width=14, anchor="w", bg=PANEL, fg=FG).pack(side="left")
            (line_text(f, var, 16, 2, bg=BG) if text == "Hook" else tk.Entry(f, width=16, textvariable=var, bg=BG, fg=FG, insertbackground=FG, relief="flat")).pack(side="left", fill="x", expand=True, ipady=3)
        hf = tk.Frame(t, bg=PANEL); hf.pack(fill="x", padx=4, pady=1)
        tk.Label(hf, text="Hook length (s)", width=14, anchor="w", bg=PANEL, fg=FG).pack(side="left")
        tk.Scale(hf, from_=0.5, to=15, resolution=0.5, orient="horizontal", variable=self.hook_len, length=120, bg=PANEL, fg=FG, troughcolor=BG, highlightthickness=0,
                 activebackground=PURPLE, command=lambda v: self.hook_len_typed()).pack(side="left", fill="x", expand=True)
        cb(self.hook_blur, "Blur the background behind the hook (fades out)", t).pack(anchor="w")
        self.hook_blur.trace_add("write", lambda *a: self.hook_blur_typed())
        btn("★ Hook look...", self.open_hook_settings, t).pack(fill="x", padx=4, pady=2)
        btn("Aa Add text...", self.add_text, t).pack(fill="x", padx=4, pady=2)
        btn("↺ Reset caption position (drag captions on the preview)", self.reset_cap_pos, t).pack(fill="x", padx=4, pady=2)
        btn("▦ Add blur box (draw on a preview)", self.arm_blur, t).pack(fill="x", padx=4, pady=2)
        btn("✨ Picture fade in/out...", self.overlay_options, t).pack(fill="x", padx=4, pady=2)
        btn("🖼 Add picture / GIF / video...", self.add_overlay_dialog, t).pack(fill="x", padx=4, pady=2)
        self.blur_lbl = tk.Label(t, text="", bg=PANEL, fg=YELLOW); self.blur_lbl.pack(anchor="w", padx=4)
        t = sect("Sound")
        btn("🎙 Voice focus...", self.open_voice_editor, t).pack(fill="x", padx=4, pady=2)
        self.voice_lbl = tk.Label(t, text="", bg=PANEL, fg=YELLOW); self.voice_lbl.pack(anchor="w", padx=4)
        btn("💥 Sound effects (MyInstants)...", self.open_sfx, t).pack(fill="x", padx=4, pady=2)
        btn("🗨 Text to speech...", self.open_tts, t).pack(fill="x", padx=4, pady=2)
        btn("🐿 Chipmunk voice part", lambda: self.add_voice_fx("Chipmunk"), t).pack(fill="x", padx=4, pady=2)
        btn("🗣 Deep voice part", lambda: self.add_voice_fx("Deep"), t).pack(fill="x", padx=4, pady=2)
        btn("🔊 Clip volume & fades...", self.open_clip_audio, t).pack(fill="x", padx=4, pady=2)
        btn("🎵 Music & fades...", self.open_music_editor, t).pack(fill="x", padx=4, pady=2)
        btn("🎵 Pick a trending sound...", lambda: self.app.open_sounds(self.set_sound), t).pack(fill="x", padx=4, pady=2)
        self.sound_lbl = tk.Label(t, text="", bg=PANEL, fg=YELLOW, wraplength=270, justify="left"); self.sound_lbl.pack(anchor="w", padx=4)
        t = sect("Video")
        cb(self.borders, "Blurred borders (untick = no borders, fills the frame)", t).pack(anchor="w")
        self.borders.trace_add("write", lambda *a: self.schedule_refresh())
        btn("⏩ Clip speed...", self.clip_speed_dialog, t).pack(fill="x", padx=4, pady=2)
        btn("▢ Clip size & position...", self.clip_size_dialog, t).pack(fill="x", padx=4, pady=2)
        cb(self.ai, "AI trim + title + hook (uses API credits)", t).pack(anchor="w")
        for v in (self.vertical, self.captions, self.layout, self.credit): v.trace_add("write", lambda *a: self.schedule_refresh())
        self.hook.trace_add("write", lambda *a: self.hook_typed())
        self.vertical.trace_add("write", lambda *a: self.on_pv_configure(None, force=True))
        self.layout.trace_add("write", lambda *a: self.ensure_cam())

        # ---- TIMELINE (bottom) ----
        tb = tk.Frame(tlp, bg=PANEL); tb.pack(fill="x", padx=8, pady=(6, 2))
        tk.Label(tb, text="TIMELINE", bg=PANEL, fg=FG, font=("Segoe UI", 11, "bold")).pack(side="left", padx=(2, 10))
        addb = tk.Menubutton(tb, text="＋ Add ▾", bg=PURPLE, fg="white", relief="flat", padx=10, activebackground=PURPLE_HI, activeforeground="white")
        am = tk.Menu(addb, tearoff=0, bg=PURPLE, fg="white", activebackground=PURPLE_HI, activeforeground="white", bd=0)
        for label_, cmd_ in (("Blur box", self.arm_blur), ("Text", self.add_text), ("Picture / GIF / video...", self.add_overlay_dialog),
                             ("Music", self.add_music_menu), ("Voice focus part", self.add_voice_part), ("Sound effect...", self.open_sfx), ("Text to speech...", self.open_tts), ("Chipmunk voice", lambda: self.add_voice_fx("Chipmunk")), ("Deep voice", lambda: self.add_voice_fx("Deep")), ("Volume part", self.add_volume_part)):
            am.add_command(label=label_, command=cmd_)
        addb.config(menu=am); addb.pack(side="left", padx=3)
        tk.Button(tb, text="＋ Join clip", bg=PURPLE, fg="white", relief="flat", command=self.add_clips).pack(side="left", padx=3)
        for text, cmd in (("✂ Cut here", lambda: self.split_selected()), ("◆ Keyframe", self.add_keyframe), ("🗑 Delete", lambda: self.tt.delete_selected())):
            tk.Button(tb, text=text, bg=PURPLE if text[0] in "✂◆" else BG, fg="white" if text[0] in "✂◆" else FG, relief="flat", command=cmd).pack(side="left", padx=3)
        tk.Label(tb, text="Click a bar or caption word, or drag a box around several, then press Delete.", bg=PANEL, fg="#adadb8").pack(side="left", padx=10)
        tk.Button(tb, text="◇ Clear keyframes", bg=PANEL, fg="#adadb8", relief="flat", command=self.clear_keyframes).pack(side="right", padx=3)
        self.tl_canvas = tk.Canvas(tlp, bg=PANEL, highlightthickness=0, takefocus=1); self.tl_canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.tt = TrackTimeline(self.tl_canvas, self)
        self.tl_canvas.bind("<Configure>", lambda e: self.tt.set_size(e.width, e.height))
        self.top.bind("<space>", self.toggle_pause)
        self.set_preview_fps()
        for k, d_, n_ in (("<Left>", -1, 1), ("<Right>", 1, 1), ("<Shift-Left>", -1, 10), ("<Shift-Right>", 1, 10)):
            try: self.top.bind(k, lambda e, d_=d_, n_=n_: (self.step_frame(d_, n_), "break")[1])
            except Exception: pass
        for k in ("<Control-z>", "<Control-Z>"):
            try: self.top.bind(k, self.undo)
            except Exception: pass
        for k, fn in (("<Control-c>", self.do_copy), ("<Control-C>", self.do_copy), ("<Control-v>", self.do_paste), ("<Control-V>", self.do_paste),
                      ("<Control-d>", self.do_dup), ("<Control-D>", self.do_dup), ("<Delete>", self.do_delete), ("<BackSpace>", self.do_delete),
                      ("<Control-x>", self.do_cut), ("<Control-X>", self.do_cut)):
            try: self.top.bind(k, fn)
            except Exception: pass
        self.edit.canvas.bind("<Double-Button-1>", self.edit_dbl)
        for k in ("<Control-b>", "<Control-B>", "<Command-b>"):
            try: self.top.bind(k, self.split_selected)
            except Exception: pass
        self.hist = [self.snapshot()]; self._seen = self.hist[0]["core"]
        for wdg in (self.top, self.tt.c, getattr(self.orig, "canvas", None), getattr(self.edit, "canvas", None), getattr(self, "pv", None)):
            if wdg is not None: enable_drop(wdg, self.on_drop, True)            # drag pictures / GIFs / clips onto the editor
        if item.get("pending_add"): self.top.after(900, lambda: self.add_paths(item.pop("pending_add")))
        self.show_mute(); self.replay(); self.watch(); self.top.after(300, self.tick_ui)
        self.voice_changed = self._wrap_tt(self.voice_changed); self.music_changed = self._wrap_tt(self.music_changed); self.audio_changed = self._wrap_tt(self.audio_changed)

    def _wrap_tt(self, fn):
        def inner(*a, **k):
            fn(*a, **k)
            try: self.tt.refresh()
            except Exception: pass
        return inner

    # ----- helpers -----
    def opts(self):
        lay = LAYOUTS.get(self.layout.get(), "blur")
        return dict(vertical=self.vertical.get(), layout=lay, cam=self.cam, borders=bool(self.borders.get()), gx=self.gx,
                    captions=self.captions.get(), ai=self.ai.get(), credit=self.credit.get().strip(), hook=("" if self.hook_item() else self.hook.get().strip()),
                    style=self.style, blurs=seg_fit_boxes(self.segs) + seg_nocam_boxes(self.segs) + seg_cam_boxes(self.segs) + list(self.blurs), voice=json.loads(json.dumps(self.voice)), music=dict(self.music),
                    texts=[dict(t) for t in self.texts], overlays=json.loads(json.dumps(self.overlays)), camq=CAM_QUALITY.get(self.camq.get(), 0), cuts=self.cuts(), clip_audio=json.loads(json.dumps(self.clip_audio)), crf=QUALITIES.get(self.quality.get(), 20), res=RESOLUTIONS.get(self.resolution.get()))

    def say(self, text):
        self.status.config(text=text)

    def lock(self, on):
        for b in self.buttons: b.config(state="disabled" if on else "normal")

    def show_mute(self):
        self.mute_btn.config(text="🔇 Sound: OFF (click to hear)" if self.audio.muted else "🔊 Sound: ON (click to mute)",
                             fg=FG if self.audio.muted else YELLOW)

    def toggle_sound(self):
        self.audio.muted = not self.audio.muted
        save_settings(preview_muted=self.audio.muted); self.show_mute()           # watch() starts/stops the sound at the right spot

    def schedule_refresh(self):
        if self._after: self.top.after_cancel(self._after)
        self._after = self.top.after(500, self.refresh_preview)

    # ----- live preview -----
    def replay(self):
        """Back to the start: both previews load their first frame, then start at the same moment."""
        self.clock.seek(0, paused=True); self.autostart, self.autostart_t = True, time.time()
        if self.show_orig.get(): self.orig.play(self.item["raw"], fit_vf(self.PW, self.PH))
        self.refresh_preview()

    def toggle_orig(self):
        on = bool(self.show_orig.get())
        try: save_settings(show_orig=on)
        except Exception: pass
        if on:
            self.orig.frame.pack(side="left", padx=8, anchor="n", before=self.edit.frame); self.sync_panes = [self.orig, self.edit]
        else:
            self.orig.frame.pack_forget(); self.orig.pump.stop(); self.sync_panes = [self.edit]
        self._pv_size = (0, 0, 0, 0); self.on_pv_configure(None, force=True)

    def watch(self):
        """Every 0.1 s: start both previews together once both show frame 1, and keep the sound lined up with pause / seek / loop."""
        if self.closed: return
        try:
            c = self.clock
            if self.autostart:
                ok = all(pn.pump.ready or pn.pump.failed for pn in self.sync_panes)
                if ok or time.time() - self.autostart_t > 6: self.autostart = False; c.resume()
            if not c.paused and not self.autostart and not self.final:      # play through the parts you cut out
                pos = c.pos(); dd = float(self.item["dur"])
                for a, b in self.cuts():
                    if a - 0.01 <= pos < b - 0.02: c.seek(min(sg["t0"] for sg in self.segs) if b >= dd - 0.06 else b); break
            c.pos()
            st = (c.gen, c.epoch, c.paused or self.autostart, self.audio.muted, self.audio.wav)
            if st != self._ast:
                self._ast = st; self.audio.stop()
                if not (c.paused or self.autostart) and not self.audio.muted: self.audio.play_from(c.pos())
            self.top.after(100, self.watch)
        except Exception:
            pass

    def edit_vf(self, size, o=None):
        """The filter chain that makes the edited picture (used by the main preview AND the export window's preview)."""
        o = o or self.opts()
        ass = os.path.join(tempfile.gettempdir(), f"clipstudio_review_{os.getpid()}_{id(self)}.ass")
        make_ass(self.words if (o["captions"] and self.words) else [], 0, self.item["dur"], ass, o["vertical"], o["hook"], o["credit"], self.style, self.texts)
        return build_vf(o["vertical"], o["layout"], ass, size, self.cam, self.gx, seg_fit_boxes(self.segs) + seg_nocam_boxes(self.segs) + seg_cam_boxes(self.segs) + list(self.blurs) + hook_blur_boxes(self.texts), self.size, overlays=self.overlays, camq=o.get("camq", 0), borders=o.get("borders", True), dur=float(self.item["dur"]))

    def cuts(self):
        out, cur, d = [], 0.0, float(self.item["dur"])
        for sg in sorted(self.segs, key=lambda x: x["t0"]):
            if sg["t0"] - cur > 0.04: out.append((cur, sg["t0"]))
            cur = max(cur, sg["t1"])
        if d - cur > 0.04: out.append((cur, d))
        return out

    def cuts_changed(self):
        if self.cuts(): return self.splice()
        self.say(f"Clip length {float(self.item['dur']):.1f}s.")

    def splice(self):
        """Parts you removed are cut out for real: the pieces on either side are joined (no gap), and everything on the timeline moves with them."""
        if self.splicing: return
        d = float(self.item["dur"]); kept = kept_segments(self.cuts(), d)
        if not kept or sum(b - a for a, b in kept) < 0.5:
            self.segs[:] = [dict(t0=0.0, t1=round(d, 2))]; return self.say("At least half a second of the clip has to stay.")
        self.splicing = True; self._ver += 1; self.say("Joining the clip back together...")
        old = self.item["raw"]; new = re.sub(r"(_v\d+)?_raw\.mp4$", f"_v{self._ver}_raw.mp4", old) if old.endswith("_raw.mp4") else os.path.splitext(old)[0] + f"_v{self._ver}.mp4"
        pos, mp = self.clock.pos(), cut_mapper(kept)
        def work():
            err = ""
            try: cut_clip(old, kept, new, self.app.log)
            except Exception as ex: err = str(ex)[-150:]
            self.app.q.put(("call", lambda: self.splice_done(new, kept, mp, pos, err)))
        threading.Thread(target=work, daemon=True).start()

    def splice_done(self, new, kept, mp, pos, err):
        if self.closed: return
        if err:
            self.splicing = False; return self.say(f"Couldn't join the clip ({err}). Ctrl+Z puts the removed part back.")
        r = remap_opts(dict(blurs=list(self.blurs), texts=list(self.texts), overlays=list(self.overlays), voice=self.voice, music=self.music, clip_audio=self.clip_audio), mp)
        self.blurs[:] = r["blurs"]; self.texts[:] = r["texts"]; self.overlays[:] = r["overlays"]
        for tgt, src in ((self.voice, r["voice"]), (self.music, r["music"]), (self.clip_audio, r["clip_audio"])): tgt.clear(); tgt.update(src)
        segs, prev = [], None                                   # joined pieces become one bar; split points you made elsewhere stay
        for sg in sorted(self.segs, key=lambda x: x["t0"]):
            a, b = mp(sg["t0"]), mp(sg["t1"])
            if b - a < 0.05: continue
            if segs and prev is not None and sg["t0"] - prev > 0.04: segs[-1]["t1"] = round(b, 2)
            else: segs.append(dict(sg, t0=round(a, 2), t1=round(b, 2)))
            prev = sg["t1"]
        self.segs[:] = segs or [dict(t0=0.0, t1=round(sum(b - a for a, b in kept), 2))]
        if self.transcribing or self.words is None: self.words = self.base_words = None; self.transcribing = False; self._raw_gen += 1
        else:
            fix = lambda ws: [dict(w, s=mp(w["s"]), e=mp(w["e"])) for w in ws if mp(w["e"]) - mp(w["s"]) >= 0.02]
            self.words = fix(self.words); self.base_words = fix(self.base_words) if self.base_words else self.base_words
            if self.editor: self.editor.set_transcript(self.words)
        self.versions.append(new); self.item["raw"] = new; self.item["dur"] = duration(new)
        self.reload_raw(mp(pos)); self.tt.sel = None; self.tt.refresh(); self.voice_changed(); self.music_changed(); self.audio_changed()
        self.splicing = False
        self.say(f"Joined. The clip is now {float(self.item['dur']):.1f}s. Ctrl+Z undoes it.")

    def retime(self, i, target):
        """Play clip bar i at `target` x speed (1 = normal). Makes a new version of the clip, like a cut does; everything on the timeline moves with it."""
        if self.splicing or self.busy: return
        sg = self.segs[i]; cur = float(sg.get("speed", 1.0)); k = max(0.1, min(8.0, float(target))) / cur
        if abs(k - 1.0) < 0.01: return
        d = float(self.item["dur"]); a, b = float(sg["t0"]), float(sg["t1"])
        pieces = [(x, y, f) for x, y, f in ((0.0, a, 1.0), (a, b, k), (b, d, 1.0)) if y - x > 0.02]
        self.splicing = True; self._ver += 1; self.say("Changing the speed...")
        old = self.item["raw"]; new = re.sub(r"(_v\d+)?_raw\.mp4$", f"_v{self._ver}_raw.mp4", old) if old.endswith("_raw.mp4") else os.path.splitext(old)[0] + f"_v{self._ver}.mp4"
        pos, mp = self.clock.pos(), retime_mapper(pieces)
        def work():
            err = ""
            try: retime_clip(old, pieces, new, self.app.log)
            except Exception as ex: err = str(ex)[-150:]
            self.app.q.put(("call", lambda: self.retime_done(new, i, target, mp, pos, err)))
        threading.Thread(target=work, daemon=True).start()

    def retime_done(self, new, i, target, mp, pos, err):
        if self.closed: return
        if err:
            self.splicing = False; return self.say(f"Couldn't change the speed ({err}).")
        r = remap_opts(dict(blurs=list(self.blurs), texts=list(self.texts), overlays=list(self.overlays), voice=self.voice, music=self.music, clip_audio=self.clip_audio), mp)
        self.blurs[:] = r["blurs"]; self.texts[:] = r["texts"]; self.overlays[:] = r["overlays"]
        for tgt, src in ((self.voice, r["voice"]), (self.music, r["music"]), (self.clip_audio, r["clip_audio"])): tgt.clear(); tgt.update(src)
        for n, sg in enumerate(self.segs):
            sg["t0"], sg["t1"] = round(mp(sg["t0"]), 2), round(mp(sg["t1"]), 2)
            if n == i: sg["speed"] = round(float(target), 3)
        if self.transcribing or self.words is None: self.words = self.base_words = None; self.transcribing = False; self._raw_gen += 1
        else:
            fix = lambda ws: [dict(w, s=mp(w["s"]), e=mp(w["e"])) for w in ws if mp(w["e"]) - mp(w["s"]) >= 0.02]
            self.words = fix(self.words); self.base_words = fix(self.base_words) if self.base_words else self.base_words
            if self.editor: self.editor.set_transcript(self.words)
        self.versions.append(new); self.item["raw"] = new; self.item["dur"] = duration(new)
        self.reload_raw(mp(pos)); self.tt.sel = None; self.tt.refresh(); self.voice_changed(); self.music_changed(); self.audio_changed()
        self.splicing = False
        self.say(f"Speed set to {float(target):g}x. The clip is now {float(self.item['dur']):.1f}s. Ctrl+Z undoes it.")

    def clip_speed_dialog(self):
        i = self.clip_at_playhead()
        if i is None: return self.say("Move the playhead onto a clip first.")
        SpeedDialog(self, i)

    def add_clips(self):
        """Pick one or more video files; they're joined onto the end of this clip (one edit, one export). Ctrl+Z takes them back off."""
        if self.busy or self.splicing: return
        w = getattr(self, "join_win", None)
        if w:
            try: return w.top.lift()
            except Exception: pass
        self.join_win = JoinDrop(self)

    def add_paths(self, paths):
        """Shared by the button and by dragging files onto the window."""
        paths = [x for x in (paths or ()) if x and os.path.abspath(x) != os.path.abspath(self.item["raw"])]
        if not paths: return
        if self.busy or self.splicing: return self.say("Wait for the current job to finish, then drop the clip(s) again.")
        self.splicing = True; self._ver += 1; old = self.item["raw"]; size = self.size
        new = re.sub(r"(_v\d+)?_raw\.mp4$", f"_v{self._ver}_raw.mp4", old) if old.endswith("_raw.mp4") else os.path.splitext(old)[0] + f"_v{self._ver}.mp4"
        self.say(f"Adding {len(paths)} clip(s)... (they're matched to this clip's size and sound)"); pos = self.clock.pos()
        def work():
            err, d0, durs = "", 0.0, []
            try:
                d0 = float(self.item["dur"]); durs = [duration(x) for x in paths]
                dims = []
                for x in paths:
                    try:
                        vw, vh = video_size(x); SW, SH = size
                        dims.append((round(min(1.0, SH * vw / vh / SW), 4), round(min(1.0, SW * vh / vw / SH), 4)))
                    except Exception: dims.append((1.0, 1.0))
                self._add_dims = dims
                concat_clips([old] + paths, new, size, video_fps(old), self.app.log)
            except Exception as ex: err = (getattr(ex, "stderr", b"") or b"").decode("utf-8", "ignore")[-150:] if hasattr(ex, "stderr") else str(ex)[-150:]
            self.app.q.put(("call", lambda: self.add_done(new, d0, durs, pos, err)))
        threading.Thread(target=work, daemon=True).start()

    def add_done(self, new, d0, durs, pos, err):
        if self.closed: return
        if err: self.splicing = False; safe_remove(new); return self.say(f"Couldn't add the clip(s): {err or 'ffmpeg failed'}")
        t = d0
        dims = getattr(self, "_add_dims", []) or []
        for k, d in enumerate(durs):
            cw, ch = dims[k] if k < len(dims) else (1.0, 1.0)
            self.segs.append(dict(t0=round(t, 2), t1=round(t + d, 2), join=True, cw=cw, ch=ch)); t += d
        d1 = duration(new); self.versions.append(new); self.item["raw"] = new; self.item["dur"] = d1
        gen = self._raw_gen
        if self.transcribing: self.words = self.base_words = None; self.transcribing = False; self._raw_gen += 1       # Whisper starts over on the longer clip
        elif self.words is not None: self.transcribe_tail(new, d0, d1)
        self.reload_raw(pos); self.tt.sel = None; self.tt.refresh(); self.splicing = False
        self.say(f"Added {len(durs)} clip(s). The clip is now {d1:.1f}s. Ctrl+Z takes them off again.")

    def transcribe_tail(self, path, a, b):
        """Captions for just the newly added part, appended to the ones you already have (your word edits are kept)."""
        gen = self._raw_gen
        def work():
            try: ws = transcribe(path, a, b)
            except Exception: ws = []
            def fin():
                if self.closed or gen != self._raw_gen or self.words is None: return
                self.words = list(self.words) + ws; self.base_words = list(self.base_words or []) + ws; self.tt.refresh()
                if self.editor: self.editor.set_transcript(self.words)
                self.schedule_refresh()
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def set_preview_fps(self):
        self.fps = clip_fps(self.item["raw"])
        for pn in (self.orig, self.edit): pn.pump.fps = int(round(min(30.0, max(10.0, self.fps))))

    def step_frame(self, direction, n=1):
        """Left / Right arrow: pause and move one frame (Shift: ten frames). Snaps to the clip's real frame grid."""
        if self._typing() or self.splicing: return
        self.autostart = False; c = self.clock; fps = float(self.edit.pump.fps or 30)        # the preview's own frame grid, so every press shows a new picture
        c.pause(); pos = c.pos(); k = round(pos * fps - 0.3)                       # the frame that's on screen now
        k = k + direction * n; t = (k + 0.3) / fps                                  # +0.3: land inside that frame, not on its edge
        d = float(self.item["dur"])
        if t < 0: t = 0.0
        if t > d - 0.05: t = d - 0.05
        c.seek(t, paused=True)
        self.say(f"Frame {max(0, round(t * fps))}  ({t:.2f}s of {d:.1f}s) - ← → step one frame, Shift+← → ten frames, Space plays.")

    def reload_raw(self, pos=0.0):
        """The clip file changed (spliced, or undone back): point everything at the new file."""
        raw, d = self.item["raw"], float(self.item["dur"]); self.clock.dur = d
        try: self.size = video_size(raw)
        except Exception: pass
        try: self.tt.wave = loudness_curve(raw)
        except Exception: self.tt.wave = None
        for ed in (self.voice_editor, self.music_editor, self.cam_editor):
            if ed:
                try: ed.close()
                except Exception: pass
        self.clock.seek(max(0.0, min(pos, d - 0.05))); self._ast = None
        self.set_preview_fps()
        self.restart_panes()
        self.refresh_audio()                                    # the preview sound belongs to the OLD file - rebuild it for the new one

    # ----- undo (Ctrl+Z, up to 5 steps back) -----
    def core_state(self):
        return json.dumps(dict(raw=self.item["raw"], dur=round(float(self.item["dur"]), 3), blurs=self.blurs, texts=self.texts, overlays=self.overlays, voice=self.voice,
                               music=self.music, ca=self.clip_audio, segs=self.segs), sort_keys=True, default=str)

    def snapshot(self, core=None):
        cp = lambda x: json.loads(json.dumps(x)) if x else None
        return dict(core=core or self.core_state(), words=cp(self.words), base=cp(self.base_words))

    def hist_tick(self):
        """Every tick: once an edit has settled (and you're not mid-drag), remember the new state. Only the last 5 steps are kept."""
        if self.splicing or self._stage or self._drag or self.tt.active or self.tt.vol_drag or self.tt.scrub: return
        core, now = self.core_state(), time.time()
        if core != self._seen: self._seen, self._seen_t = core, now; return
        if now - self._seen_t >= 0.4 and core != self.hist[-1]["core"]:
            self.hist.append(self.snapshot(core)); del self.hist[:-6]

    def undo(self, e=None):
        if self._typing(): return
        if self.splicing: return "break"
        core = self.core_state()
        if core != self.hist[-1]["core"]: st = self.hist[-1]                      # an edit that hasn't been filed yet
        elif len(self.hist) >= 2: self.hist.pop(); st = self.hist[-1]
        else: self.say("Nothing to undo (Ctrl+Z goes back up to 5 steps)."); return "break"
        self.restore(st); self.say(f"Undid the last change.  {len(self.hist) - 1} more step(s) can be undone.")
        return "break"

    def restore(self, st):
        data = json.loads(st["core"]); pos = self.clock.pos()
        self.blurs[:] = data["blurs"]; self.texts[:] = data["texts"]; self.overlays[:] = data.get("overlays", []); self.segs[:] = data["segs"]
        for tgt, src in ((self.voice, data["voice"]), (self.music, data["music"]), (self.clip_audio, data["ca"])): tgt.clear(); tgt.update(src)
        if data["raw"] != self.item["raw"]:                                         # undoing a splice: the longer clip comes back
            self.item["raw"], self.item["dur"] = data["raw"], data["dur"]
            self.words, self.base_words = st["words"], st["base"]; self.transcribing = False; self._raw_gen += 1
            if self.editor and self.words: self.editor.set_transcript(self.words)
            self.reload_raw(pos)
        self._seen = self.core_state(); self.tt.sel = None; self.tt.sel_kf = None; self.tt.refresh(); self.schedule_refresh()
        self.voice_changed(); self.music_changed(); self.audio_changed(); self.hook_entry_sync()

    def split_selected(self, e=None):
        try:
            w = self.top.focus_get(); cls = w.winfo_class() if w is not None else ""
        except Exception: cls = ""
        if cls in ("Entry", "Text", "TEntry"): return
        self.tt.split_selected(); return "break"

    def toggle_pause(self, e=None):
        try:
            w = self.top.focus_get(); cls = w.winfo_class() if w is not None else ""
        except Exception: cls = ""
        if cls in ("Entry", "Text", "TEntry", "Spinbox"): return            # typing a space in a text box
        self.autostart = False; c = self.clock
        c.resume() if c.paused else c.pause()
        return "break"

    def open_export(self):
        if self.export_dlg: return self.export_dlg.top.lift()
        if self.busy or self.splicing: return
        self.export_dlg = ExportDialog(self)

    def restart_panes(self):
        if self.show_orig.get(): self.orig.play(self.item["raw"], fit_vf(self.PW, self.PH))
        self.refresh_preview()

    def export_path(self):
        ext = FORMATS.get(self.fmt.get(), ".mp4"); raw = self.item["raw"]
        name = re.sub(r'[\\/:*?"<>|]+', "", self.fname.get()).strip().rstrip(".")
        base = (name[:80] if name else os.path.basename(unversion(raw)).replace("_raw.mp4", "_final").replace(".mp4", "")) + ext
        return os.path.join(os.path.dirname(raw), base)

    def refresh_preview(self):
        if self.final or self.busy: return
        o = self.opts()
        if o["captions"] and self.words is None and not self.transcribing: self.start_transcribe()
        size = (self.VW, self.VH) if o["vertical"] else (self.PW, self.PH)
        self.edit.resize(*size); self.edit.play(self.item["raw"], self.edit_vf(size, o))
        self.blur_lbl.config(text=", ".join(x for x in (f"{len(self.blurs)} blur box(es)" if self.blurs else "", f"{len(self.texts)} text(s)" if self.texts else "", f"{len(self.overlays)} overlay(s)" if self.overlays else "") if x))

    def start_transcribe(self):
        self.transcribing = True; gen = self._raw_gen
        self.say("Transcribing on your PC (free). The first run downloads the Whisper model, which can take a few minutes...")
        def work():
            err = ""
            def live(ws, frac):
                def ui():
                    if self.closed or gen != self._raw_gen: return
                    self.words, self.trans_pct = list(ws), frac; self.tt.refresh(); self.schedule_refresh()
                    self.say(f"Whisper is listening... {int(frac * 100)}%  ({len(ws)} words so far, shown live on the timeline)")
                self.app.q.put(("call", ui))
            try: w = transcribe(self.item["raw"], 0, self.item["dur"], on_words=live)
            except CaptionError as e: w, err = [], str(e)
            except Exception as e: w, err = [], f"Transcription failed: {e}"
            def fin():
                if gen != self._raw_gen: return                              # the clip was spliced while Whisper was listening: it starts over on the new clip
                self.transcribing = False; self.base_words = w; self.words = list(w); self.trans_pct = 1.0; self.tt.refresh()
                self.say(err or ("Captions ready. Use Customize captions... to fix words or change the look." if w else "No speech found in this clip."))
                if err: self.app.log(err)
                if self.editor: self.editor.set_transcript(self.words)
                self.refresh_preview()
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    # ----- facecam -----
    def ensure_cam(self):
        """Called when the layout changes: reuse the remembered box for this stream, or look for the facecam."""
        if LAYOUTS.get(self.layout.get()) != "facecam" or self.cam or self.detecting or self.size[0] / max(1, self.size[1]) < 0.75: return
        saved = self.app.recall_cam(self.item.get("channel", ""))
        if saved:
            self.cam = cam_from_fractions(saved["x"], saved["y"], saved["w"], saved["h"], self.item["raw"])
            return self.say("Using the facecam box you saved for this stream. Adjust facecam... to change it.")
        self.redetect()

    def redetect(self):
        if self.detecting: return
        self.detecting = True; self.say("Looking for the streamer's facecam...")
        def work():
            logs = []
            try: cam = detect_facecam(self.item["raw"], log=logs.append)
            except Exception as e: cam = None; logs.append(f"Facecam detection failed: {str(e)[:100]}")
            def fin():
                self.detecting = False
                if cam:
                    if self.cam: cam.update(motion=self.cam.get("motion", "none"), amt=self.cam.get("amt", 20))      # keep the movement you chose
                    self.cam = cam; self.say(f"Facecam found (face seen in {cam['hits']} of {cam['samples']} frames). Check the preview, or use Adjust facecam... to fine-tune.")
                    if self.cam_editor: self.cam_editor.load(cam)
                else:
                    self.say((logs[-1] if logs else "No facecam found.") + "  Showing the blurred layout until you place the box.")
                self.refresh_preview()
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def open_voice_editor(self):
        if self.voice_editor: return self.voice_editor.top.lift()
        self.voice_editor = VoiceEditor(self.app, self)

    def open_music_editor(self):
        if self.music_editor: return self.music_editor.top.lift()
        self.music_editor = MusicEditor(self.app, self)

    def music_changed(self):
        self.sound_lbl.config(text=(f"♪ {self.music['title']} (mixed in)" if music_active(self.music) else (f"♪ {self.sound['title']}" if self.sound else "")))
        if self._aafter: self.top.after_cancel(self._aafter)
        self._aafter = self.top.after(600, self.refresh_audio)

    def open_clip_audio(self):
        if self.clip_editor: return self.clip_editor.top.lift()
        self.clip_editor = ClipAudioEditor(self.app, self)

    def audio_changed(self):
        if self._aafter: self.top.after_cancel(self._aafter)
        self._aafter = self.top.after(600, self.refresh_audio)

    def add_volume_part(self):
        d = float(self.item["dur"]); t0 = round(min(d - 0.5, max(0.0, self.clock.pos())), 2)
        self.clip_audio["ranges"].append(dict(t0=t0, t1=round(min(d, t0 + 3.0), 2), gain=0.5)); self.audio_changed()
        self.say("Volume part added (starts at 50%). Drag its ends to fit; double-click it to set the exact volume (0 = silent, 200 = twice as loud).")

    def add_voice_fx(self, kind="Chipmunk"):
        d = float(self.item["dur"]); t0 = round(min(d - 0.5, max(0.0, self.clock.pos())), 2)
        self.clip_audio.setdefault("fx", []).append(dict(t0=t0, t1=round(min(d, t0 + 3.0), 2), kind=kind, fade=0.25)); self.audio_changed(); self.tt.refresh()
        self.say(f"{kind} voice added on the Voice effect row. Drag its ends to fit; double-click it to change the voice or the fade.")

    def edit_voice_fx(self, i):
        VoiceFxDialog(self, i)

    def open_sfx(self):
        w = getattr(self, "sfx_win", None)
        if w:
            try: return w.top.lift()
            except Exception: pass
        self.sfx_win = SfxWindow(self.app, self)

    def add_sfx(self, title, wav):
        """Drop a sound effect on the Sound effect row at the playhead."""
        try: n = duration(wav)
        except Exception: n = 1.5
        d = float(self.item["dur"]); t0 = round(min(max(0.0, d - 0.3), max(0.0, self.clock.pos())), 2)
        self.clip_audio.setdefault("sfx", []).append(dict(text=title, wav=wav, vol=1.0, duck=False, t0=t0, t1=round(min(d, t0 + n), 2)))
        self.audio_changed(); self.tt.refresh(); self.say(f"Added '{title}' at {t0:g}s. Drag it to move it; double-click it to change its volume.")

    def edit_sfx(self, i):
        it = self.clip_audio["sfx"][i]
        v = simpledialog.askfloat("Sound effect volume", f"Volume % for '{it.get('text', 'sound')}' (100 = as is, 200 = twice as loud):", initialvalue=round(float(it.get("vol", 1.0)) * 100), minvalue=0, maxvalue=400, parent=self.top)
        if v is not None: it["vol"] = v / 100.0; self.audio_changed()

    def open_tts(self, i=None):
        TTSDialog(self, i if isinstance(i, int) else None)

    def edit_volume_part(self, i):
        r = self.clip_audio["ranges"][i]
        v = simpledialog.askfloat("Volume for this part", "Volume % for this stretch (0 = silent, 100 = unchanged, 200 = twice as loud):",
                                  initialvalue=round(float(r.get("gain", 0.5)) * 100), minvalue=0, maxvalue=400, parent=self.top)
        if v is not None: r["gain"] = v / 100.0; self.audio_changed()

    def voice_changed(self):
        self.voice_lbl.config(text=(f"voice focus on ({len(self.voice['ranges'])} part(s))" if voice_active(self.voice) else ""))
        if self._aafter: self.top.after_cancel(self._aafter)
        self._aafter = self.top.after(600, self.refresh_audio)

    def refresh_audio(self):
        """Rebuild the preview sound with the current voice-focus settings (in the background; the cleaned-up Demucs result is cached)."""
        if self.final: return
        self._agen += 1; gen = self._agen; v = json.loads(json.dumps(self.voice)); mu = dict(self.music); ca = json.loads(json.dumps(self.clip_audio))
        if audio_active(v, mu, ca): self.say("Updating the preview sound..." + (" (AI separation can take a minute the first time)" if v.get("ai") else ""))
        def work():
            ok = self.audio.prepare(self.item["raw"], v, self.app.log, mu, ca)
            def fin():
                if gen == self._agen: self.say("Preview sound updated. Turn the sound button ON to hear it." if ok else "Couldn't build the preview sound (see log).")
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    # ----- layout: previews grow and shrink with the window / the purple divider -----
    def on_pv_configure(self, e=None, force=False):
        if self._lay_after:
            try: self.top.after_cancel(self._lay_after)
            except Exception: pass
        self._lay_after = self.top.after(1 if force else 250, self.apply_pv_size)

    def apply_pv_size(self):
        self._lay_after = None
        if self.closed: return
        try:
            over = self.pv_bar.winfo_reqheight() + self.pv_ctl.winfo_reqheight() + self.status.winfo_reqheight() + 64      # everything above/below the pictures
            aw, ah = self.pv.winfo_width() - 40 - (16 if self.show_orig.get() else 0), self.pv.winfo_height() - over
        except Exception: return
        if aw < 200 or ah < 100: return
        vert, both = self.vertical.get() and not self.final, self.show_orig.get()
        wide = (750 if vert else 960) if both else (270 if vert else 480)
        k = min(aw / wide, ah / (480 if vert else 270), 3.0)
        k = max(0.4, k); ev = lambda v: max(2, int(v) // 2 * 2)
        self.PW, self.PH = ev(480 * k), ev(270 * k)
        self.VW, self.VH = (ev(270 * k), ev(480 * k)) if vert else (self.PW, self.PH)
        if (self.PW, self.PH, self.VW, self.VH) == self._pv_size: return
        self._pv_size = (self.PW, self.PH, self.VW, self.VH)
        self.orig.resize(self.PW, self.PH)
        if not self.final and self.show_orig.get(): self.orig.play(self.item["raw"], fit_vf(self.PW, self.PH))
        self.refresh_preview()

    def draw_stage(self):
        """Outlines (and corner handles for the selected one) of the blur boxes and text that are showing right now."""
        sel = self.tt.sel
        for name, pane in (("orig", self.orig), ("edit", self.edit)):
            c = pane.canvas; c.delete("bb")
            if name == "orig" and not self.show_orig.get(): continue
            for key in ("blur", "text", "overlay", "clip"):
                if name == "orig" and key in ("text", "overlay", "clip"): continue
                for i, it in self.active_items(key):
                    picked = sel == (key, i)
                    if key == "clip" and not picked: continue
                    r = self.el_rect(name, key, it)
                    if not r: continue
                    c.create_rectangle(*r, outline="white" if picked else ("#ff3b3b" if key == "blur" else "#f59e0b" if key == "overlay" else "#00e5ff" if key == "clip" else "#a970ff"), width=2 if picked else 1,
                                       dash=() if picked else (4, 3), tags="bb")
                    if picked:
                        for hx, hy in self.corners(r).values(): c.create_rectangle(hx - 4, hy - 4, hx + 4, hy + 4, fill="white", outline=PURPLE, tags="bb")
            cr = self.cap_rect(name)
            if cr: c.create_rectangle(*cr, outline="#ffffff", width=1, dash=(2, 4), tags="bb")
            if self._drag and self._drag_pane == name: c.create_rectangle(*self._drag, outline=RED, dash=(4, 3), width=2, tags="bb")
            c.tag_raise("bb")

    def tick_ui(self):
        """Every 120 ms: outlines on the pictures, and the timeline (with its moving playhead)."""
        if self.closed: return
        try:
            self.draw_stage(); self.tt.draw(); self.hist_tick(); self.sync_cam_box()
            self.top.after(120, self.tick_ui)
        except Exception:
            if not self.closed: self.top.after(500, self.tick_ui)

    # ----- drawing a blur box on the original preview -----
    def arm_blur(self):
        self.draw_mode = True; self.clock.pause()
        try:
            for pane in (self.orig, self.edit): pane.canvas.config(cursor="crosshair")
        except Exception: pass
        self.say("Drag a rectangle on either picture to blur that spot. The video is paused so you can aim; the new box starts at the playhead and lasts to the end - trim it on the timeline.")

    def pv_down(self, e, pane="orig"):
        if self.draw_mode: self._drag, self._drag_pane = (e.x, e.y, e.x, e.y), pane
        else: self.stage_press(e, pane)

    def pv_move(self, e, pane="orig"):
        if self.draw_mode:
            if self._drag and pane == self._drag_pane: self._drag = (self._drag[0], self._drag[1], e.x, e.y)
        else: self.stage_move(e, pane)

    def edit_rect_to_src(self, x0, y0, x1, y1):
        """A rectangle drawn on the EDITED picture -> the same spot on the original frame (as fractions), whatever layout is on."""
        o = self.opts(); W, H = self.edit.pump.w, self.edit.pump.h; SW, SH = self.size
        cl = lambda v, a, b: max(a, min(b, v))
        if not o["vertical"]:
            sc = min(W / SW, H / SH); dw, dh = SW * sc, SH * sc; ox, oy = (W - dw) / 2, (H - dh) / 2
            f = lambda px, py: ((cl(px, ox, ox + dw) - ox) / dw, (cl(py, oy, oy + dh) - oy) / dh)
        elif o["layout"] == "facecam" and self.cam_gx_for(self.clip_at_playhead())[0]:
            cam, egx = self.cam_gx_for(self.clip_at_playhead()); TH = cam_top_h(W, H, cam); GH = H - TH
            if y0 < TH:                                                              # drawn on the facecam strip
                sc = max(W / (cam["w"] * SW), TH / (cam["h"] * SH))
                f = lambda px, py: (cam["x"] + cam["w"] / 2 + (cl(px, 0, W) - W / 2) / (sc * SW), cam["y"] + cam["h"] / 2 + (cl(py, 0, TH) - TH / 2) / (sc * SH))
            else:                                                                    # drawn on the gameplay part
                gx = egx if egx is not None else auto_gx(cam, W, GH); gw = min(SW, SH * W / GH)
                f = lambda px, py: (((SW - gw) * gx + cl(px, 0, W) / W * gw) / SW, (cl(py, TH, H) - TH) / GH)
        elif o["layout"] == "crop":
            cw = min(SW, SH * 9 / 16); f = lambda px, py: (((SW - cw) / 2 + cl(px, 0, W) / W * cw) / SW, cl(py, 0, H) / H)
        else:
            fh = W * SH / SW; fy0 = (H - fh) / 2; f = lambda px, py: (cl(px, 0, W) / W, (cl(py, fy0, fy0 + fh) - fy0) / fh)
        ax, ay = f(x0, y0); bx, by = f(x1, y1)
        return tuple(cl(v, 0.0, 1.0) for v in (min(ax, bx), min(ay, by), abs(bx - ax), abs(by - ay)))

    def pv_up(self, e, pane="orig"):
        if not self.draw_mode: return self.stage_release(e, pane)
        if not self._drag or pane != self._drag_pane: return
        d = self._drag; self._drag = None; self.draw_mode = False
        try:
            for pn in (self.orig, self.edit): pn.canvas.config(cursor="")
        except Exception: pass
        if pane == "orig":
            SW, SH = self.size; sc = min(self.PW / SW, self.PH / SH); dw, dh = SW * sc, SH * sc; ox, oy = (self.PW - dw) / 2, (self.PH - dh) / 2
            fr = lambda px, py: (max(0.0, min(1.0, (px - ox) / dw)), max(0.0, min(1.0, (py - oy) / dh)))
            x0, y0 = fr(d[0], d[1]); x1, y1 = fr(e.x, e.y); x, y, w, h = min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)
        else:
            x, y, w, h = self.edit_rect_to_src(d[0], d[1], e.x, e.y)
        if w < 0.01 or h < 0.01: return self.say("That box was too small - press + Blur box and drag a bigger rectangle.")
        t0 = round(min(float(self.item["dur"]) - 0.5, max(0.0, self.clock.pos())), 2)
        self.blurs.append(dict(x=x, y=y, w=w, h=h, t0=t0, t1=round(float(self.item["dur"]), 2)))
        self.blurs_changed(); self.say("Blur box added. Drag its bar on the timeline to choose when it shows. Press ▶ to watch the result.")

    # ----- selecting, moving and resizing elements right on the pictures -----
    def pane_dims(self, pane):
        pn = self.orig if pane == "orig" else self.edit
        return pn.pump.w, pn.pump.h

    def items_of(self, key):
        return self.segs if key == "clip" else self.blurs if key == "blur" else self.overlays if key == "overlay" else self.texts

    def active_items(self, key):
        pos, d = self.clock.pos(), float(self.item["dur"])
        return [(i, it) for i, it in enumerate(self.items_of(key)) if it.get("t0", 0.0) - 0.02 <= pos <= it.get("t1", d) + 0.02]

    def el_state(self, key, it, pos=None):
        pos = self.clock.pos() if pos is None else pos
        if key == "clip": return dict(s=float(it.get("s", 1.0)), px=float(it.get("px", 0.0)), py=float(it.get("py", 0.0)))
        if key == "overlay": return kf_state(it, pos, OVL_KEYS)
        return kf_state(it, pos, BLUR_KEYS) if key == "blur" else kf_state(text_defaults(it, self.vertical.get()), pos, TEXT_KEYS)

    def edit_fwd_rect(self, st):
        """A blur box (fractions of the original frame) -> where it sits on the EDITED picture, or None if that part isn't shown."""
        o = self.opts(); W, H = self.edit.pump.w, self.edit.pump.h; SW, SH = self.size
        ci = self.clip_at_playhead()
        if ci is not None and self.segs[ci].get("nocam") and o["layout"] == "facecam": o = dict(o, layout="blur")
        x0, y0, x1, y1 = st["x"], st["y"], st["x"] + st["w"], st["y"] + st["h"]; cl = lambda v, a, b: max(a, min(b, v))
        if not o["vertical"]:
            sc = min(W / SW, H / SH); dw, dh = SW * sc, SH * sc; ox, oy = (W - dw) / 2, (H - dh) / 2
            return (ox + x0 * dw, oy + y0 * dh, ox + x1 * dw, oy + y1 * dh)
        ecam, egx = self.cam_gx_for(ci)
        if o["layout"] == "facecam" and ecam:
            cam = ecam; TH = cam_top_h(W, H, cam); GH = H - TH
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            if cam["x"] <= cx <= cam["x"] + cam["w"] and cam["y"] <= cy <= cam["y"] + cam["h"]:
                sc = max(W / (cam["w"] * SW), TH / (cam["h"] * SH)); mx, my = cam["x"] + cam["w"] / 2, cam["y"] + cam["h"] / 2
                r = (cl(W / 2 + (x0 - mx) * sc * SW, 0, W), cl(TH / 2 + (y0 - my) * sc * SH, 0, TH), cl(W / 2 + (x1 - mx) * sc * SW, 0, W), cl(TH / 2 + (y1 - my) * sc * SH, 0, TH))
            else:
                gx = egx if egx is not None else auto_gx(cam, W, GH); gw = min(SW, SH * W / GH); fx = lambda f: (f * SW - (SW - gw) * gx) / gw * W
                r = (cl(fx(x0), 0, W), TH + cl(y0, 0, 1) * GH, cl(fx(x1), 0, W), TH + cl(y1, 0, 1) * GH)
        elif o["layout"] == "crop":
            cw = min(SW, SH * 9 / 16); fx = lambda f: (f * SW - (SW - cw) / 2) / cw * W
            r = (cl(fx(x0), 0, W), y0 * H, cl(fx(x1), 0, W), y1 * H)
        else:
            fh = W * SH / SW; fy0 = (H - fh) / 2
            r = (x0 * W, cl(fy0 + y0 * fh, 0, H), x1 * W, cl(fy0 + y1 * fh, 0, H))
        return r if (r[2] - r[0] > 2 and r[3] - r[1] > 2) else None

    def el_rect(self, pane, key, it):
        st = self.el_state(key, it); W, H = self.pane_dims(pane); SW, SH = self.size
        if key == "clip":                                                  # where this clip's picture sits on the edited preview
            if pane == "orig": return None
            cw, ch = float(it.get("cw", 1.0)), float(it.get("ch", 1.0)); w, h = cw * st["s"], ch * st["s"]
            return self.edit_fwd_rect(dict(x=0.5 + st["px"] - w / 2, y=0.5 + st["py"] - h / 2, w=w, h=h))
        if key == "blur":
            if pane == "orig":
                sc = min(W / SW, H / SH); dw, dh = SW * sc, SH * sc; ox, oy = (W - dw) / 2, (H - dh) / 2
                return (ox + st["x"] * dw, oy + st["y"] * dh, ox + (st["x"] + st["w"]) * dw, oy + (st["y"] + st["h"]) * dh)
            return self.edit_fwd_rect(st)
        if pane == "orig": return None
        if key == "overlay":
            w = max(8.0, st["w"] * W); h = w * float(it.get("ar", 1.0)); cx, cy = st["x"] * W, st["y"] * H
            return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)
        vert = self.vertical.get(); fs = (110 if vert else 80) * 0.8 * st["size"] * H / (1920 if vert else 1080)
        lines = (it.get("text") or " ").split("\n"); w = 0.52 * fs * max(len(l) for l in lines) + 0.5 * fs; h = 1.25 * fs * len(lines)
        cx, cy = st["x"] * W, st["y"] * H
        return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)

    def cap_default_centre(self, W, H):
        """Where the captions sit with no hand placement, as a fraction of the picture (matches make_ass)."""
        S = resolve_style(self.style); vert = self.vertical.get(); k = H / (1920 if vert else 1080)
        fs = S["size"] * (1 if vert else 0.73)
        if S["position"] == "seam" and vert: return 0.5, cam_top_h(1080, 1920) / 1920
        if S["position"] == "top": return 0.5, ((90 if vert else 40) + fs * 0.6) * k / H
        if S["position"] == "middle": return 0.5, 0.5
        mv = S["y_offset"] if vert else int(S["y_offset"] * 0.2)
        return 0.5, (H / k - mv - fs * 0.6) * k / H

    def cap_rect(self, pane="edit"):
        """Box around the caption words showing right now (None when captions are off or nothing is being said)."""
        if pane != "edit" or not self.captions.get() or not self.words: return None
        S = resolve_style(self.style); W, H = self.pane_dims(pane); vert = self.vertical.get(); pos = self.clock.pos(); n = S["words"]
        idx = next((i for i, w in enumerate(self.words) if w["s"] - 0.05 <= pos <= w["e"] + 0.15), None)
        if idx is None: return None
        grp = self.words[(idx // n) * n:(idx // n) * n + n]; chars = max(4, len(" ".join(x["w"] for x in grp)))
        fs = S["size"] * (1 if vert else 0.73) * H / (1920 if vert else 1080)
        w = min(W * 0.96, 0.56 * fs * chars + fs * 0.6); h = fs * 1.35
        cx, cy = (S["cap_x"], S["cap_y"]) if S["cap_x"] is not None else self.cap_default_centre(W, H)
        return (cx * W - w / 2, cy * H - h / 2, cx * W + w / 2, cy * H + h / 2)

    @staticmethod
    def corners(r): return {"nw": (r[0], r[1]), "ne": (r[2], r[1]), "sw": (r[0], r[3]), "se": (r[2], r[3])}

    def stage_hit(self, x, y, pane):
        inside = lambda r, m=0: r is not None and r[0] - m <= x <= r[2] + m and r[1] - m <= y <= r[3] + m
        sel = self.tt.sel
        if sel and sel[0] in ("blur", "text", "overlay", "clip"):
            act = dict(self.active_items(sel[0]))
            if sel[1] in act and not (pane == "orig" and sel[0] in ("text", "overlay", "clip")):
                r = self.el_rect(pane, sel[0], act[sel[1]])
                if r:
                    for h, (hx, hy) in self.corners(r).items():
                        if abs(x - hx) <= 10 and abs(y - hy) <= 10: return sel[0], sel[1], h
                    if inside(r): return sel[0], sel[1], "move"
        for key in ("overlay", "text", "blur"):
            if pane == "orig" and key in ("text", "overlay"): continue
            for i, it in reversed(self.active_items(key)):
                if inside(self.el_rect(pane, key, it)): return key, i, "move"
        if pane == "edit":                                                 # the caption words: drag to put them anywhere
            if inside(self.cap_rect("edit")): return "cap", 0, "move"
        if pane == "edit":                                                 # click the picture itself: its clip gets handles (drag to move, corners to resize)
            for i, it in reversed(self.active_items("clip")):
                if inside(self.el_rect(pane, "clip", it)): return "clip", i, "move"
        return None

    def stage_press(self, e, pane):
        if self.splicing: return
        hit = self.stage_hit(e.x, e.y, pane); self.tt.sel_kf = None
        if not hit: self.tt.sel = None; self._stage = None; return
        key, i, handle = hit; self.clock.pause()
        if key == "cap":
            self.tt.sel = None; r = self.cap_rect("edit"); W, H = self.pane_dims("edit")
            self._stage = dict(pane=pane, key="cap", i=0, handle="move", sx=e.x, sy=e.y, rect=r, moved=False, pos=self.clock.pos(), c0=((r[0] + r[2]) / 2 / W, (r[1] + r[3]) / 2 / H)); return
        self.tt.sel = (key, i)
        it = self.items_of(key)[i]
        self._stage = dict(pane=pane, key=key, i=i, handle=handle, sx=e.x, sy=e.y, rect=self.el_rect(pane, key, it), st0=self.el_state(key, it),
                           pos=self.clock.pos(), moved=False)

    def stage_move(self, e, pane):
        sg = self._stage
        if not sg or pane != sg["pane"] or not sg["rect"]: return
        key, i, r0 = sg["key"], sg["i"], sg["rect"]; W, H = self.pane_dims(pane)
        if key == "cap":
            if abs(e.x - sg["sx"]) + abs(e.y - sg["sy"]) < 2 and not sg["moved"]: return
            self.style["cap_x"] = round(max(0.0, min(1.0, sg["c0"][0] + (e.x - sg["sx"]) / W)), 4)
            self.style["cap_y"] = round(max(0.0, min(1.0, sg["c0"][1] + (e.y - sg["sy"]) / H)), 4)
            sg["moved"] = True; self.schedule_refresh(); return
        it = self.items_of(key)[i]
        if sg["handle"] == "move":
            dx, dy = e.x - sg["sx"], e.y - sg["sy"]; r = (r0[0] + dx, r0[1] + dy, r0[2] + dx, r0[3] + dy)
        else:
            ox, oy = {"nw": (r0[2], r0[3]), "ne": (r0[0], r0[3]), "sw": (r0[2], r0[1]), "se": (r0[0], r0[1])}[sg["handle"]]
            r = (min(ox, e.x), min(oy, e.y), max(ox, e.x), max(oy, e.y))
        if key == "clip":
            s0 = sg["st0"]; ref = self.edit_fwd_rect(dict(x=0.25, y=0.25, w=0.5, h=0.5))
            if sg["handle"] == "move" and ref:
                kx, ky = 0.5 / max(1.0, ref[2] - ref[0]), 0.5 / max(1.0, ref[3] - ref[1]); dx, dy = e.x - sg["sx"], e.y - sg["sy"]
                st = dict(s=s0["s"], px=round(max(-1.5, min(1.5, s0["px"] + dx * kx)), 4), py=round(max(-1.5, min(1.5, s0["py"] + dy * ky)), 4))
            else:
                cx, cy = (r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2; hx, hy = self.corners(r0)[sg["handle"]] if sg["handle"] != "move" else (r0[2], r0[3])
                f = ((e.x - cx) ** 2 + (e.y - cy) ** 2) ** 0.5 / max(1.0, ((hx - cx) ** 2 + (hy - cy) ** 2) ** 0.5)
                st = dict(s=round(max(0.2, min(5.0, s0["s"] * f)), 3), px=s0["px"], py=s0["py"])
            it.update(st); sg["moved"] = True; return
        if key == "blur":
            if pane == "orig":
                SW, SH = self.size; sc = min(W / SW, H / SH); dw, dh = SW * sc, SH * sc; ox, oy = (W - dw) / 2, (H - dh) / 2
                fr = lambda px, py: (max(0.0, min(1.0, (px - ox) / dw)), max(0.0, min(1.0, (py - oy) / dh)))
                a, b = fr(r[0], r[1]), fr(r[2], r[3]); x, y, w, h = a[0], a[1], b[0] - a[0], b[1] - a[1]
            else: x, y, w, h = self.edit_rect_to_src(*r)
            st = dict(x=x, y=y, w=max(w, 0.01), h=max(h, 0.01))
        elif key == "overlay":
            s0 = sg["st0"]
            if sg["handle"] == "move": st = dict(x=max(0.0, min(1.0, (r[0] + r[2]) / 2 / W)), y=max(0.0, min(1.0, (r[1] + r[3]) / 2 / H)), w=s0["w"])
            else:
                cx, cy = (r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2; hx, hy = self.corners(r0)[sg["handle"]]
                f = ((e.x - cx) ** 2 + (e.y - cy) ** 2) ** 0.5 / max(1.0, ((hx - cx) ** 2 + (hy - cy) ** 2) ** 0.5)
                st = dict(x=s0["x"], y=s0["y"], w=round(max(0.03, min(2.0, s0["w"] * f)), 4))
        else:
            s0 = sg["st0"]
            if sg["handle"] == "move": st = dict(x=max(0.0, min(1.0, (r[0] + r[2]) / 2 / W)), y=max(0.0, min(1.0, (r[1] + r[3]) / 2 / H)), size=s0["size"])
            else:
                cx, cy = (r0[0] + r0[2]) / 2, (r0[1] + r0[3]) / 2; hx, hy = self.corners(r0)[sg["handle"]]
                f = max(0.2, ((e.x - cx) ** 2 + (e.y - cy) ** 2) ** 0.5 / max(1.0, ((hx - cx) ** 2 + (hy - cy) ** 2) ** 0.5))
                st = dict(x=s0["x"], y=s0["y"], size=round(max(0.2, min(6.0, s0["size"] * f)), 3))
        keys = BLUR_KEYS if key == "blur" else OVL_KEYS if key == "overlay" else TEXT_KEYS
        if it.get("kf"): upsert_kf(it, sg["pos"], st, keys)            # an animated element: this becomes a keyframe at the playhead
        else: it.update(st)
        sg["moved"] = True

    def stage_release(self, e, pane):
        sg, self._stage = self._stage, None
        if not sg or not sg["moved"]: return
        if sg["key"] == "cap": return self.say("Captions moved. Drag them again to adjust, or press ↺ Reset caption position to put them back.")
        if sg["key"] == "clip": return self.fit_changed()
        (self.blurs_changed if sg["key"] == "blur" else self.overlays_changed if sg["key"] == "overlay" else self.texts_changed)()
        it = self.items_of(sg["key"])[sg["i"]]
        self.say("Keyframe recorded at the playhead - move the playhead and drag again to animate it." if it.get("kf") else
                 "Moved / resized. Press ◆ Keyframe, then change it at another time, to make it move.")

    def reset_cap_pos(self):
        self.style["cap_x"] = self.style["cap_y"] = None; self.schedule_refresh()
        self.say("Captions are back where the Position setting puts them.")

    def edit_dbl(self, e):
        hit = self.stage_hit(e.x, e.y, "edit")
        if hit and hit[0] == "text": self.edit_text(hit[1])

    # ----- keyframes, copy / paste, delete -----
    def selected_el(self):
        sel = self.tt.sel
        if sel and sel[0] in ("blur", "text", "overlay"):
            lst = self.items_of(sel[0])
            if sel[1] < len(lst): return sel[0], lst[sel[1]]

    def add_keyframe(self):
        se = self.selected_el()
        if not se: return self.say("Click a blur box or text first (on a picture, or its bar on the timeline), move the playhead, then press ◆ Keyframe.")
        key, it = se; pos = round(self.clock.pos(), 2); d = float(self.item["dur"])
        if not (it.get("t0", 0.0) - 0.02 <= pos <= it.get("t1", d) + 0.02): return self.say("Move the playhead inside the element's bar first.")
        upsert_kf(it, pos, self.el_state(key, it, pos), BLUR_KEYS if key == "blur" else OVL_KEYS if key == "overlay" else TEXT_KEYS)
        self.tt.refresh(); self.schedule_refresh()
        self.say(f"Keyframe added at {pos:.1f}s. Move the playhead and drag / resize the element on the picture to add another - it glides between them.")

    def clear_keyframes(self):
        se = self.selected_el()
        if not se or not se[1].get("kf"): return self.say("That element has no keyframes.")
        se[1].pop("kf"); self.tt.sel_kf = None; self.tt.refresh(); self.schedule_refresh(); self.say("Keyframes cleared (the element stays where it is now).")

    def _typing(self):
        try:
            w = self.top.focus_get(); return (w.winfo_class() if w is not None else "") in ("Entry", "Text", "TEntry", "Spinbox")
        except Exception: return False

    def do_copy(self, e=None):
        if self._typing(): return
        self.tt.copy_selected(); return "break"

    def do_paste(self, e=None):
        if self._typing(): return
        self.tt.paste(); return "break"

    def do_cut(self, e=None):
        """Ctrl+X: copy the selected element, then remove it (paste it back anywhere with Ctrl+V)."""
        if self._typing(): return
        if self.tt.copy_selected():
            self.tt.delete_selected(); self.say("Cut. Move the playhead and press Ctrl+V to paste it.")
        return "break"

    def do_dup(self, e=None):
        if self._typing(): return
        self.tt.copy_selected(); self.tt.paste(); return "break"

    def do_delete(self, e=None):
        if self._typing(): return
        self.tt.delete_selected(); return "break"

    def sync_vol_editor(self):
        if self.clip_editor:
            try: self.clip_editor.vars["vol"].set(self.clip_audio["vol"] * 100); self.clip_editor.mute.set(False)
            except Exception: pass

    def blurs_changed(self):
        self.tt.refresh(); self.schedule_refresh()

    # ----- text overlays, music, voice parts on the timeline -----
    def add_text(self):
        d = float(self.item["dur"]); t0 = round(min(d - 1.0, max(0.0, self.clock.pos())), 2)
        TextDialog(self, dict(text="", t0=t0, t1=round(min(d, t0 + 3.0), 2), pos="top", x=0.5, y=TEXT_PRESET_Y["top"][0 if self.vertical.get() else 1], size=1.0), True)

    def edit_text(self, i):
        if 0 <= i < len(self.texts): TextDialog(self, self.texts[i], False)

    def texts_changed(self):
        self.tt.refresh(); self.schedule_refresh(); self.hook_entry_sync(); self.hook_len_sync()
        it = self.hook_item()
        if it and bool(it.get("blurbg")) != bool(self.hook_blur.get()):
            self._hook_guard = True
            try: self.hook_blur.set(bool(it.get("blurbg")))
            finally: self._hook_guard = False

    # ----- the hook: big words for the first 3 seconds, a text element like any other (click it on the preview to move / resize, double-click for its look) -----
    def cam_gx_for(self, i):
        if i is not None and 0 <= i < len(self.segs) and self.segs[i].get("cam"): return self.segs[i]["cam"], self.segs[i].get("gx")
        return self.cam, self.gx

    def cam_linked_toggled(self):
        if getattr(self, "_cam_guard", False): return
        i = self.clip_at_playhead()
        if i is None: return
        sg = self.segs[i]
        if self.cam_linked.get(): sg.pop("cam", None); sg.pop("gx", None); self.say("This clip uses the same facecam as the others again.")
        else:
            base = self.cam or dict(x=0.70, y=0.60, w=0.28, h=0.36, ar=1.0, src_ar=1.0)
            sg["cam"] = dict(base); sg["gx"] = self.gx; self.say("This clip now has its own facecam. Use Adjust facecam... to place it - the other clips won't change.")
        if self.cam_editor:
            try: self.cam_editor.close()
            except Exception: pass
        self.tt.refresh(); self.schedule_refresh()

    def cam_toggled(self):
        """The tick box only affects the clip you have selected (or the one under the playhead)."""
        if getattr(self, "_cam_guard", False): return
        i = self.clip_at_playhead()
        if i is None: return
        if self.cam_on.get(): self.segs[i].pop("nocam", None)
        else: self.segs[i]["nocam"] = True
        self.tt.refresh(); self.schedule_refresh()
        self.say("Facecam is off for this clip only." if not self.cam_on.get() else "Facecam is back on for this clip.")

    def sync_cam_box(self):
        i = self.clip_at_playhead()
        if i is None: return
        want, linked = not self.segs[i].get("nocam"), not self.segs[i].get("cam")
        if bool(self.cam_on.get()) != want or bool(self.cam_linked.get()) != linked:
            self._cam_guard = True
            try: self.cam_on.set(want); self.cam_linked.set(linked)
            finally: self._cam_guard = False

    def fit_changed(self):
        self.tt.refresh(); self.schedule_refresh()
        self.say("Clip moved / resized. Drag its corners to resize, drag inside to move, or use ▢ Clip size & position... for Fit / Fill.")

    def clip_at_playhead(self):
        sel = self.tt.sel
        if sel and sel[0] == "clip" and sel[1] < len(self.segs): return sel[1]
        act = self.active_items("clip"); return act[-1][0] if act else None

    def clip_size_dialog(self):
        i = self.clip_at_playhead()
        if i is None: return self.say("Move the playhead onto a clip first.")
        ClipFitDialog(self, i)

    def overlay_options(self):
        sel = self.tt.sel; i = sel[1] if sel and sel[0] == "overlay" and sel[1] < len(self.overlays) else (0 if self.overlays else None)
        if i is None: return self.say("Add a picture first (🖼 Add picture / GIF / video), or click one on the timeline.")
        OverlayDialog(self, i)

    def hook_len_typed(self):
        it = self.hook_item()
        if getattr(self, "_hook_guard", False) or not it: return
        d = float(self.item["dur"]); it["t1"] = round(min(d, float(it.get("t0", 0.0)) + float(self.hook_len.get())), 2)
        self.tt.refresh(); self.schedule_refresh()

    def hook_len_sync(self):
        it = self.hook_item()
        if it:
            self._hook_guard = True
            try: self.hook_len.set(round(max(0.5, float(it.get("t1", 3.0)) - float(it.get("t0", 0.0))) * 2) / 2)
            finally: self._hook_guard = False

    def hook_blur_typed(self):
        it = self.hook_item()
        if getattr(self, "_hook_guard", False) or not it: return
        it["blurbg"] = bool(self.hook_blur.get()); self.tt.refresh(); self.schedule_refresh()

    def hook_item(self):
        return next((t for t in self.texts if t.get("hook")), None)

    def hook_entry_sync(self):
        it = self.hook_item(); want = (it.get("text", "") if it else "")
        if self.hook.get().strip() != want.strip():
            self._hook_guard = True
            try: self.hook.set(want)
            finally: self._hook_guard = False

    def hook_text_from_item(self):
        it = self.hook_item()
        if it and self.hook.get() != it.get("text", ""): self.hook_entry_sync()

    def hook_typed(self):
        """You typed in the Hook box: the hook element on the picture follows."""
        if getattr(self, "_hook_guard", False): return
        txt = self.hook.get().strip(); it = self.hook_item(); d = float(self.item["dur"])
        if txt and not it:
            self.texts.insert(0, dict(hook=True, text=txt, t0=0.0, t1=round(min(d, float(self.hook_len.get())), 2), blurbg=bool(self.hook_blur.get()), pos="top", x=0.5, y=0.2 if self.vertical.get() else 0.13, size=1.1,
                                      font="Impact", color=self.style.get("hook_color", "#FF1F1F"), outline=7, pop=True))
        elif txt and it: it["text"] = txt
        elif it: self.texts.remove(it)
        self.tt.refresh(); self.schedule_refresh()
        if self.tt.sel and self.tt.sel[0] == "text" and self.tt.sel[1] >= len(self.texts): self.tt.sel = None

    def open_hook_settings(self):
        it = self.hook_item()
        if not it:
            d = float(self.item["dur"])
            it = dict(hook=True, text="", t0=0.0, t1=round(min(d, 3.0), 2), pos="top", x=0.5, y=0.2 if self.vertical.get() else 0.13, size=1.1, font="Impact",
                      color=self.style.get("hook_color", "#FF1F1F"), outline=7, pop=True)
            return TextDialog(self, it, True)
        TextDialog(self, it, False)

    # ----- picture / GIF / video overlays (drag them in from anywhere) -----
    def overlays_changed(self):
        self.tt.refresh(); self.schedule_refresh()

    def add_overlay_dialog(self):
        ps = filedialog.askopenfilenames(title="Pick pictures, GIFs or short videos to lay over the clip", initialdir=os.path.expanduser("~"),
                                         filetypes=[("Pictures / GIFs / videos", "*.png *.jpg *.jpeg *.webp *.gif *.bmp *.apng *.webm *.mov *.mp4"), ("All files", "*.*")])
        self.add_overlays(list(ps or ()))

    def add_overlays(self, paths, t=None):
        """Put each file on the timeline as an overlay starting at `t` (default: the playhead): centred, about a third of the picture wide."""
        d = float(self.item["dur"]); n = 0
        for pth in paths:
            if not pth or not os.path.isfile(pth): continue
            try: iw, ih = video_size(pth)
            except Exception: self.say(f"Couldn't read {os.path.basename(pth)} as a picture or video."); continue
            t0 = round(min(max(0.0, d - 0.5), max(0.0, self.clock.pos() if t is None else t)), 2); ai = anim_info(pth)
            length = min(8.0, max(2.0, ai[0] / ai[1])) if ai else 4.0
            vert = self.vertical.get(); wf = 0.55 if vert else 0.28; asp = 9 / 16 if vert else 16 / 9        # asp = frame width / height
            if ih / max(1, iw) * wf * asp > 0.8: wf = 0.8 / (ih / max(1, iw) * asp)                          # very tall picture: keep it on screen
            self.overlays.append(dict(path=pth, name=os.path.basename(pth)[:40], t0=t0, t1=round(min(d, t0 + length), 2), x=0.5, y=0.5, w=round(max(0.05, min(0.9, wf)), 3),
                                      ar=round(ih / max(1, iw), 4), anim=bool(ai)))
            self.tt.sel = ("overlay", len(self.overlays) - 1); n += 1
        if n:
            self.overlays_changed(); self.say(f"Added {n} overlay(s). Drag it on the preview to move it, drag a corner to resize, drag its bar on the timeline to change when it shows. ◆ Keyframe makes it move.")

    def on_drop(self, paths, e=None):
        """Files dragged onto the editor. Pictures / GIFs always become overlays; a video dropped on the TIMELINE becomes an overlay (at the spot you
        dropped it), a video dropped anywhere else is added to the end of the clip."""
        if self.splicing: return self.say("Wait a moment for the clip to finish joining, then drop the file(s) again.")
        imgs = [x for x in paths if x.lower().endswith(IMG_EXT)]; vids = [x for x in paths if x not in imgs]
        t = None
        try:
            if e is not None and self.tt.c.winfo_rootx() <= e.x_root <= self.tt.c.winfo_rootx() + self.tt.c.winfo_width() and \
               self.tt.c.winfo_rooty() <= e.y_root <= self.tt.c.winfo_rooty() + self.tt.c.winfo_height():
                t = self.tt._t(e.x_root - self.tt.c.winfo_rootx())
        except Exception: t = None
        if imgs: self.add_overlays(imgs, t)
        if vids:
            if t is not None: self.add_overlays(vids, t)
            else: self.add_paths(vids)

    def add_music_menu(self):
        if self.music.get("path"): return self.open_music_editor()
        m = tk.Menu(self.top, tearoff=0)
        m.add_command(label="🎵 Trending sound...", command=lambda: self.app.open_sounds(self.set_sound))
        m.add_command(label="My own audio file...", command=self.pick_music_file)
        try: m.tk_popup(self.top.winfo_pointerx(), self.top.winfo_pointery())
        except Exception: self.pick_music_file()

    def pick_music_file(self):
        pth = filedialog.askopenfilename(title="Pick an audio file", filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac *.ogg *.flac"), ("All", "*.*")])
        if not pth: return
        self.music.update(path=pth, title=os.path.basename(pth), on=True, ss=0.0, at=0.0, until=float(self.item["dur"]))
        self.music_changed(); self.tt.refresh()
        self.say("Music added. Drag its bar on the timeline to move it; open 🎚 Music & fades... for volume and fades.")

    def add_voice_part(self):
        d = float(self.item["dur"]); t0 = round(min(d - 0.5, max(0.0, self.clock.pos())), 2)
        self.voice["ranges"].append(dict(t0=t0, t1=round(min(d, t0 + 3.0), 2))); self.voice["on"] = True
        self.voice_changed(); self.tt.refresh()
        self.say("Voice-focus part added (the streamer's voice is boosted over the game in that stretch). Drag its ends to fit.")

    def change_dest(self):
        p = filedialog.askdirectory(title="Choose where finished clips are saved", initialdir=os.path.abspath(CLIPS_DIR))
        if p and self.app.apply_dest(p): self.dest_lbl.config(text=os.path.abspath(CLIPS_DIR))

    def open_blur_editor(self):
        if self.blur_editor: return self.blur_editor.top.lift()
        self.blur_editor = BlurEditor(self.app, self)

    def open_cam_editor(self):
        if self.cam_editor: return self.cam_editor.top.lift()
        self.cam_editor = FacecamEditor(self.app, self)

    def set_sound(self, entry):
        """A song was picked in the Trending sounds window: download its 30-second sample, then open the Music window to set it up."""
        self.sound = entry
        self.sound_lbl.config(text=f"♪ {entry['title']}")
        self.say(f"Getting a 30-second sample of {entry['title']}...")
        def work():
            try: got, err = fetch_preview_audio(entry), ""
            except Exception as ex: got, err = None, str(ex)[:100]
            def fin():
                if not got:
                    return self.say(f"Couldn't get a sample of {entry['title']} ({err or 'not in Apple’s catalog'}). Use 🎚 Music & fades... to add your own audio file instead.")
                self.music.update(path=got[0], title=entry["title"], on=True, ss=0.0, until=float(self.item["dur"]))
                self.say(f"Added a 30-second sample of {entry['title']}. Set volume and fades in the Music window.")
                self.open_music_editor(); self.music_changed()
                if self.music_editor: self.music_editor.load()
            self.app.q.put(("call", fin))
        threading.Thread(target=work, daemon=True).start()

    def note_sound(self, video_path):
        if self.sound:
            try: write_sound_note(video_path, self.sound, load_chart(), mixed=music_active(self.music))
            except OSError as e: self.app.log(f"Couldn't save the sound note: {e}")

    def open_editor(self):
        if self.editor: return self.editor.top.lift()
        self.editor = CaptionEditor(self.app, self.style, on_change=self.schedule_refresh, review=self)
        if self.words: self.editor.set_transcript(self.words)

    def set_edited_text(self, text):
        if self.base_words:
            self.words = edit_words(self.base_words, text); self.schedule_refresh()

    def retranscribe(self):
        if self.transcribing: return
        self.words = self.base_words = None; self.captions.set(True); self.refresh_preview()

    def open_player(self):
        path = os.path.abspath(self.final or self.item["raw"])
        try: os.startfile(path)
        except AttributeError: subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

    # ----- decisions -----
    def process(self):
        if self.busy or self.splicing: return
        self.busy = self.decided = True; self.lock(True); o = self.opts()
        self.say("Processing" + (" (sending to the AI - uses credits)" if o["ai"] else "") + "... please wait.")
        raw = self.item["raw"]; final = self.export_path()
        def work():
            try: res, err = process_clip(raw, final, o, self.item["msgs"], self.app.log, words=self.words), ""
            except Exception as e: res, err = None, str(e)
            self.app.q.put(("call", lambda: self.done(res, err)))
        threading.Thread(target=work, daemon=True).start()

    def done(self, res, err):
        self.busy = False
        if res is None:
            self.decided = False; self.lock(False); self.restart_panes(); return self.say(f"Processing failed: {err}")
        self.orig.stop(); self.edit.stop()
        self.app.finish_clip(res, self.item["raw"], self.item["idx"], self.item["stamp"])
        self.final = res["final"]; self.lock(False); self.note_sound(self.final)
        self.export_btn.config(state="disabled")
        ai = res.get("ai"); size = (self.VW, self.VH) if self.opts()["vertical"] else (self.PW, self.PH)
        no_ai = " | AI did not run - check your API key / credits (see log); clip saved without it" if (self.opts()["ai"] and not ai) else ""
        self.say(f"Saved {os.path.basename(self.final)}" + (f"  |  AI {ai['score']}/10: {ai['title']} - {ai['reason']}" if ai else "") + no_ai
                 + "   (showing the finished clip; close this window when done)")
        self.audio.prepare(self.final)
        try: self.clock.dur = duration(self.final)
        except Exception: pass
        self.sync_panes = [self.edit]; self.clock.seek(0, paused=True); self.autostart, self.autostart_t = True, time.time()
        self.edit.resize(*size); self.edit.play(self.final, fit_vf(*size))

    def save_raw(self):
        self.orig.stop(); self.edit.stop(); raw = self.item["raw"]; new = unversion(raw).replace("_raw.mp4", ".mp4")
        safe_remove(new); os.replace(raw, new); self.decided = True; self.note_sound(new)
        self.app.clip_n += 1; self.app.refresh_size(force=True)
        self.app.q.put(("clip", self.item["idx"], f"{self.item['stamp']}  ✔ saved raw {os.path.basename(new)}")); self._close()

    def discard(self):
        self.orig.stop(); self.edit.stop(); safe_remove(self.item["raw"]); self.decided = True
        self.app.refresh_size(force=True)
        self.app.q.put(("clip", self.item["idx"], f"{self.item['stamp']}  🗑 discarded (no credits used)")); self._close()

    def on_close(self):
        if self.busy: return
        if not self.decided:                                  # closing the window never deletes anything
            self.orig.stop(); self.edit.stop(); self.app.clip_n += 1; self.note_sound(self.item["raw"])
            self.app.q.put(("clip", self.item["idx"], f"{self.item['stamp']}  kept raw (not processed): {os.path.basename(self.item['raw'])}"))
        self._close()

    def _close(self):
        if self.cam_editor: self.cam_editor.close()
        if self.editor: self.editor.destroy()
        if self.export_dlg: self.export_dlg.close(restart=False)
        if self.clip_editor: self.clip_editor.close()
        self.closed = True; self.orig.destroy(); self.edit.destroy()
        for v in self.versions:                                        # the older copies made by splicing (the current one is handled by save / discard / keep)
            if v != self.item["raw"]: safe_remove(v)
        safe_remove(os.path.join(tempfile.gettempdir(), f"clipstudio_review_{os.getpid()}_{id(self)}.ass"))
        self.top.destroy(); self.app.review_win = None; self.app.root.after(200, self.app.open_next_review)


MAX_STREAMS = 8


class Smoother:
    """Makes a meter look clean without hiding anything. The detector always uses the exact raw numbers; this is only what you SEE.
    Rises are followed almost instantly (with a tiny 0.25 s look-ahead so the bar doesn't trail the action), falls ease down smoothly."""
    LEAD = 0.25

    def __init__(self): self.v = self.hold = self.hold_t = self.vel = self.prev = 0.0; self.prev_t = None

    def feed(self, raw, now):
        """call once per detector reading"""
        if self.prev_t is not None and now > self.prev_t:
            inst = (raw - self.prev) / (now - self.prev_t); self.vel = 0.5 * self.vel + 0.5 * inst
        self.prev, self.prev_t = raw, now

    def frame(self, dt, now):
        raw = self.prev
        fresh = max(0.0, 1.0 - (now - (self.prev_t or now)) / 0.5)                           # the guess fades out before the next reading
        pred = raw + min(max(self.vel, 0.0), 4.0) * self.LEAD * fresh                        # only ever predicts a rise, and only a little
        pred = min(pred, raw * 1.1 + 0.05)                                                   # never shows much more than the real value
        k = min(1.0, dt * (28 if pred > self.v else 6))                                      # up: ~instant, down: gentle
        self.v += (pred - self.v) * k
        if self.v >= self.hold: self.hold, self.hold_t = self.v, now
        elif now - self.hold_t > 1.2: self.hold = max(self.v, self.hold - dt * 0.6)
        return self.v, self.hold


def smooth_hist(hist):
    """Visual-only smoothing of the history bars: a light blur, but a spike never gets flattened (it keeps >= 85% of its real height)."""
    v = [x[0] for x in hist]; out = []
    for i, x in enumerate(v):
        a, b = v[i - 1] if i else x, v[i + 1] if i + 1 < len(v) else x
        out.append(max(0.25 * a + 0.5 * x + 0.25 * b, 0.85 * x))
    return out


class Watcher:
    """Everything that belongs to ONE watched stream: its recorder, chat reader, detectors and meters. The app runs one per channel."""
    def __init__(self, channel):
        self.channel = channel
        self.det, self.aud, self.buf = PeakDetector(), AudioHeat(), None
        self.stop_ev = threading.Event()
        self.chat_log = deque(maxlen=4000)           # (wall, user, text) for AI context
        self.hist = deque(maxlen=360); self.ahist = deque(maxlen=360)
        self.disp = self.hold = self.hold_t = 0.0
        self.adisp = self.ahold = self.ahold_t = 0.0
        self.last_clip_t = self.chat_hot_t = self.aud_hot_t = -1e9
        self.lines = deque(maxlen=300)                # this stream's recent chat lines
        self.clips = 0; self.r = self.ar = None; self.ok_since = time.time()
        self.sm, self.asm, self.rt = Smoother(), Smoother(), time.time()

    def stop(self):
        self.stop_ev.set()
        if self.buf:
            try: self.buf.stop()
            except Exception: pass
            self.buf = None


class App:
    def __init__(self, root):
        self.root = root
        root.title("Live Stream Clipper"); root.configure(bg=BG); root.geometry("1040x1000")
        self.q = queue.Queue()
        self.all_lines = deque(maxlen=500)             # (channel, 'user: text') of every stream, for the 'All streams' feed
        self.watchers = {}; self.view = None          # channel -> Watcher; the one whose meters are on screen
        self.running = False
        self.clip_n = 0; self.last_tick = 0.0; self.used_mb = 0.0; self._size_t = 0.0; self.limit_hit = False; self.review_q = deque(); self.review_win = None; self.style_editor = None
        cfg = load_settings()
        try:
            if cfg.get('clips_dir'): set_clips_dir(cfg['clips_dir'])
        except OSError: pass
        self.sounds_win = None; self.channel = ''; saved_cs = cfg.get('caption_style') or {}
        if saved_cs and all(saved_cs.get(k) == v for k, v in LEGACY_CAPTION.items() if k in saved_cs and k != "censor") and "censor" not in saved_cs:
            saved_cs = {}                                      # you never changed the old default look -> you get the new one
        if str(saved_cs.get('hook_color', '')).upper() == '#FFE500': saved_cs = {**saved_cs, 'hook_color': CAPTION_DEFAULT['hook_color']}      # the old yellow default becomes the new red one
        self.caption_style = {**CAPTION_DEFAULT, **saved_cs}
        WHISPER.update(model=cfg.get('whisper_model', 'medium'), clean=cfg.get('whisper_clean', True), vocab=cfg.get('whisper_vocab', ''),
                       language=(cfg.get('whisper_language') or None) if 'whisper_language' in cfg else 'en')
        self._build(); self.refresh_tabs(); enable_drop(self.root, self.studio_open)     # dropping video files on the main window opens the studio
        self.poll(); self.render()

    # ---------------- UI ----------------
    def _build(self):
        r = self.root
        top = tk.Frame(r, bg=BG); top.pack(fill="x", padx=12, pady=(12, 4))
        tk.Label(top, text="Streamer(s)", bg=BG, fg=FG).pack(side="left")
        self.chan = tk.Entry(top, width=30, bg=PANEL, fg=FG, insertbackground=FG, relief="flat")
        self.chan.pack(side="left", padx=8, ipady=4, fill="x", expand=True)
        try: self.chan.bind("<KeyRelease>", lambda e: self.refresh_tabs())
        except Exception: pass
        self.btn = tk.Button(top, text="▶  START", bg=PURPLE, fg="white", relief="flat", padx=16, command=self.toggle)
        self.btn.pack(side="right")
        self.set_btn = tk.Button(top, text="⚙ Settings", bg=PANEL, fg=FG, relief="flat", padx=8, command=lambda: self.settings.set(not self.settings.is_open))
        self.set_btn.pack(side="right", padx=8)
        tk.Button(top, text="🎬 Editing studio", bg=PURPLE, fg="white", relief="flat", padx=10, command=self.open_studio).pack(side="right", padx=(0, 4))
        tk.Label(r, text="Type one or more channel names (or links), separated by commas, then press START.", bg=BG, fg="#adadb8", anchor="w").pack(fill="x", padx=14)

        # ---- everything else lives behind the Settings button (hidden until you want it) ----
        self.settings = Collapsible(r, "Settings", open=False, bg=PANEL, pack=False, body_bg=PANEL)
        self.settings.head.pack_forget()                                # no extra header: the top-bar button opens it
        st = self.settings.body
        self.settings.set = lambda on, _s=self.settings: self._show_settings(on)
        o = tk.Frame(st, bg=PANEL); o.pack(fill="x", padx=8, pady=(6, 2))
        self.cap, self.ai, self.vert = tk.BooleanVar(), tk.BooleanVar(), tk.BooleanVar()
        for v, t in ((self.cap, "Captions"), (self.vert, "Vertical 9:16")):
            tk.Checkbutton(o, text=t, variable=v, bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG).pack(side="left", padx=(0, 14))
        self.layout_choice = tk.StringVar(value="Blurred background")
        purple_menu(o, self.layout_choice, *LAYOUTS).pack(side="left", padx=(0, 14))
        self.review = tk.BooleanVar(value=True)
        tk.Checkbutton(o, text="Look at each clip before it's made", variable=self.review, bg=PANEL, fg=YELLOW, selectcolor=BG, activebackground=PANEL,
                       activeforeground=YELLOW).pack(side="left")
        o5 = tk.Frame(st, bg=PANEL); o5.pack(fill="x", padx=8, pady=(2, 6))
        tk.Label(o5, text="Save clips to", bg=PANEL, fg=FG).pack(side="left")
        self.dest = tk.StringVar(value=os.path.abspath(CLIPS_DIR))
        de = tk.Entry(o5, width=44, textvariable=self.dest, bg=BG, fg=FG, insertbackground=FG, relief="flat"); de.pack(side="left", padx=6, ipady=3, fill="x", expand=True)
        de.bind("<Return>", lambda ev: self.apply_dest(self.dest.get())); de.bind("<FocusOut>", lambda ev: self.apply_dest(self.dest.get()))
        tk.Button(o5, text="Browse...", bg=BG, fg=FG, relief="flat", command=self.browse_dest).pack(side="left")
        tk.Button(o5, text="New folder...", bg=BG, fg=FG, relief="flat", command=self.new_dest).pack(side="left", padx=4)
        tk.Button(o5, text="Open", bg=BG, fg=FG, relief="flat", command=self.open_clips).pack(side="left")

        adv = Collapsible(st, "How sensitive?  (when to take a clip)", bg=PANEL, pack=dict(fill="x", padx=8))
        s = tk.Frame(adv.body, bg=PANEL); s.pack(fill="x")
        self.trig = self._slider(s, "Trigger score", 1.5, 8.0, 3.0, 0.1)
        self.pre = self._slider(s, "Pre-roll (s)", 30, 90, 45, 1)
        self.post = self._slider(s, "Post-roll (s)", 5, 30, 15, 1)
        self.cool = self._slider(s, "Cooldown (s)", 30, 300, 90, 5)
        self.loud = self._slider(s, "Streamer loudness (+dB)", 4, 20, 9, 0.5)

        more = Collapsible(st, "More options", bg=PANEL, pack=dict(fill="x", padx=8, pady=(0, 6)))
        m = more.body
        r1 = tk.Frame(m, bg=PANEL); r1.pack(fill="x", pady=2)
        tk.Label(r1, text="Stream quality", bg=PANEL, fg=FG).pack(side="left")
        self.quality = tk.StringVar(value="720p60,720p,best")
        purple_menu(r1, self.quality, "720p60,720p,best", "480p,best", "1080p60,1080p,best", "best").pack(side="left", padx=6)
        self.demo = tk.BooleanVar(); tk.Checkbutton(r1, text="Demo (fake chat)", variable=self.demo, bg=PANEL, fg=FG, selectcolor=BG,
                                                    activebackground=PANEL, activeforeground=FG).pack(side="left", padx=10)
        tk.Checkbutton(r1, text="AI trim + title", variable=self.ai, bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG).pack(side="left")
        r2 = tk.Frame(m, bg=PANEL); r2.pack(fill="x", pady=2)
        tk.Label(r2, text="Credit on clips", bg=PANEL, fg=FG).pack(side="left")
        self.credit = tk.Entry(r2, width=16, bg=BG, fg=FG, insertbackground=FG, relief="flat"); self.credit.pack(side="left", padx=6, ipady=3)
        tk.Label(r2, text="  Storage limit (MB, 0 = none)", bg=PANEL, fg=FG).pack(side="left")
        self.limit = tk.StringVar(value="2000")
        tk.Entry(r2, width=7, textvariable=self.limit, bg=BG, fg=FG, insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        r3 = tk.Frame(m, bg=PANEL); r3.pack(fill="x", pady=2)
        tk.Label(r3, text="Anthropic API key (AI only, not saved)", bg=PANEL, fg=FG).pack(side="left")
        self.apikey = tk.StringVar(value=os.environ.get("ANTHROPIC_API_KEY", ""))
        tk.Entry(r3, width=30, textvariable=self.apikey, show="*", bg=BG, fg=FG, insertbackground=FG, relief="flat").pack(side="left", padx=6, ipady=3)
        r4 = tk.Frame(m, bg=PANEL); r4.pack(fill="x", pady=(2, 4))
        tk.Button(r4, text="Caption style...", bg=BG, fg=FG, relief="flat", command=self.open_style_editor).pack(side="left")
        tk.Button(r4, text="🎵 Trending sounds...", bg=BG, fg=FG, relief="flat", command=lambda: self.open_sounds()).pack(side="left", padx=8)
        tk.Button(r4, text="Try a video file...", bg=BG, fg=FG, relief="flat", command=self.try_file).pack(side="left")

        self.tabs_row = tk.Frame(r, bg=BG); self.tabs_row.pack(fill="x", padx=12, pady=(8, 0))
        self.tab = "All streams"; self.adding = False; self.stream_modes = {k: v for k, v in (load_settings().get("stream_modes") or {}).items() if v in TRIGGER_MODES}
        o6 = tk.Frame(r, bg=BG); o6.pack(fill="x", padx=12, pady=(4, 0))
        self.mode_lbl = tk.Label(o6, text="Take a clip when (all streams):", bg=BG, fg=FG); self.mode_lbl.pack(side="left")
        self.mode = tk.StringVar(value=TRIGGER_MODES[0]); self._mode_guard = False
        try: self.mode.trace_add("write", lambda *a: self.mode_changed())
        except Exception: pass
        purple_menu(o6, self.mode, *TRIGGER_MODES).pack(side="left", padx=6)
        tk.Button(o6, text="Reset", bg=PANEL, fg=FG, relief="flat", command=self.clear_override).pack(side="left", padx=(0, 8))
        sv = tk.Frame(r, bg=BG); sv.pack(fill="x", padx=12, pady=(8, 0))
        self.view_var = tk.StringVar(value="-"); self.view_box = tk.Frame(sv, bg=BG)       # (kept for the logic; not shown - the tabs do this job)
        self.allstats = tk.Label(r, text="", bg=BG, fg=FG, anchor="w", justify="left", font=("Consolas", 9)); self.allstats.pack(fill="x", padx=14)
        self.h_chat = tk.Label(r, text="CHAT HEAT", bg=BG, fg="#adadb8", font=("Segoe UI", 9, "bold")); self.h_chat.pack(anchor="w", padx=14, pady=(6, 0))
        self.meter = tk.Canvas(r, height=64, bg=PANEL, highlightthickness=0); self.meter.pack(fill="x", padx=12)
        self.stats = tk.Label(r, text="idle", bg=BG, fg="#adadb8", anchor="w", font=("Consolas", 10)); self.stats.pack(fill="x", padx=14, pady=4)
        self.h_loud = tk.Label(r, text="STREAMER HEAT (loudness vs. normal)", bg=BG, fg="#adadb8", font=("Segoe UI", 9, "bold")); self.h_loud.pack(anchor="w", padx=14)
        self.ameter = tk.Canvas(r, height=44, bg=PANEL, highlightthickness=0); self.ameter.pack(fill="x", padx=12)
        self.astats = tk.Label(r, text="audio: waiting...", bg=BG, fg="#adadb8", anchor="w", font=("Consolas", 10)); self.astats.pack(fill="x", padx=14, pady=2)
        det = Collapsible(r, "Show history graphs", bg=BG, pack=dict(fill="x", padx=12, pady=(0, 4)))
        self.h_hist = tk.Label(det.body, text="HEAT HISTORY (last 3 min)   cyan line = clip triggered", bg=BG, fg="#adadb8", font=("Segoe UI", 9, "bold")); self.h_hist.pack(anchor="w", padx=2)
        self.graph = tk.Canvas(det.body, height=100, bg=PANEL, highlightthickness=0); self.graph.pack(fill="x", pady=(0, 6))
        self.agraph = tk.Canvas(det.body, height=60, bg=PANEL, highlightthickness=0); self.agraph.pack(fill="x", pady=(0, 4))

        mid = tk.Frame(r, bg=BG); mid.pack(fill="both", expand=True, padx=12)
        self.chat = purple_scrolled_text(mid, height=10, bg=PANEL, fg=FG, relief="flat", state="disabled", wrap="word")
        self.chat.pack(side="left", fill="both", expand=True)
        self.clips = tk.Listbox(mid, bg=PANEL, fg=FG, relief="flat", width=46, highlightthickness=0)
        self.clips.pack(side="left", fill="y", padx=(8, 0))
        self.logbox = purple_scrolled_text(r, height=4, bg="#0e0e10", fg="#adadb8", relief="flat", state="disabled")
        self.logbox.pack(fill="x", padx=12, pady=8)

    def _show_settings(self, on):
        self.settings.is_open = bool(on)
        try:
            if on:
                self.settings.body.pack(fill="x", padx=(10, 0), pady=(2, 4))
                self.settings.box.pack(fill="x", padx=12, pady=6, after=self.set_btn.master)
            else: self.settings.box.pack_forget()
            self.set_btn.config(bg=PURPLE if on else PANEL, fg="white" if on else FG)
        except Exception: pass

    def _slider(self, parent, label, lo, hi, val, res):
        f = tk.Frame(parent, bg=PANEL); f.pack(side="left", expand=True, fill="x", padx=8, pady=4)
        tk.Label(f, text=label, bg=PANEL, fg=FG).pack(anchor="w")
        v = tk.DoubleVar(value=val)
        tk.Scale(f, from_=lo, to=hi, resolution=res, orient="horizontal", variable=v, bg=PANEL, fg=FG,
                 troughcolor=BG, highlightthickness=0, length=200).pack(fill="x")
        return v

    # ---------------- helpers ----------------
    def log(self, msg):
        self.q.put(("log", f"[{time.strftime('%H:%M:%S')}] {msg}"))

    def _append(self, box, text):
        box.config(state="normal"); box.insert("end", text + "\n"); box.see("end")
        if int(box.index("end-1c").split(".")[0]) > 600: box.delete("1.0", "100.0")
        box.config(state="disabled")

    # ---------------- per-stream tabs ----------------
    def tab_names(self):
        names = list(self.watchers)
        for c in self.parse_channels():
            if c not in names: names.append(c)
        return names

    def mode_for(self, channel):
        return self.stream_modes.get(channel) or self.default_mode

    @property
    def default_mode(self):
        return getattr(self, "_default_mode", TRIGGER_MODES[0])

    def refresh_tabs(self):
        names = self.tab_names()
        if self.tab != "All streams" and self.tab not in names: self.tab = "All streams"
        for c in self.tabs_row.winfo_children():
            try: c.destroy()
            except Exception: pass
        for name in ["All streams"] + names:
            on = name == self.tab; mark = " ◆" if name in self.stream_modes else ""
            b = tk.Button(self.tabs_row, text=name + mark, bg=PURPLE if on else PANEL, fg="white" if on else FG, relief="flat", padx=12, pady=3,
                          font=("Segoe UI", 9, "bold" if on else "normal"), command=lambda n=name: self.pick_tab(n))
            b.pack(side="left", padx=(0, 3))
            if name != "All streams":
                for ev in ("<Button-3>", "<Button-2>", "<Control-Button-1>"):
                    try: b.bind(ev, lambda e, n=name: self.tab_menu(e, n))
                    except Exception: pass
        if self.adding:
            e = tk.Entry(self.tabs_row, width=18, bg=PANEL, fg=FG, insertbackground=FG, relief="flat"); e.pack(side="left", ipady=4, padx=(2, 3))
            for ev, fn in (("<Return>", lambda ev, e=e: self.add_live(e.get())), ("<Escape>", lambda ev: self.cancel_add())):
                try: e.bind(ev, fn)
                except Exception: pass
            try: e.focus_set()
            except Exception: pass
            tk.Button(self.tabs_row, text="Add", bg=PURPLE, fg="white", relief="flat", padx=8, pady=2, command=lambda e=e: self.add_live(e.get())).pack(side="left")
            tk.Button(self.tabs_row, text="✕", bg=PANEL, fg=FG, relief="flat", padx=6, pady=2, command=self.cancel_add).pack(side="left", padx=3)
        elif len(self.watchers) < MAX_STREAMS:
            tk.Button(self.tabs_row, text="+", bg=PANEL, fg=FG, relief="flat", padx=10, pady=3, font=("Segoe UI", 10, "bold"),
                      command=self.begin_add).pack(side="left", padx=(0, 3))
        tk.Label(self.tabs_row, text="  click a streamer to see them (right-click to remove)", bg=BG, fg="#adadb8").pack(side="left")
        self.sync_mode_box()

    def begin_add(self):
        self.adding = True; self.refresh_tabs()

    def cancel_add(self):
        self.adding = False; self.refresh_tabs()

    def add_live(self, text):
        """The '+' on the tab row: start watching more streamers right now; the streams already running aren't touched."""
        chans = self.parse_channels(text)
        self.adding = False
        if not chans:
            if text.strip(): self.log("That doesn't look like a channel name.")
            return self.refresh_tabs()
        added = [c for c in chans if c not in self.watchers and self.add_watcher(c)]
        known = self.parse_channels()
        try: self.chan.delete(0, "end"); self.chan.insert(0, ", ".join(known + [c for c in added if c not in known]))
        except Exception: pass
        self.after_change()
        if added: self.pick_tab(added[-1])

    def pick_tab(self, name):
        old = self.tab; self.tab = name
        if name in self.watchers and name != self.view: self.set_view(name)      # the heat meters follow the tab you click
        elif (old == "All streams") != (name == "All streams"): self.refill_chat()
        self.refresh_tabs()

    def tab_menu(self, e, name):
        m = tk.Menu(self.root, tearoff=0, bg=PURPLE, fg="white", activebackground=PURPLE_HI, activeforeground="white", bd=0)
        m.add_command(label=f"Remove {name}", command=lambda: self.remove_tab(name))
        try: m.tk_popup(e.x_root, e.y_root)
        finally:
            try: m.grab_release()
            except Exception: pass

    def remove_tab(self, name):
        keep = [c for c in self.parse_channels() if c != name]
        try: self.chan.delete(0, "end"); self.chan.insert(0, ", ".join(keep))
        except Exception: pass
        self.stream_modes.pop(name, None); save_settings(stream_modes=self.stream_modes)
        if name in self.watchers: self.remove_watcher(name)
        else: self.refresh_tabs()

    # live chat: every stream's chat keeps flowing in the background, so switching tabs never leaves a gap
    def refill_chat(self):
        if self.tab == "All streams": lines = [f"[{ch}] {t}" for ch, t in list(self.all_lines)[-250:]]
        else:
            w = self.watchers.get(self.view); lines = list(w.lines)[-250:] if w else []
        try:
            self.chat.config(state="normal"); self.chat.delete("1.0", "end")
            if lines: self.chat.insert("end", "\n".join(lines) + "\n")
            self.chat.see("end"); self.chat.config(state="disabled")
        except Exception: pass

    def set_view(self, ch):
        self.view = ch; self.view_var.set(ch)
        for lbl, base in ((self.h_chat, "CHAT HEAT"), (self.h_hist, "HEAT HISTORY (last 3 min)   cyan line = clip triggered"), (self.h_loud, "STREAMER HEAT (loudness vs. normal)")):
            lbl.config(text=f"{base}  -  {ch.upper()}", fg=PURPLE_HI)
        w = self.watchers.get(ch)
        if w:
            for m in (w.sm, w.asm): m.v = m.hold = 0.0                         # no ghost of the previous stream's bar
        self.refill_chat()

    def sync_mode_box(self):
        self._mode_guard = True
        try:
            if self.tab == "All streams": self.mode.set(self.default_mode); self.mode_lbl.config(text="Take a clip when (all streams):")
            else: self.mode.set(self.mode_for(self.tab)); self.mode_lbl.config(text=f"Take a clip when ({self.tab}):")
        finally: self._mode_guard = False

    def mode_changed(self):
        if self._mode_guard: return
        v = self.mode.get()
        if self.tab == "All streams": self._default_mode = v
        else:
            self.stream_modes[self.tab] = v; save_settings(stream_modes=self.stream_modes); self.refresh_tabs()
            self.log(f"{self.tab}: will clip on '{v}'.")

    def clear_override(self):
        if self.tab != "All streams" and self.tab in self.stream_modes:
            del self.stream_modes[self.tab]; save_settings(stream_modes=self.stream_modes); self.refresh_tabs()
            self.log(f"{self.tab} now follows the 'all streams' setting.")

    def open_clips(self):
        os.makedirs(CLIPS_DIR, exist_ok=True)
        try: os.startfile(os.path.abspath(CLIPS_DIR))  # Windows
        except AttributeError:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", os.path.abspath(CLIPS_DIR)])

    # ---------------- start / stop ----------------
    def toggle(self):
        self.stop() if self.running else self.start()

    def parse_channels(self, text=None):
        out = []
        raw = self.chan.get() if text is None else text; raw = raw if isinstance(raw, str) else ""
        for tok in re.split(r"[,;\s]+", raw.strip()):
            m = re.search(r"twitch\.tv/([A-Za-z0-9_]+)", tok)
            ch = (m.group(1) if m else tok).lower().lstrip("#@")
            if ch and re.fullmatch(r"[a-z0-9_]{2,25}", ch) and ch not in out: out.append(ch)
        return out

    def start(self):
        chans = self.parse_channels()
        if self.demo.get() and not chans: chans = ["demo"]
        if not chans: return self.log("Enter one or more channel names or URLs (separated by commas), or tick Demo.")
        key = self.apikey.get().strip()
        if key: os.environ["ANTHROPIC_API_KEY"] = key
        if self.ai.get() and not os.environ.get("ANTHROPIC_API_KEY"):
            self.log("AI trim is ticked but no Anthropic API key was entered - clips will use chat timing only. Paste a key in the box or untick AI.")
        for ch in chans: self.add_watcher(ch)
        self.after_change()

    def add_from_entry(self):
        chans = self.parse_channels()
        if self.demo.get() and not chans: chans = ["demo"]
        if not chans: return self.log("Type a channel name in the box first.")
        for ch in chans: self.add_watcher(ch)
        self.after_change()

    def add_watcher(self, ch):
        if ch in self.watchers: return self.log(f"Already watching {ch}.")
        if len(self.watchers) >= MAX_STREAMS: return self.log(f"That's the limit: {MAX_STREAMS} streams at once.")
        w = Watcher(ch)
        if self.demo.get():
            threading.Thread(target=self.demo_chat, args=(w,), daemon=True).start()
            threading.Thread(target=self.demo_audio, args=(w,), daemon=True).start()
            self.log(f"DEMO mode ({ch}): fake chat with random hype bursts (no video, so no clips are cut).")
        else:
            try:
                w.buf = StreamBuffer(buf_dir=os.path.join("buffer", ch), log=lambda m, ch=ch: self.log(f"[{ch}] {m}"),
                                     on_audio=lambda t, db, ch=ch: self.q.put(("audio", ch, t, db)))
                w.buf.start(ch, self.quality.get())
            except Exception as e:
                return self.log(f"Could not start the recorder for {ch}: {e}")
            threading.Thread(target=self.irc, args=(w,), daemon=True).start()
            self.log(f"Watching {ch}: recording buffer + reading chat...  Clips are saved to: {os.path.abspath(CLIPS_DIR)}")
        self.watchers[w.channel] = w
        if self.view is None: self.set_view(ch)
        if len(self.watchers) >= 3 and "720p60" in self.quality.get() and not self.demo.get():
            self.log("Tip: with 3+ streams, a lower quality (480p) in the drop-down keeps your CPU and internet from struggling. It only affects new streams.")
        return True

    def remove_view(self):
        if self.view: self.remove_watcher(self.view)

    def remove_watcher(self, ch):
        w = self.watchers.pop(ch, None)
        if not w: return
        w.stop(); self.log(f"Stopped watching {ch}.")
        if self.view == ch:
            nxt = next(iter(self.watchers), None)
            if nxt: self.set_view(nxt)
            else: self.view = None; self.clear_chat()
        self.after_change()

    def after_change(self):
        self.refresh_tabs()
        self.running = bool(self.watchers)
        self.btn.config(text="■  STOP" if self.running else "▶  START", bg=RED if self.running else PURPLE)
        for c in self.view_box.winfo_children():
            try: c.destroy()
            except Exception: pass
        if self.watchers:
            self.view_var.set(self.view or "-")
            purple_menu(self.view_box, self.view_var, *self.watchers.keys(), command=self.pick_view).pack(side="left")
        else: self.allstats.config(text="")

    def pick_view(self, ch=None):
        ch = ch or self.view_var.get()
        if ch in self.watchers and ch != self.view:
            self.set_view(ch)
            if self.tab != "All streams": self.tab = ch; self.refresh_tabs()

    def clear_chat(self):
        try: self.chat.config(state="normal"); self.chat.delete("1.0", "end"); self.chat.config(state="disabled")
        except Exception: pass

    def stop(self):
        for w in list(self.watchers.values()): w.stop()
        self.watchers.clear(); self.view = None; self.after_change(); self.log("Stopped.")

    # ---------------- chat sources (threads -> queue) ----------------
    def irc(self, w):
        channel = w.channel
        try:
            s = socket.create_connection(("irc.chat.twitch.tv", 6667), timeout=5); s.settimeout(1)
            for ln in ("PASS SCHMOOPIIE", "NICK justinfan%d" % random.randint(10000, 99999), f"JOIN #{channel}"):
                s.send((ln + "\r\n").encode())
            buf = ""
            while not w.stop_ev.is_set():
                try: buf += s.recv(4096).decode("utf-8", "ignore")
                except socket.timeout: continue
                *lines, buf = buf.split("\r\n")
                for ln in lines:
                    if ln.startswith("PING"): s.send(b"PONG :tmi.twitch.tv\r\n")
                    elif "PRIVMSG" in ln:
                        self.q.put(("chat", channel, time.time(), ln.split("!", 1)[0].lstrip(":"), ln.split("PRIVMSG", 1)[1].split(":", 1)[1]))
        except Exception as e:
            self.log(f"[{channel}] Chat connection error: {e}")

    def demo_chat(self, w):
        words = ["hello", "lol", "nice", "gg", "?", "true", "first time here", "what game is this"]
        hype = ["KEKW", "OMEGALUL", "POGGERS", "NO WAY", "CLIP IT", "LMAOOO", "W", "INSANE", "WTF"]
        t0, next_spike, spike_end = time.time(), time.time() + random.randint(30, 60), 0
        while not w.stop_ev.is_set():
            now = time.time()
            if now > next_spike: spike_end = now + 12; next_spike = now + random.randint(80, 130)
            rate = 2.0 + random.random()
            if now < spike_end: rate += 28 * min(1, (spike_end - now) / 4, (12 - (spike_end - now)) / 2 + 0.3)
            n = int(rate * 0.1) + (random.random() < (rate * 0.1) % 1)
            for _ in range(n):
                txt = random.choice(hype if now < spike_end and random.random() < .8 else words)
                self.q.put(("chat", w.channel, now, f"viewer{random.randint(1, 400)}", txt))
            time.sleep(0.1)

    def demo_audio(self, w):
        """Fake stream loudness: talking around -30 dB with an occasional loud scream."""
        next_b, b_end = time.time() + random.randint(50, 90), 0
        while not w.stop_ev.is_set():
            now = time.time()
            if now > next_b: b_end = now + 4; next_b = now + random.randint(90, 150)
            db = -30 + random.gauss(0, 2.5) + (16 if now < b_end else 0)
            self.q.put(("audio", w.channel, now, db)); time.sleep(0.1)

    # ---------------- main loop ----------------
    def poll(self):
        try:
            while True:
                ev = self.q.get_nowait()
                if ev[0] == "chat":
                    _, ch, t, u, txt = ev; w = self.watchers.get(ch)
                    if not w: continue
                    w.det.add(t, u, txt); w.chat_log.append((t, u, txt))
                    line = f"{u}: {txt}"; w.lines.append(line); self.all_lines.append((ch, line))
                    if self.tab == "All streams": self._append(self.chat, f"[{ch}] {line}")        # merged live feed of every stream
                    elif ch == self.view: self._append(self.chat, line)
                elif ev[0] == "audio":
                    w = self.watchers.get(ev[1])
                    if w: w.aud.add(ev[2], ev[3])
                elif ev[0] == "log": self._append(self.logbox, ev[1])
                elif ev[0] == "clip": self.clips.delete(ev[1]); self.clips.insert(ev[1], ev[2])
                elif ev[0] == "review": self.enqueue_review(ev[1])
                elif ev[0] == "call": ev[1]()
        except queue.Empty:
            pass
        now = time.time()
        if self.running and now - self.last_tick >= 0.5:
            self.last_tick = now; self.tick(now)
        self.root.after(100, self.poll)

    def tick(self, now):
        lines = []
        for ch, w in list(self.watchers.items()):
            fire, why = self.tick_one(w, now)
            r, ar = w.r, w.ar
            lines.append(f"{'▶' if ch == self.view else ' '} {ch[:18]:<18} chat {r['rate']/w.det.window:4.1f}/s  score {r['score']:.1f}/{w.det.trigger:.1f}  "
                         f"loud {ar['rise']:+5.1f} dB  buffer {(w.buf.buffered if w.buf else 0):3.0f}s  clips {w.clips}" + ("  🔥" if fire else ""))
            if fire: self.on_trigger(w, now, r["score"], why)
        self.allstats.config(text="\n".join(lines) if len(lines) > 1 else "")
        w = self.watchers.get(self.view)
        if not w: return
        r, ar, d, a = w.r, w.ar, w.det, w.aud
        buf = f"buffer {w.buf.buffered:4.0f}s" if w.buf else "buffer  n/a"
        state = "CALIBRATING baseline..." if r["warming"] else f"score {r['score']:.1f} / {d.trigger:.1f}"
        self.refresh_size(); lim = self.limit_mb()
        size = f"clips {self.used_mb:.0f}/{lim:.0f} MB" if lim > 0 else f"clips {self.used_mb:.0f} MB"
        if lim > 0 and self.used_mb >= lim: size += " ⛔ PAUSED"
        self.stats.config(text=f"{w.channel}: {r['rate']/d.window:5.1f} msg/s (normal {r['base_rate']:.1f})   {r['uniq']:3d} chatters   "
                               f"{r['hype']:3d} hype msgs   {state}   {buf}   saved: {self.clip_n}   {size}")
        if ar["silent"]: atxt = "audio: no signal yet (or stream is muted)"
        elif ar["warming"]: atxt = f"audio {ar['db']:5.1f} dB   CALIBRATING normal volume..."
        else: atxt = f"audio {ar['db']:5.1f} dB   normal {ar['base_db']:5.1f} dB   {ar['rise']:+5.1f} dB   (clips at +{a.rise:.1f} dB)"
        self.astats.config(text=atxt)
        self.draw_graph(None, w.hist); self.draw_graph(self.agraph, w.ahist, 70)

    def render(self):
        """~25 fps: the two big meters glide smoothly instead of jumping every half second."""
        try:
            w = self.watchers.get(self.view)
            if w and w.r:
                now = time.time(); dt = min(max(now - w.rt, 0.001), 0.2); w.rt = now
                d, h = w.sm.frame(dt, now); self.draw_meter(None, d, h)
                d, h = w.asm.frame(dt, now); self.draw_meter(self.ameter, d, h, 44, "LOUD")
        except Exception: pass
        self.root.after(40, self.render)

    def tick_one(self, w, now):
        """Run one stream's detectors. Returns (fire, why); each stream has its own cooldown."""
        d, a = w.det, w.aud
        d.trigger, d.cooldown = self.trig.get(), 0.0          # cooldown is handled below, per stream
        a.rise, a.cooldown = self.loud.get(), 0.0
        r, ar = d.tick(now), a.tick(now); w.r, w.ar = r, ar
        lvl, alvl = r["level"], ar["level"]
        w.sm.feed(lvl, now); w.asm.feed(alvl, now)               # (display only - detection below uses the exact values)
        if r["trigger"] or (lvl >= 1.0 and r["rate"] >= d.min_msgs): w.chat_hot_t = now
        if ar["trigger"] or (alvl >= 1.0 and not ar["warming"] and ar["db"] > a.floor_db): w.aud_hot_t = now
        mode = self.mode_for(w.channel)
        chat_f, aud_f = now - w.chat_hot_t < 1.0, now - w.aud_hot_t < 1.0
        if mode == TRIGGER_MODES[1]: fire, why = chat_f, "CHAT PEAK"
        elif mode == TRIGGER_MODES[2]: fire, why = aud_f, "STREAMER LOUD"
        elif mode == TRIGGER_MODES[3]:    # both within 20 s of each other
            fire = (now - w.chat_hot_t < 20) and (now - w.aud_hot_t < 20) and (chat_f or aud_f); why = "CHAT PEAK + STREAMER LOUD"
        else: fire, why = chat_f or aud_f, ("STREAMER LOUD" if aud_f and not chat_f else "CHAT PEAK" if chat_f and not aud_f else "CHAT PEAK + STREAMER LOUD")
        fire = fire and now - w.last_clip_t >= self.cool.get()
        if fire: w.last_clip_t = now
        w.hist.append((lvl, fire)); w.ahist.append((alvl, fire))
        return fire, why

    def draw_meter(self, c=None, disp=None, hold=None, H=64, label="CLIP"):
        c = c or self.meter; disp = 0.0 if disp is None else disp; hold = 0.0 if hold is None else hold
        c.delete("all"); W = c.winfo_width(); y0, y1 = 6, H - 6
        c.create_rectangle(0, y0, W, y1, fill=PANEL, outline="")
        full = min(disp, SCALE) / SCALE * W; x = 0
        for lo, hi, col in ((0, 0.5, GREEN), (0.5, 1.0, YELLOW), (1.0, SCALE, RED)):          # one solid bar, coloured by how hot it is
            a, b = lo / SCALE * W, hi / SCALE * W
            if full > a: c.create_rectangle(a, y0, min(full, b), y1, fill=col, outline="")
        hx = min(hold, SCALE) / SCALE * W
        c.create_rectangle(hx - 2, 2, hx + 2, H - 2, fill="white", outline="")
        tx = W / SCALE
        c.create_line(tx, 0, tx, H, fill="white", dash=(3, 3))
        c.create_text(tx + 6, 10, text=label, fill="white", anchor="w", font=("Segoe UI", 8, "bold"))

    def draw_graph(self, c=None, hist=None, H=120):
        c = c or self.graph; hist = [] if hist is None else hist
        c.delete("all"); W = c.winfo_width(); bw = W / max(1, hist.maxlen if hasattr(hist, "maxlen") and hist.maxlen else len(hist)); top = 2.0
        sm = smooth_hist(hist)
        for i, ((lvl, mk), v) in enumerate(zip(hist, sm)):
            h = min(v, top) / top * (H - 6)
            col = GREEN if v < 0.5 else YELLOW if v < 1.0 else RED
            x = i * bw
            c.create_rectangle(x, H - h, x + bw + 0.6, H, fill=col, outline="")
            if mk: c.create_line(x, 0, x, H, fill="#00e5ff", width=2)
        ty = H - 1.0 / top * (H - 6)
        c.create_line(0, ty, W, ty, fill="white", dash=(4, 4))

    # ---------------- review queue / processing ----------------
    def current_opts(self, channel=""):
        saved = self.recall_cam(channel) if self.layout_value() == "facecam" else None
        return dict(vertical=self.vert.get(), layout=self.layout_value(), cam=saved, captions=self.cap.get(), ai=self.ai.get(),
                    credit=self.credit.get().strip(), hook="", style=self.caption_style,
                    blurs=self.recall_blurs(channel), camq=int(load_settings().get("camq", 1)))

    def layout_value(self):
        return LAYOUTS.get(self.layout_choice.get(), "blur")

    def recall_cam(self, channel):
        c = load_settings().get("cams", {}).get(channel or "_")
        return c if isinstance(c, dict) and all(k in c for k in ("x", "y", "w", "h")) else None

    def recall_blurs(self, channel):
        b = load_settings().get("blurs", {}).get(channel or "_", [])
        return [x for x in b if isinstance(x, dict) and all(k in x for k in ("x", "y", "w", "h"))] if isinstance(b, list) else []

    def remember_blurs(self, channel, blurs):
        d = load_settings().get("blurs", {}); d[channel or "_"] = [{k: round(b[k], 5) for k in ("x", "y", "w", "h")} for b in blurs]
        save_settings(blurs=d); self.log("Blur boxes saved for " + (channel or "this stream") + ".")

    def remember_cam(self, channel, cam):
        cams = load_settings().get("cams", {}); cams[channel or "_"] = {k: round(cam[k], 5) for k in ("x", "y", "w", "h")}
        save_settings(cams=cams); self.log("Facecam box saved for " + (channel or "this stream") + ".")

    def finish_clip(self, res, raw, idx, stamp):
        if res["final"] != raw and os.path.exists(res["final"]):
            safe_remove(raw)                              # keep only the finished clip (saves disk space)
        self.clip_n += 1; self.refresh_size(force=True)
        ai = res.get("ai"); label = f"  AI {ai['score']}/10 {ai['title']}" if ai else ""
        self.q.put(("clip", idx, f"{stamp}  ✔ {os.path.basename(res['final'])}{label}"))
        self.log(f"Saved {res['final']} ({res['dur']:.0f}s). Clips folder: {self.used_mb:.0f}"
                 + (f" / {self.limit_mb():.0f} MB" if self.limit_mb() > 0 else " MB"))

    # ---------------- destination folder + sounds ----------------
    def apply_dest(self, path):
        path = os.path.abspath(os.path.expanduser(str(path).strip().strip('"')))
        if path == os.path.abspath(CLIPS_DIR): return True
        try: set_clips_dir(path)
        except OSError as e:
            self.log(f"Can't use that folder ({e}). Still saving to {os.path.abspath(CLIPS_DIR)}")
            self.dest.set(os.path.abspath(CLIPS_DIR)); return False
        self.dest.set(path); self.refresh_size(force=True); self.limit_hit = False
        save_settings(clips_dir=path); self.log(f"Clips will now be saved to: {path}"); return True

    def browse_dest(self):
        p = filedialog.askdirectory(title="Choose where finished clips are saved", initialdir=os.path.abspath(CLIPS_DIR))
        if p: self.apply_dest(p)

    def new_dest(self):
        parent = filedialog.askdirectory(title="Pick the folder to create the new clips folder inside")
        if not parent: return
        name = simpledialog.askstring("New folder", "Name for the new clips folder:", parent=self.root)
        name = re.sub(r'[<>:"/\\|?*]', "", name or "").strip()
        if name: self.apply_dest(os.path.join(parent, name))

    def open_sounds(self, on_pick=None):
        if self.sounds_win: self.sounds_win.close()
        self.sounds_win = SoundsWindow(self, on_pick)

    def open_style_editor(self):
        if self.style_editor: return self.style_editor.top.lift()
        self.style_editor = CaptionEditor(self, self.caption_style)

    def enqueue_review(self, item):
        self.review_q.append(item)
        if self.review_win is None: self.open_next_review()
        else: self.log(f"{len(self.review_q)} more clip(s) waiting for your review.")

    def open_next_review(self):
        if self.review_win is None and self.review_q:
            item = self.review_q.popleft()
            self.review_win = ReviewWindow(self, item, len(self.review_q))

    def open_studio(self):
        """The editing studio, without waiting for a clip: a window you drag video files into (or Browse). They open in the editor."""
        if self.review_win is not None: return self.review_win.top.lift()
        if getattr(self, "studio_win", None):
            try: return self.studio_win.top.lift()
            except Exception: pass
        self.studio_win = StudioDrop(self)

    def studio_open(self, paths):
        """Files dragged in / browsed: the first opens in the editor, the rest are added after it. If the editor is already open they're added to it."""
        paths = [x for x in paths if x and os.path.isfile(x)]
        if not paths: return
        if self.review_win is not None: return self.review_win.add_paths(paths)
        w = getattr(self, "studio_win", None)
        if w:
            try: w.top.destroy()
            except Exception: pass
            self.studio_win = None
        os.makedirs(CLIPS_DIR, exist_ok=True); stamp = time.strftime("%H:%M:%S"); idx = self.clips.size()
        dst = os.path.join(CLIPS_DIR, f"studio_{time.strftime('%H%M%S')}_raw.mp4")
        self.clips.insert("end", f"{stamp}  (studio) {os.path.basename(paths[0])[:30]} preparing...")
        def work():
            try:
                sh("ffmpeg", "-y", "-v", "error", "-i", paths[0], "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-pix_fmt", "yuv420p",
                   "-c:a", "aac", "-movflags", "+faststart", dst)
                item = dict(raw=dst, dur=duration(dst), msgs=[], idx=idx, stamp=stamp, score=0.0, channel="", pending_add=paths[1:])
                self.q.put(("clip", idx, f"{stamp}  (studio) {os.path.basename(paths[0])[:30]} 🎬 open in the editor")); self.q.put(("review", item))
            except Exception as e:
                self.log(f"Could not open {os.path.basename(paths[0])}: {(getattr(e, 'stderr', b'') or b'').decode('utf-8', 'ignore')[-150:] or e}")
        threading.Thread(target=work, daemon=True).start()
        self.log("Getting the video ready for the editor (your original file isn't changed)...")

    def try_file(self):
        """Open the review window on any video file (first 90 s) - handy to test vertical/captions without waiting for a spike."""
        path = filedialog.askopenfilename(title="Pick a video to try the review window",
                                          filetypes=[("Video", "*.mp4 *.mkv *.mov *.ts *.webm"), ("All files", "*.*")])
        if not path: return
        os.makedirs(CLIPS_DIR, exist_ok=True); stamp = time.strftime("%H:%M:%S")
        dst = os.path.join(CLIPS_DIR, f"test_{time.strftime('%H%M%S')}_raw.mp4"); idx = self.clips.size()
        self.clips.insert("end", f"{stamp}  (test file) preparing...")
        def work():
            try:
                sh("ffmpeg", "-y", "-i", path, "-t", "90", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-c:a", "aac", dst)
                item = dict(raw=dst, dur=duration(dst), msgs=[], idx=idx, stamp=stamp, score=0.0)
                self.q.put(("clip", idx, f"{stamp}  (test file) 👀 waiting for review")); self.q.put(("review", item))
            except Exception as e:
                self.log(f"Could not open that file: {e}")
        threading.Thread(target=work, daemon=True).start()

    # ---------------- storage limit ----------------
    def limit_mb(self):
        try: return max(0.0, float(str(self.limit.get()).strip() or 0))
        except ValueError: return 0.0

    def refresh_size(self, force=False):
        if force or time.time() - self._size_t > 5:
            self.used_mb = folder_mb(CLIPS_DIR); self._size_t = time.time()

    def limit_reached(self):
        self.refresh_size(); lim = self.limit_mb()
        return lim > 0 and self.used_mb >= lim

    # ---------------- clipping ----------------
    def on_trigger(self, w, now, score, why="CHAT PEAK"):
        tag = f"[{w.channel}] " if len(self.watchers) > 1 or w.channel != "demo" else ""
        if self.limit_reached():
            if not self.limit_hit:
                self.limit_hit = True
                self.log(f"⛔ Storage limit reached ({self.used_mb:.0f} / {self.limit_mb():.0f} MB). Clipping is paused; the meters keep running. "
                         "Raise the limit or delete clips to resume.")
            return
        self.limit_hit = False
        pre, post = int(self.pre.get()), int(self.post.get())
        idx = self.clips.size(); stamp = time.strftime("%H:%M:%S")
        self.clips.insert("end", f"{stamp}  {tag}score {score:.1f}  ⏳ waiting {post}s...")
        self.log(f"🔥 {tag}{why} (score {score:.1f}). Clip = {pre}s before → {post}s after.")
        if w.buf is None:
            self.q.put(("clip", idx, f"{stamp}  {tag}score {score:.1f}  (demo: no video)")); self.clip_n += 1; w.clips += 1; return
        self.root.after(post * 1000, lambda: threading.Thread(target=self.make_clip, args=(w, now, pre, post, score, idx, stamp, tag), daemon=True).start())

    def make_clip(self, w, trig_wall, pre, post, score, idx, stamp, tag=""):
        if self.limit_reached():
            self.q.put(("clip", idx, f"{stamp}  {tag}⛔ skipped (storage limit)")); return
        try:
            os.makedirs(CLIPS_DIR, exist_ok=True)
            raw = os.path.join(CLIPS_DIR, f"clip_{w.channel}_{time.strftime('%H%M%S', time.localtime(trig_wall))}_raw.mp4")
            if w.buf is None: raise RuntimeError("that stream was removed before the clip could be cut")
            raw, dur = w.buf.cut(trig_wall - pre, trig_wall + post, raw)
            w.clips += 1
            msgs = [{"t": t - (trig_wall - pre), "user": u, "text": x} for t, u, x in list(w.chat_log) if trig_wall - pre <= t <= trig_wall + post]
            if self.review.get():                    # park it: you preview it, then decide (no credits spent yet)
                self.q.put(("clip", idx, f"{stamp}  {tag}score {score:.1f}  👀 waiting for your review"))
                self.q.put(("review", dict(raw=raw, dur=dur, msgs=msgs, idx=idx, stamp=stamp, score=score, channel=w.channel)))
                self.log(f"{tag}Clip cut and waiting for your review. Nothing has been sent to the AI.")
                return
            self.q.put(("clip", idx, f"{stamp}  {tag}score {score:.1f}  ⚙ processing..."))
            res = process_clip(raw, raw.replace("_raw.mp4", "_final.mp4"), self.current_opts(w.channel), msgs, self.log)
            self.finish_clip(res, raw, idx, stamp)
        except Exception as e:
            self.q.put(("clip", idx, f"{stamp}  {tag}✖ failed")); self.log(f"{tag}Clip failed: {e}")

def doctor(argv=None):
    """python clip_studio.py doctor [--load-model]   -> checks everything this app needs and says how to fix what's missing."""
    import importlib.util
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    problems = []
    def good(msg): print(f"  ✔ {msg}")
    def bad(msg, fix): problems.append(msg); print(f"  ✖ {msg}\n      fix: {fix}")
    print(f"\nclip_studio doctor  (Python {sys.version.split()[0]})\n")
    if shutil.which("ffmpeg"):
        flt = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True).stdout
        good(f"ffmpeg found: {shutil.which('ffmpeg')}")
        good("ffmpeg can burn captions (ass filter)") if re.search(r"\bass\b", flt) else bad("this ffmpeg can't burn captions (no 'ass' filter)", "install a full ffmpeg build (Windows: winget install Gyan.FFmpeg)")
    else:
        bad("ffmpeg not found", "python -m pip install imageio-ffmpeg   (or Windows: winget install Gyan.FFmpeg, then reopen)")
    for mod, fix in (("faster_whisper", "python -m pip install faster-whisper   (needed for captions)"),
                     ("anthropic", "python -m pip install anthropic   (only for AI trim/title)"),
                     ("streamlink", "python -m pip install streamlink   (only for live recording)"),
                     ("tkinterdnd2", "python -m pip install tkinterdnd2   (only for dragging files into the editing studio; the Browse button works without it)"),
                     ("yt_dlp", "python -m pip install yt-dlp   (only for downloading VOD pieces)")):
        good(f"{mod} installed") if importlib.util.find_spec(mod) else bad(f"{mod} missing", fix)
    try:
        from importlib import metadata
        vers = {}
        for pkg in ("faster-whisper", "av", "ctranslate2"):
            try: vers[pkg] = metadata.version(pkg)
            except Exception: pass
        if vers: print("      versions: " + ", ".join(f"{k} {v}" for k, v in vers.items()) + "   (if captions ever fail with 'metadata_errors':  python -m pip install -U av faster-whisper)")
    except Exception: pass
    good("OpenCV face models found (facecam auto-detect works)") if _face_detector() else bad("facecam auto-detect unavailable (OpenCV face models not found)", "python -m pip install opencv-python   (you can still place the facecam box by hand)")
    good("tkinter (GUI) available") if tk else bad("tkinter missing - GUI won't open", "Windows/Mac Python includes it; Linux: sudo apt install python3-tk")
    good("ANTHROPIC_API_KEY is set") if os.environ.get("ANTHROPIC_API_KEY") else print("  - ANTHROPIC_API_KEY not set (only needed for AI trim; you can paste it in the GUI)")
    good("TwitchDownloaderCLI found") if shutil.which("TwitchDownloaderCLI") else print("  - TwitchDownloaderCLI not found (optional; the built-in downloader is used instead)")
    if "--load-model" in (argv or []):
        print(f"\n  Loading the Whisper '{WHISPER['model']}' model (first time downloads it - this can take a few minutes)...")
        try: get_whisper(); good("Whisper model loaded - captions will work")
        except CaptionError as e: bad("Whisper model failed to load", str(e))
    else:
        print("\n  (add --load-model to also download/load the Whisper model now)")
    print("\nAll good!" if not problems else f"\n{len(problems)} thing(s) to fix above.")


def captions_cli(argv):
    """python clip_studio.py captions myvideo.mp4   -> transcribes the first 30 s, prints the timed words, writes captions_test.mp4"""
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    ap = argparse.ArgumentParser(prog="clip_studio.py captions", description=captions_cli.__doc__)
    ap.add_argument("video"); ap.add_argument("--seconds", type=int, default=30); ap.add_argument("--preset", default="Bold Yellow", choices=list(CAPTION_PRESETS))
    ap.add_argument("--model", default="small"); ap.add_argument("--language"); ap.add_argument("--landscape", action="store_true")
    ap.add_argument("--out", default="captions_test.mp4")
    a = ap.parse_args(argv); WHISPER.update(model=a.model, language=a.language)
    src = "captions_test_src.mp4"
    sh("ffmpeg", "-y", "-i", a.video, "-t", str(a.seconds), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "20", "-c:a", "aac", src)
    dur = duration(src); print(f"Transcribing {dur:.0f}s locally with Whisper '{a.model}' (first run downloads the model)...")
    try: words = transcribe(src, 0, dur)
    except CaptionError as e: sys.exit(f"\n✖ {e}")
    if not words: sys.exit("\nNo speech was detected in that part of the video.")
    print(f"\n{len(words)} words found:\n")
    for w in words[:60]: print(f"  {w['s']:6.2f}s  {w['w']}")
    style = {**CAPTION_DEFAULT, **CAPTION_PRESETS[a.preset]}
    render(src, 0, dur, words, a.out, not a.landscape, "blur", "", "", style)
    safe_remove(src); print(f"\n✔ Wrote {a.out}  (preset: {a.preset}). Open it to check the captions.")


def facecam_cli(argv):
    """python clip_studio.py facecam stream.mp4  -> finds the facecam, prints the box, writes facecam_debug.png (box drawn) and facecam_test.mp4 (9:16 result)."""
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception: pass
    ap = argparse.ArgumentParser(prog="clip_studio.py facecam", description=facecam_cli.__doc__)
    ap.add_argument("video"); ap.add_argument("--start", type=float, default=0); ap.add_argument("--seconds", type=float, default=30)
    a = ap.parse_args(argv); end = min(duration(a.video), a.start + a.seconds)
    cam = detect_facecam(a.video, a.start, end)
    if not cam: sys.exit("\nNo facecam found (see the message above).")
    print(f"\nFacecam found: x={cam['x']:.3f} y={cam['y']:.3f} w={cam['w']:.3f} h={cam['h']:.3f}  (fractions of the screen; seen in {cam['hits']}/{cam['samples']} frames)")
    print(f"For clip mode you can pass it by hand:  --layout facecam --cam {cam['x']:.3f},{cam['y']:.3f},{cam['w']:.3f},{cam['h']:.3f}")
    sh("ffmpeg", "-y", "-ss", str(a.start + (end - a.start) / 2), "-i", a.video, "-frames:v", "1", "-vf", draw_cam_vf(cam), "facecam_debug.png")
    render(a.video, a.start, min(end, a.start + 8), [], "facecam_test.mp4", True, "facecam", "", "", None, cam)
    print("Wrote facecam_debug.png (red box = detected cam) and facecam_test.mp4 (the 9:16 result). Open them to check.")


# ============================================================================
# ENTRY POINT
# ============================================================================
def chat_cli(argv):
    mode, target, out = argv[:3]
    m = download_vod_chat(target) if mode == "vod" else record_live_chat(target, out)
    json.dump(m, open(out, "w"))
    print(f"saved {len(m)} messages -> {out}")


def run_gui():
    if tk is None:
        sys.exit("tkinter isn't installed. Install it (Linux: sudo apt install python3-tk) or use the CLI: python clip_studio.py clip --help")
    try: root = TkinterDnD.Tk() if HAVE_DND else tk.Tk()
    except Exception: root = tk.Tk()
    app = App(root)
    root.protocol("WM_DELETE_WINDOW", lambda: (app.stop(), root.destroy()))
    root.mainloop()


if __name__ == "__main__":
    setup_workdir()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "gui"
    if cmd == "gui": run_gui()
    elif cmd == "clip": cli_main(sys.argv[2:])
    elif cmd == "chat": chat_cli(sys.argv[2:])
    elif cmd == "doctor": doctor(sys.argv[2:])
    elif cmd == "captions": captions_cli(sys.argv[2:])
    elif cmd == "facecam": facecam_cli(sys.argv[2:])
    else: sys.exit(__doc__)
