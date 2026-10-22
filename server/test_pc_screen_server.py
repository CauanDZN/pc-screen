"""Exercises the server with fake screen/audio capture (no real display or audio device needed):
MJPEG framing, the live /config get+post, audio chunk encoding, token checks, 404s, and that
`--help` doesn't crash (argparse treats a literal `%` in a help string as a format spec, which bit
an earlier version of this file).

Run: python3 -m unittest test_pc_screen_server -v
"""
import http.client
import json
import subprocess
import sys
import threading
import time
import unittest
from io import BytesIO
from types import SimpleNamespace

from PIL import Image

import pc_screen_server as srv


def fake_capture(_bbox=None):
    return Image.new("RGB", (64, 48), (10, 20, 30))


class FakeRecorder:
    """Stands in for a soundcard WASAPI loopback recorder: a silent context manager."""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_open_recorder(_samplerate):
    return FakeRecorder()


def fake_capture_audio(_recorder, numframes):
    import numpy as np
    # a quiet 440Hz-ish tone so the encoder has real signal, not literal digital silence
    t = np.linspace(0, numframes / 48000.0, numframes, endpoint=False)
    tone = 0.05 * np.sin(2 * np.pi * 440 * t)
    return tone.reshape(-1, 1).repeat(2, axis=1).astype("float32")


