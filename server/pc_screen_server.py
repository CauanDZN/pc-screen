#!/usr/bin/env python3
"""Mirror this PC's screen (and its audio) to the TV app, over plain HTTP. No login by default,
LAN only, nothing persistent — start it when you want to watch, Ctrl+C when done.

    python pc_screen_server.py
    python pc_screen_server.py --token minhasenha      # require ?token=minhasenha in every URL

Open http://<this PC's IP>:8090/ in any browser: a control panel to change FPS, JPEG quality,
video scale, audio bitrate and audio buffer live, without restarting this script. The same page
also has the two URLs the TV app needs.

Windows will ask to allow this through the firewall the first time it runs — allow it for private
networks.

Video: MJPEG (one JPEG per frame over one long HTTP response). <img src="...mjpg"> has decoded that
natively since the Netscape days, so the TV app needs no video-decoding JS at all, and each frame
stands alone — no decode buffer adding its own latency. Why not H.264/WebRTC like Sunshine+Moonlight:
this TV's browser is from 2019 (webOS 4, Chromium 53), and a real low-latency WebRTC pipeline needs a
GPU encoder, a signalling server and a browser new enough to negotiate it reliably.

Audio: the same idea, one continuous MP3 stream over HTTP — the same trick internet radio has used
for decades, and <audio src="...mp3"> plays it with no JS either. Video and audio are two
independent HTTP connections with no shared clock, so they are not frame-synced; --audio-delay-ms
(also in the control panel) is a blunt manual offset for when audio and picture noticeably drift,
same idea as a soundbar's "audio delay" setting. --audio-buffer-ms trades latency for smoothness:
raise it if audio stutters, lower it if audio feels laggy.

Needs: pip install pillow soundcard lameenc numpy (pillow is already installed if you ran
backup.py earlier). soundcard needs a real Windows/macOS audio device, so it is only imported when
a browser actually asks for /audio.mp3 — the video-only path doesn't need it installed at all.
"""
import argparse
import collections
import dataclasses
import io
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
BOUNDARY = "lgtvframe"


# ---- shared, live-editable settings ------------------------------------------------------------

@dataclasses.dataclass
class Settings:
    fps: float = 30.0
    quality: int = 70       # JPEG 1-95
    scale: float = 1.0      # 0.1-1.0
    audio_bitrate: int = 128   # kbps: 64/96/128/192/256/320
    audio_buffer_ms: int = 150  # 30-1000
    audio_delay_ms: int = 0     # -2000..2000, informational: the TV app reads this via /config

    LIMITS = {
        "fps": (1, 60),
        "quality": (1, 95),
        "scale": (0.1, 1.0),
        "audio_bitrate": (32, 320),
        "audio_buffer_ms": (30, 1000),
        "audio_delay_ms": (-5000, 5000),
    }

    def as_dict(self):
        return dataclasses.asdict(self)

    def update(self, patch):
        """Validates a dict of changes and only then applies them — all or nothing, so one bad
        field in a patch can't leave an earlier field in that same patch half-applied."""
        validated = {}
        for key, value in patch.items():
            if key not in self.LIMITS:
                raise ValueError(f"campo desconhecido: {key}")
            lo, hi = self.LIMITS[key]
            try:
                value = type(getattr(self, key))(value)
            except (TypeError, ValueError):
                raise ValueError(f"{key}: valor invalido")
            if not (lo <= value <= hi):
                raise ValueError(f"{key}: precisa estar entre {lo} e {hi}")
            validated[key] = value
        for key, value in validated.items():
            setattr(self, key, value)


class SharedSettings:
    """Settings plus a lock, so the streaming loops (one per connected client, each its own
    thread) and the /config endpoint can touch it concurrently without tearing a read in half."""

    def __init__(self, initial):
        self._s = initial
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return dataclasses.replace(self._s)

    def update(self, patch):
        with self._lock:
            self._s.update(patch)  # raises before mutating anything if any field is invalid
            return dataclasses.replace(self._s)


# ---- video: capture + encode (same shape as before, now reading live settings) -----------------

def capture_frame(bbox=None):
    """Grab the screen as a PIL Image. Swapped out in tests; needs ImageGrab (Windows/macOS)."""
    from PIL import ImageGrab
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def jpeg_bytes(img, quality):
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


# ---- audio: capture + encode ---------------------------------------------------------------

def open_loopback_recorder(samplerate=48000):
    """Opens a WASAPI loopback recorder for the current default speaker (Windows/macOS via
    `soundcard`). Imported lazily: a video-only session never needs this installed."""
    import soundcard as sc
    speaker = sc.default_speaker()
    mic = sc.get_microphone(speaker.name, include_loopback=True)
    return mic.recorder(samplerate=samplerate)


def capture_audio_chunk(recorder, numframes):
    """Records `numframes` samples as a float32 numpy array (frames, channels). Swapped out in
    tests so they don't need a real audio device."""
    return recorder.record(numframes=numframes)


def pcm16_bytes(float_chunk):
    import numpy as np
    clipped = np.clip(float_chunk, -1.0, 1.0)
    return (clipped * 32767.0).astype(np.int16).tobytes()


