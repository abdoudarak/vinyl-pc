import asyncio, json, sys, threading, time, urllib.request, urllib.parse
import numpy as np
import pyaudiowpatch as pa
import webview
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from winsdk.windows.media.control import (
    GlobalSystemMediaTransportControlsSessionManager as Manager,
    GlobalSystemMediaTransportControlsSessionPlaybackStatus as Status)
from winsdk.windows.storage.streams import DataReader

PORT = 8765
HERE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
STATE = {"active": False}
COVER = b""
session = None
loop = asyncio.new_event_loop()


async def read_thumb(ref):
    if ref is None:
        return b""
    stream = await ref.open_read_async()
    reader = DataReader(stream)
    await reader.load_async(stream.size)
    buf = bytearray(stream.size)
    reader.read_bytes(buf)
    return bytes(buf)


async def poll():
    global STATE, COVER, session
    mgr = await Manager.request_async()
    last, cid = None, 0
    while True:
        try:
            s = mgr.get_current_session()
            session = s
            if s is None:
                STATE = {"active": False}
            else:
                p = await s.try_get_media_properties_async()
                key = (p.title, p.artist)
                if key != last:
                    COVER = await read_thumb(p.thumbnail)
                    last, cid = key, cid + 1
                tl = s.get_timeline_properties()
                playing = s.get_playback_info().playback_status == Status.PLAYING
                pos = tl.position.total_seconds()
                dur = tl.end_time.total_seconds()
                try:
                    if playing and tl.last_updated_time.tzinfo:
                        pos += (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
                except Exception:
                    pass
                pos = max(0, min(pos, dur)) if dur > 0 else max(0, pos)
                STATE = {"active": True, "title": p.title, "artist": p.artist,
                         "pos": pos, "dur": dur, "playing": playing, "cover": cid}
        except Exception as e:
            print("poll error:", e)
        await asyncio.sleep(0.5)


async def control(a):
    s = session
    if not s:
        return
    if a == "toggle":
        await s.try_toggle_play_pause_async()
    elif a == "next":
        await s.try_skip_next_async()
    elif a == "prev":
        await s.try_skip_previous_async()



NB = 48
BANDS, BASS, LAST_AUDIO = [0.0] * NB, 0.0, 0.0


def audio_thread():
    global BANDS, BASS, LAST_AUDIO
    try:
        p = pa.PyAudio()
        wasapi = p.get_host_api_info_by_type(pa.paWASAPI)
        dev = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not dev.get("isLoopbackDevice"):
            for lb in p.get_loopback_device_info_generator():
                if dev["name"] in lb["name"]:
                    dev = lb
                    break
        ch, rate = dev["maxInputChannels"], int(dev["defaultSampleRate"])
        win = np.hanning(1024)
        edges = [int(x) for x in np.logspace(0.3, 2.5, NB + 1)]
        for i in range(1, len(edges)):
            edges[i] = max(edges[i], edges[i - 1] + 1)
        state = {"old": np.zeros(NB), "peak": 0.02}

        def cb(data, frames, t, status):
            global BANDS, BASS, LAST_AUDIO
            x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768
            if ch > 1:
                x = x[: len(x) // ch * ch].reshape(-1, ch).mean(1)
            if len(x) < 1024:
                x = np.pad(x, (0, 1024 - len(x)))
            m = np.abs(np.fft.rfft(x[:1024] * win))
            v = np.array([m[a:b].mean() for a, b in zip(edges[:-1], edges[1:])])
            state["peak"] = max(state["peak"] * 0.998, float(v.max()), 0.02)
            v = np.clip(v / state["peak"], 0, 1) ** 1.3
            state["old"] = np.maximum(v, state["old"] * 0.85)
            BANDS = [round(float(a), 3) for a in state["old"]]
            BASS = float(np.mean(state["old"][:5]))
            LAST_AUDIO = time.time()
            return (None, pa.paContinue)

        p.open(format=pa.paInt16, channels=ch, rate=rate, input=True,
               input_device_index=dev["index"], frames_per_buffer=1024, stream_callback=cb)
        while True:
            time.sleep(1)
    except Exception as e:
        print("audio error:", e)


W = None
FULL = False


class Api:
    def mini(self):
        global FULL
        if FULL:
            W.toggle_fullscreen()
            FULL = False
        W.resize(380, 580)
        W.on_top = True
        return "mini"

    def full(self):
        global FULL
        if not FULL:
            W.toggle_fullscreen()
            FULL = True
        W.on_top = False
        return "full"

    def minimize(self):
        W.minimize()

    def close(self):
        W.destroy()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            self.send((HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif u.path == "/api/now":
            self.send(json.dumps(STATE).encode(), "application/json")
        elif u.path == "/api/audio":
            fresh = time.time() - LAST_AUDIO < 0.4
            self.send(json.dumps({"b": BANDS if fresh else [0] * NB, "bass": BASS if fresh else 0}).encode(), "application/json")
        elif u.path == "/cover":
            self.send(COVER, "image/jpeg")
        elif u.path == "/api/ctl":
            asyncio.run_coroutine_threadsafe(control(q.get("a", [""])[0]), loop)
            self.send(b"{}", "application/json")
        elif u.path == "/api/lyrics":
            params = {"track_name": q.get("title", [""])[0], "artist_name": q.get("artist", [""])[0]}
            d = q.get("dur", ["0"])[0]
            if d and float(d) > 0:
                params["duration"] = int(float(d))
            try:
                req = urllib.request.Request("https://lrclib.net/api/get?" + urllib.parse.urlencode(params),
                                             headers={"User-Agent": "VinylPlayerPC/1.0"})
                self.send(urllib.request.urlopen(req, timeout=8).read(), "application/json")
            except Exception:
                self.send(b"{}", "application/json")
        else:
            self.send_response(404)
            self.end_headers()


if __name__ == "__main__":
    threading.Thread(target=lambda: (asyncio.set_event_loop(loop), loop.run_until_complete(poll())),
                     daemon=True).start()
    threading.Thread(target=audio_thread, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    W = webview.create_window("Vinyl Player", f"http://127.0.0.1:{PORT}", width=380, height=580,
                              frameless=True, easy_drag=True, on_top=True, js_api=Api(),
                              background_color="#07050c")
    webview.start()