def parse_mjpeg_frames(data):
    """Splits a captured multipart/x-mixed-replace blob into complete JPEG bodies, skipping any
    trailing fragment the test's read loop stopped in the middle of (no `\\r\\n\\r\\n` yet)."""
    parts = data.split(f"--{srv.BOUNDARY}\r\n".encode())
    out = []
    for p in parts:
        if b"image/jpeg" not in p or b"\r\n\r\n" not in p:
            continue
        out.append(p.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n"))
    return out


def make_args(**overrides):
    cli = SimpleNamespace(
        token="", bbox=None,
        capture=fake_capture, open_recorder=fake_open_recorder,
        capture_audio=fake_capture_audio, make_encoder=srv.make_mp3_encoder,
    )
    for k, v in overrides.items():
        setattr(cli, k, v)
    return cli


class SettingsTest(unittest.TestCase):
    def test_valid_update_applies(self):
        s = srv.SharedSettings(srv.Settings())
        new = s.update({"fps": 24, "quality": 50})
        self.assertEqual(new.fps, 24)
        self.assertEqual(new.quality, 50)

    def test_out_of_range_rejected_and_nothing_partially_applied(self):
        s = srv.SharedSettings(srv.Settings(fps=30))
        with self.assertRaises(ValueError):
            s.update({"fps": 999})
        self.assertEqual(s.snapshot().fps, 30, "a rejected update must not mutate the live settings")

    def test_unknown_field_rejected(self):
        s = srv.SharedSettings(srv.Settings())
        with self.assertRaises(ValueError):
            s.update({"not_a_real_field": 1})

    def test_mixed_valid_and_invalid_in_one_patch_applies_nothing(self):
        s = srv.SharedSettings(srv.Settings(quality=70))
        with self.assertRaises(ValueError):
            s.update({"quality": 80, "fps": -5})
        self.assertEqual(s.snapshot().quality, 70, "fps failing must not leave quality half-applied")


class AudioEncodeTest(unittest.TestCase):
    def test_pcm16_bytes_shape(self):
        import numpy as np
        chunk = np.zeros((480, 2), dtype="float32")
        data = srv.pcm16_bytes(chunk)
        self.assertEqual(len(data), 480 * 2 * 2)  # frames * channels * 2 bytes/sample

    def test_mp3_encoder_emits_a_real_frame_sync(self):
        encoder = srv.make_mp3_encoder(48000, 2, 128)
        chunk = fake_capture_audio(None, 48000 // 10)  # 100ms
        data = srv.encode_mp3_chunk(encoder, srv.pcm16_bytes(chunk), flush=True)
        self.assertGreater(len(data), 0)
        # MPEG audio frame sync: 11 set bits -> 0xFF followed by top 3 bits of the next byte set
        idx = data.find(b"\xff")
        self.assertNotEqual(idx, -1, "no 0xFF sync byte found in encoder output at all")
        self.assertEqual(data[idx + 1] & 0xE0, 0xE0, "byte after 0xFF is not a valid MP3 frame sync")


class AudioCaptureTest(unittest.TestCase):
    """The actual fix for the "data discontinuity" stutter: capture must be drained in small, fixed
    chunks on its own thread, decoupled from however big a batch a client wants for sending."""

    def _counting_capture(self):
        import numpy as np
        calls = {"n": 0}

        def capture(_recorder, numframes):
            calls["n"] += 1
            return np.full((numframes, 2), 0.01 * calls["n"], dtype="float32")

        return capture, calls

    def test_acquire_starts_thread_only_on_first_caller(self):
        capture, calls = self._counting_capture()
        ac = srv.AudioCapture(lambda sr: FakeRecorder(), capture, samplerate=1000)
        ac.acquire()
        ac.acquire()  # second caller must not spawn a second capture thread
        time.sleep(0.1)
        ac.read(10, timeout=1)
        n_after_two_acquires = calls["n"]
        ac.release()
        ac.release()
        time.sleep(0.05)
        self.assertGreater(n_after_two_acquires, 0)

    def test_release_to_zero_stops_the_thread(self):
        capture, calls = self._counting_capture()
        ac = srv.AudioCapture(lambda sr: FakeRecorder(), capture, samplerate=1000)
        ac.acquire()
        time.sleep(0.1)
        ac.release()
        time.sleep(0.05)
        stopped_at = calls["n"]
        time.sleep(0.15)
        self.assertEqual(calls["n"], stopped_at, "capture kept running after the last client released it")

    def test_read_returns_concatenated_frames_in_order(self):
        import numpy as np
        ac = srv.AudioCapture(lambda sr: FakeRecorder(), fake_capture_audio, samplerate=1000)
        ac.acquire()
        try:
            out = ac.read(500, timeout=2)
            self.assertGreaterEqual(len(out), 1)
            self.assertEqual(out.shape[1], 2)
        finally:
            ac.release()

    def test_slow_consumer_drops_oldest_instead_of_growing_forever(self):
        # this is the scenario that caused the real bug: a producer faster than the consumer.
        # the fix must bound memory/latency by dropping old audio, not let the queue grow forever
        # and not silently stall the producer thread (which is what caused the WASAPI overruns).
        capture, calls = self._counting_capture()
        ac = srv.AudioCapture(lambda sr: FakeRecorder(), capture, samplerate=1000)
        ac.POLL_MS = 5
        ac.MAX_QUEUE_MS = 100
        ac.acquire()
        try:
            time.sleep(0.5)  # producer runs far ahead; nobody calls read() during this
            with ac._cv:
                queued_ms = ac._queued_frames / ac.samplerate * 1000
                self.assertGreater(calls["n"], 0, "producer thread never ran")
                self.assertLessEqual(queued_ms, ac.MAX_QUEUE_MS * 1.5,
                                      "queue grew well past its cap -- old audio is not being dropped")
                self.assertGreater(ac._dropped_frames, 0, "expected some frames to have been dropped by now")
        finally:
            ac.release()

    def test_read_times_out_with_partial_data_when_capture_is_slow(self):
        def never_enough(_recorder, numframes):
            import numpy as np
            time.sleep(0.2)
            return np.zeros((1, 2), dtype="float32")  # far less than what read() will ask for

        ac = srv.AudioCapture(lambda sr: FakeRecorder(), never_enough, samplerate=48000)
        ac.acquire()
        try:
            start = time.monotonic()
            out = ac.read(48000, timeout=0.3)  # asks for 1s of audio, should NOT block ~1s
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 1.0)
            self.assertLess(len(out), 48000)
        finally:
            ac.release()


class HelpTest(unittest.TestCase):
    def test_help_does_not_crash(self):
        r = subprocess.run([sys.executable, srv.__file__, "--help"], capture_output=True, text=True, timeout=10)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("usage:", r.stdout)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.settings = srv.SharedSettings(srv.Settings(fps=200.0))  # fast fps: tests don't wait around
        self.args = make_args()
        self.httpd = srv.ThreadingHTTPServer(("127.0.0.1", 0), srv.make_handler(self.settings, self.args))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)

    def test_unknown_path_is_404(self):
        c = self.conn()
        c.request("GET", "/nope")
        self.assertEqual(c.getresponse().status, 404)
        c.close()

    def test_control_page_served_at_root(self):
        c = self.conn()
        c.request("GET", "/")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        body = r.read()
        self.assertIn(b"<html", body.lower())
        c.close()

    def test_config_get_returns_current_settings(self):
        c = self.conn()
        c.request("GET", "/config")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        body = json.loads(r.read())
        self.assertIn("fps", body)
        self.assertIn("audio_buffer_ms", body)
        c.close()

    def test_config_get_has_cors_header_for_the_tv_app(self):
        # the TV app's page is a different origin than http://<pc>:<port>; without this header
        # the browser drops the response and audio_delay_ms silently falls back to 0.
        c = self.conn()
        c.request("GET", "/config")
        r = c.getresponse()
        self.assertEqual(r.getheader("Access-Control-Allow-Origin"), "*")
        r.read()
        c.close()

    def test_config_post_updates_and_video_loop_picks_it_up(self):
        c = self.conn()
        body = json.dumps({"quality": 42}).encode()
        c.request("POST", "/config", body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(json.loads(r.read())["quality"], 42)
        c.close()
        self.assertEqual(self.settings.snapshot().quality, 42)

    def test_config_post_invalid_value_is_400(self):
        c = self.conn()
        body = json.dumps({"quality": 999}).encode()
        c.request("POST", "/config", body=body, headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        self.assertIn("error", json.loads(r.read()))
        c.close()

    def test_token_required_on_video_and_audio_when_set(self):
        self.args.token = "secret"
        try:
            for path in ("/stream.mjpg", "/audio.mp3"):
                c = self.conn()
                c.request("GET", path)
                self.assertEqual(c.getresponse().status, 403, path)
                c.close()
        finally:
            self.args.token = ""

    def test_video_stream_is_valid_mjpeg_with_decodable_frames(self):
        c = self.conn()
        c.request("GET", "/stream.mjpg")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn("multipart/x-mixed-replace", r.getheader("Content-Type"))

        data = b""
        deadline = time.time() + 3
        while data.count(b"\xff\xd8\xff") < 3 and time.time() < deadline:
            chunk = r.read(8192)
            if not chunk:
                break
            data += chunk
        c.close()
        self.assertGreaterEqual(data.count(b"\xff\xd8\xff"), 3)
        jpegs = parse_mjpeg_frames(data)
        self.assertGreaterEqual(len(jpegs), 2)
        img = Image.open(BytesIO(jpegs[0]))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(img.size, (64, 48))

    def test_video_scale_resizes_frames_live(self):
        self.settings.update({"scale": 0.5})
        c = self.conn()
        c.request("GET", "/stream.mjpg")
        r = c.getresponse()
        data = b""
        deadline = time.time() + 3
        while data.count(b"\xff\xd8\xff") < 2 and time.time() < deadline:
            chunk = r.read(8192)
            if not chunk:
                break
            data += chunk
        c.close()
        jpegs = parse_mjpeg_frames(data)
        self.assertGreaterEqual(len(jpegs), 1)
        img = Image.open(BytesIO(jpegs[0]))
        self.assertEqual(img.size, (32, 24))

    def test_audio_stream_has_content_type_and_real_mp3_bytes(self):
        self.settings.update({"audio_buffer_ms": 50})  # small, so the test doesn't wait long
        c = self.conn()
        c.request("GET", "/audio.mp3")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertEqual(r.getheader("Content-Type"), "audio/mpeg")
        data = b""
        deadline = time.time() + 3
        while len(data) < 200 and time.time() < deadline:
            chunk = r.read(4096)
            if not chunk:
                break
            data += chunk
        c.close()
        self.assertGreater(len(data), 0, "no audio bytes arrived at all")
        idx = data.find(b"\xff")
        self.assertNotEqual(idx, -1)
        self.assertEqual(data[idx + 1] & 0xE0, 0xE0, "no valid MP3 frame sync in the audio stream")


if __name__ == "__main__":
    unittest.main(verbosity=2)