class AudioCapture:
    """One shared WASAPI loopback device, drained continuously in small, fixed-size chunks on its
    own thread -- this is the actual fix for the "data discontinuity" / audio-stutter problem:
    the OS buffer needs frequent, small reads regardless of how big a chunk a client wants to batch
    for sending (that batching is `Settings.audio_buffer_ms`, a completely separate knob). Opens
    the device when the first client connects, closes it when the last one disconnects; assumes a
    single concurrent audio listener (fine for "open it on the TV when you want to watch" use --
    two simultaneous listeners would split the data between them, not each get a full copy).
    """

    POLL_MS = 20          # how often we drain the device -- small and fixed, not user-configurable
    MAX_QUEUE_MS = 2000    # safety cap: if nobody drains fast enough, drop the oldest audio

    def __init__(self, open_recorder, capture_fn, samplerate=48000, channels=2):
        self._open_recorder = open_recorder
        self._capture_fn = capture_fn
        self.samplerate = samplerate
        self.channels = channels
        self._cv = threading.Condition()
        self._chunks = collections.deque()   # small float32 (frames, channels) arrays, oldest first
        self._queued_frames = 0
        self._dropped_frames = 0
        self._refcount = 0
        self._thread = None
        self._stop = threading.Event()

    def acquire(self):
        with self._cv:
            self._refcount += 1
            if self._refcount == 1:
                self._stop.clear()
                self._chunks.clear()
                self._queued_frames = 0
                self._dropped_frames = 0
                self._thread = threading.Thread(target=self._run, daemon=True)
                self._thread.start()

    def release(self):
        thread = None
        with self._cv:
            self._refcount = max(0, self._refcount - 1)
            if self._refcount == 0:
                self._stop.set()
                thread = self._thread
        if thread:
            thread.join(timeout=2)

    def _run(self):
        numframes = max(1, int(self.samplerate * self.POLL_MS / 1000))
        max_frames = int(self.samplerate * self.MAX_QUEUE_MS / 1000)
        try:
            recorder = self._open_recorder(self.samplerate)
            with recorder:
                while not self._stop.is_set():
                    chunk = self._capture_fn(recorder, numframes)
                    with self._cv:
                        self._chunks.append(chunk)
                        self._queued_frames += len(chunk)
                        while self._queued_frames > max_frames and len(self._chunks) > 1:
                            dropped = self._chunks.popleft()
                            self._queued_frames -= len(dropped)
                            self._dropped_frames += len(dropped)
                        self._cv.notify_all()
        except Exception:
            # device vanished (app closed, output device changed) -- readers just stop getting
            # data and the client (the TV app's retry loop) reconnects.
            pass

    def read(self, min_frames, timeout=2.0):
        """Blocks until at least `min_frames` are queued (or `timeout`), then returns and removes
        that many frames (concatenated, oldest first) -- less than `min_frames` only on timeout or
        if capture has stopped."""
        import numpy as np
        deadline = time.monotonic() + timeout
        with self._cv:
            while self._queued_frames < min_frames and not self._stop.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._cv.wait(timeout=remaining)
            if not self._chunks:
                return np.zeros((0, self.channels), dtype="float32")
            available = np.concatenate(list(self._chunks), axis=0)
            take = min(len(available), min_frames) if self._queued_frames >= min_frames else len(available)
            out, rest = available[:take], available[take:]
            self._chunks.clear()
            if len(rest):
                self._chunks.append(rest)
            self._queued_frames = len(rest)
            return out


def make_mp3_encoder(samplerate, channels, bitrate):
    import lameenc
    enc = lameenc.Encoder()
    enc.set_bit_rate(bitrate)
    enc.set_in_sample_rate(samplerate)
    enc.set_channels(channels)
    enc.set_quality(2)  # 2 = highest quality lameenc offers; this is a LAN stream, CPU is cheap
    return enc


def encode_mp3_chunk(encoder, pcm16, flush=False):
    data = encoder.encode(pcm16)
    if flush:
        data += encoder.flush()
    return data


# ---- HTTP -----------------------------------------------------------------------------------

CONTROL_PAGE_FALLBACK = """<!doctype html><meta charset="utf-8"><title>PC Screen</title>
<body style="font:14px sans-serif;background:#111;color:#eee;padding:24px">
<p>control.html nao foi encontrado ao lado do script. Os endpoints ainda funcionam:</p>
<ul><li><a href="/stream.mjpg" style="color:#6cf">/stream.mjpg</a></li>
<li><a href="/audio.mp3" style="color:#6cf">/audio.mp3</a></li>
<li>GET/POST /config (JSON)</li></ul></body>"""


def make_handler(settings, args, audio_capture=None):
    if audio_capture is None:
        audio_capture = AudioCapture(args.open_recorder, args.capture_audio)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *a):
            pass  # quiet; real errors still raise and print their own traceback

        def _check_token(self, query):
            if not args.token:
                return True
            got = parse_qs(query).get("token", [""])[0]
            if got != args.token:
                self.send_response(403)
                self.end_headers()
                self.wfile.write(b"bad token")
                return False
            return True

        def do_GET(self):
            parsed = urlparse(self.path)

            if parsed.path == "/":
                self._serve_control_page()
            elif parsed.path == "/config":
                self._serve_config_get()
            elif parsed.path == "/stream.mjpg":
                if self._check_token(parsed.query):
                    self._serve_video()
            elif parsed.path == "/audio.mp3":
                if self._check_token(parsed.query):
                    self._serve_audio()
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self):
            parsed = urlparse(self.path)
            if parsed.path != "/config":
                self.send_response(404)
                self.end_headers()
                return
            if not self._check_token(parsed.query):
                return
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                patch = json.loads(raw or b"{}")
                new = settings.update(patch)
            except (ValueError, json.JSONDecodeError) as e:
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": str(e)}).encode())
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(new.as_dict()).encode())

        # -- handlers --

        def _serve_control_page(self):
            path = os.path.join(HERE, "control.html")
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                body = CONTROL_PAGE_FALLBACK.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _serve_config_get(self):
            # CORS: the TV app's page is on a different origin than http://<pc>:<port>, and it
            # reads /config with a plain XHR GET to pick up the live audio_delay_ms — without this
            # header the browser silently drops the response and the app falls back to 0ms.
            body = json.dumps(settings.snapshot().as_dict()).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _serve_video(self):
            self.send_response(200)
            self.send_header("Age", "0")
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Pragma", "no-cache")
            self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY}")
            self.end_headers()
            try:
                while True:
                    s = settings.snapshot()
                    start = time.monotonic()
                    img = args.capture(args.bbox)
                    if s.scale != 1.0:
                        w, h = img.size
                        img = img.resize((max(1, int(w * s.scale)), max(1, int(h * s.scale))))
                    data = jpeg_bytes(img, s.quality)
                    self.wfile.write(f"--{BOUNDARY}\r\n".encode())
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(data)}\r\n\r\n".encode())
                    self.wfile.write(data)
                    self.wfile.write(b"\r\n")
                    period = 1.0 / max(s.fps, 1)
                    elapsed = time.monotonic() - start
                    if elapsed < period:
                        time.sleep(period - elapsed)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the TV app closed, or was closed — normal, not an error

        def _serve_audio(self):
            self.send_response(200)
            self.send_header("Cache-Control", "no-cache, private")
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            s0 = settings.snapshot()
            encoder = args.make_encoder(audio_capture.samplerate, audio_capture.channels, s0.audio_bitrate)
            audio_capture.acquire()
            try:
                while True:
                    s = settings.snapshot()
                    min_frames = max(1, int(audio_capture.samplerate * s.audio_buffer_ms / 1000))
                    chunk = audio_capture.read(min_frames)
                    if len(chunk) == 0:
                        continue  # capture thread hasn't produced anything yet (or just started)
                    data = encode_mp3_chunk(encoder, pcm16_bytes(chunk))
                    if data:
                        self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
            finally:
                audio_capture.release()

    return Handler


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--fps", type=float, default=30.0)
    ap.add_argument("--quality", type=int, default=70, help="JPEG quality 1-95")
    ap.add_argument("--scale", type=float, default=1.0, help="0.75 = 75%% of native resolution")
    ap.add_argument("--audio-bitrate", type=int, default=128, dest="audio_bitrate", help="MP3 kbps")
    ap.add_argument("--audio-buffer-ms", type=int, default=150, dest="audio_buffer_ms",
                     help="maior = mais estavel e mais atraso; menor = mais responsivo, mais risco de engasgo")
    ap.add_argument("--audio-delay-ms", type=int, default=0, dest="audio_delay_ms",
                     help="ajuste manual de defasagem audio/video; lido pelo app da TV via /config")
    ap.add_argument("--bbox", type=int, nargs=4, default=None, metavar=("X1", "Y1", "X2", "Y2"),
                     help="capture only this screen region instead of everything")
    ap.add_argument("--token", default="", help="require ?token=... in every URL (off by default)")
    cli = ap.parse_args()

    settings = SharedSettings(Settings(
        fps=cli.fps, quality=cli.quality, scale=cli.scale,
        audio_bitrate=cli.audio_bitrate, audio_buffer_ms=cli.audio_buffer_ms, audio_delay_ms=cli.audio_delay_ms,
    ))

    # Wire real implementations here so the handler factory (and its tests) can take fakes instead.
    cli.capture = capture_frame
    cli.open_recorder = open_loopback_recorder
    cli.capture_audio = capture_audio_chunk
    cli.make_encoder = make_mp3_encoder

    server = ThreadingHTTPServer(("0.0.0.0", cli.port), make_handler(settings, cli))
    qs = f"?token={cli.token}" if cli.token else ""
    ip = lan_ip()
    print(f"Painel de controle: http://{ip}:{cli.port}/")
    print(f"Video:  http://{ip}:{cli.port}/stream.mjpg{qs}")
    print(f"Audio:  http://{ip}:{cli.port}/audio.mp3{qs}")
    if not cli.token:
        print("Sem --token: qualquer um na sua rede Wi-Fi consegue abrir esses links enquanto isto roda.")
    print("Ctrl+C para parar.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nParado.")


if __name__ == "__main__":
    main()
