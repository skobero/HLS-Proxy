#!/usr/bin/env python3
import sys
import os
import re
import time
import socket
import threading
import urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STREAM_FILE = os.path.join(BASE_DIR, "stream_url.txt")
DEFAULT_PORT = 8080

session = requests.Session()
retries = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
session.mount('https://', HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=retries))
session.mount('http://', HTTPAdapter(pool_connections=30, pool_maxsize=30, max_retries=retries))

def get_headers_for_url(url):
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }
    if any(k in url for k in ["matchora", "kugunor", "onhockey"]):
        headers["Referer"] = "https://matchora.to/"
        headers["Origin"] = "https://matchora.to"
    return headers

def extract_matchora_channel_id(input_str):
    if any(k in input_str for k in ["matchora", "onhockey", "kugunor"]):
        m = re.search(r'channel/(\d+)', input_str)
        if m:
            return m.group(1)
        m = re.search(r'/hls/(\d+)/', input_str)
        if m:
            return m.group(1)
        if input_str.strip().isdigit():
            return input_str.strip()
    return None

def strip_png_header_if_needed(raw_data):
    """Odstraní maskovací PNG hlavičku, pokud ji poskytovatel streamu vkládá před TS segmenty."""
    if raw_data.startswith(b'\x89PNG'):
        iend_idx = raw_data.find(b'IEND')
        if iend_idx != -1:
            raw_data = raw_data[iend_idx + 8:]
    # Ujistíme se, že segment začíná MPEG-TS sync bajtem 0x47
    if len(raw_data) > 0 and not raw_data.startswith(b'\x47'):
        sync_idx = raw_data.find(b'\x47')
        if sync_idx != -1:
            raw_data = raw_data[sync_idx:]
    return raw_data

def resolve_variant_if_master(base_url, content):
    """Pokud je vrácený obsah Master Playlist (EXT-X-STREAM-INF), vybere nejvyšší kvalitu."""
    if "EXT-X-STREAM-INF" not in content:
        return base_url, content
    lines = content.splitlines()
    best_bw = -1
    best_url = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXT-X-STREAM-INF"):
            bw_m = re.search(r"BANDWIDTH=(\d+)", line)
            bw = int(bw_m.group(1)) if bw_m else 0
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or lines[j].strip().startswith("#")):
                j += 1
            if j < len(lines):
                cand_url = urllib.parse.urljoin(base_url, lines[j].strip())
                if bw > best_bw or best_url is None:
                    best_bw = bw
                    best_url = cand_url
            i = j
        else:
            i += 1
    if best_url:
        print(f"[*] Master playlist detekovan, vybrana nejlepsi varianta: {best_url[:80]}...")
        resp = session.get(best_url, headers=get_headers_for_url(best_url), timeout=8)
        if resp.status_code == 200:
            return best_url, resp.text
    return base_url, content

class VirtualSegment:
    def __init__(self, seg_id, duration, uri, is_discontinuity=False, key_line=None):
        self.seg_id = seg_id
        self.duration = duration
        self.uri = uri
        self.is_discontinuity = is_discontinuity
        self.key_line = key_line

class PlaylistEngine:
    def __init__(self, max_window=7):
        self.lock = threading.Lock()
        self.max_window = max_window
        self.segments = []
        self.next_seg_id = 1
        self.media_sequence = 1
        self.discontinuity_sequence = 0
        self.target_duration = 6
        self.last_epoch = None

    def update(self, epoch, raw_text, base_url):
        with self.lock:
            raw_stripped = raw_text.strip()
            if not raw_stripped.startswith("#EXTM3U"):
                print("[!] Upozorneni: Zdroj nevratil platny HLS playlist (#EXTM3U).")
                return

            lines = raw_stripped.splitlines()
            parsed_segs = []
            cur_duration = 6.0
            cur_key = None
            target_dur = 6

            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if not line:
                    i += 1
                    continue
                if line.startswith("#EXT-X-TARGETDURATION:"):
                    try:
                        target_dur = int(line.split(":")[1])
                    except Exception:
                        pass
                elif line.startswith("#EXT-X-KEY:"):
                    cur_key = line
                elif line.startswith("#EXTINF:"):
                    m = re.match(r"#EXTINF:([0-9.]+)", line)
                    if m:
                        cur_duration = float(m.group(1))
                elif not line.startswith("#"):
                    abs_seg_url = urllib.parse.urljoin(base_url, line)
                    parsed_segs.append({
                        "duration": cur_duration,
                        "uri": abs_seg_url,
                        "key": cur_key
                    })
                i += 1

            self.target_duration = max(self.target_duration, target_dur)

            is_new_stream = (self.last_epoch is not None and self.last_epoch != epoch)
            self.last_epoch = epoch

            if is_new_stream:
                print(f"[+] Prepnut stream (epoch {epoch})! Vytvarim plynuly prechod (Discontinuity bridge)...")
                # Ponecháme v okně posledních 2-3 segmenty ze starého streamu, aby TV dokončila stahování bufferu
                if len(self.segments) > 2:
                    to_remove = len(self.segments) - 2
                    for s in self.segments[:to_remove]:
                        self.media_sequence += 1
                        if s.is_discontinuity:
                            self.discontinuity_sequence += 1
                    self.segments = self.segments[to_remove:]

                # První segment z nového streamu označíme jako DISCONTINUITY
                first = True
                for ps in parsed_segs:
                    seg = VirtualSegment(
                        seg_id=self.next_seg_id,
                        duration=ps["duration"],
                        uri=ps["uri"],
                        is_discontinuity=first,
                        key_line=ps["key"]
                    )
                    self.next_seg_id += 1
                    self.segments.append(seg)
                    first = False
            else:
                existing_uris = {s.uri for s in self.segments}
                for ps in parsed_segs:
                    if ps["uri"] not in existing_uris:
                        seg = VirtualSegment(
                            seg_id=self.next_seg_id,
                            duration=ps["duration"],
                            uri=ps["uri"],
                            is_discontinuity=False,
                            key_line=ps["key"]
                        )
                        self.next_seg_id += 1
                        self.segments.append(seg)

            # Udržujeme velikost okna pro live stream
            while len(self.segments) > self.max_window:
                removed = self.segments.pop(0)
                self.media_sequence += 1
                if removed.is_discontinuity:
                    self.discontinuity_sequence += 1

    def build_m3u8(self, base_proxy_url):
        with self.lock:
            if not self.segments:
                return None

            out = [
                "#EXTM3U",
                "#EXT-X-VERSION:3",
                f"#EXT-X-TARGETDURATION:{self.target_duration}",
                f"#EXT-X-MEDIA-SEQUENCE:{self.media_sequence}",
            ]
            if self.discontinuity_sequence > 0:
                out.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{self.discontinuity_sequence}")
            out.append("#EXT-X-INDEPENDENT-SEGMENTS")

            for s in self.segments:
                if s.is_discontinuity:
                    out.append("#EXT-X-DISCONTINUITY")
                if s.key_line:
                    def replace_key_uri(match):
                        k_url = match.group(1)
                        abs_k = urllib.parse.urljoin(s.uri, k_url)
                        return f'URI="{base_proxy_url}/segment?url={urllib.parse.quote(abs_k, safe="")}"'
                    mod_key = re.sub(r'URI="([^"]+)"', replace_key_uri, s.key_line)
                    out.append(mod_key)
                out.append(f"#EXTINF:{s.duration:.3f},")
                proxy_seg = f"{base_proxy_url}/segment?url={urllib.parse.quote(s.uri, safe='')}"
                out.append(proxy_seg)

            return "\n".join(out)

class StreamManager:
    def __init__(self):
        self.lock = threading.Lock()
        self.raw_input = ""
        self.active_m3u8_url = ""
        self.token_expiry = 0
        self.matchora_ch = None
        self.epoch = 1
        self.file_mtime = 0
        self.load_from_file()

    def check_file_change(self):
        """Automaticky detekuje, zda uživatel neupravil soubor stream_url.txt ručně na disku."""
        if os.path.exists(STREAM_FILE):
            try:
                mtime = os.path.getmtime(STREAM_FILE)
                if mtime > self.file_mtime:
                    with open(STREAM_FILE, "r", encoding="utf-8") as f:
                        new_content = f.read().strip()
                    self.file_mtime = mtime
                    if new_content != self.raw_input:
                        print(f"[*] Zjistena zmena v {STREAM_FILE}!")
                        self.set_stream(new_content, save_file=False)
            except Exception as e:
                print(f"[!] Chyba cteni souboru: {e}")

    def set_stream(self, url, save_file=True):
        with self.lock:
            url_clean = url.strip()
            if url_clean == self.raw_input:
                return
            self.raw_input = url_clean
            self.matchora_ch = extract_matchora_channel_id(self.raw_input)
            self.active_m3u8_url = ""
            self.token_expiry = 0
            self.epoch += 1

            if save_file:
                try:
                    with open(STREAM_FILE, "w", encoding="utf-8") as f:
                        f.write(self.raw_input + "\n")
                    self.file_mtime = os.path.getmtime(STREAM_FILE)
                except Exception as e:
                    print(f"[!] Nelze zapsat do {STREAM_FILE}: {e}")

            print(f"[+] Nastaven novy stream (epoch {self.epoch}): {self.raw_input[:70]}...")

    def load_from_file(self):
        if os.path.exists(STREAM_FILE):
            try:
                self.file_mtime = os.path.getmtime(STREAM_FILE)
                with open(STREAM_FILE, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                if content:
                    self.raw_input = content
                    self.matchora_ch = extract_matchora_channel_id(self.raw_input)
            except Exception as e:
                print(f"[!] Chyba nacitani {STREAM_FILE}: {e}")

    def get_active_url(self, force_refresh=False):
        now = time.time()
        with self.lock:
            if not self.raw_input and not self.matchora_ch:
                return None, self.epoch

            if not force_refresh and self.active_m3u8_url and now < self.token_expiry - 30:
                return self.active_m3u8_url, self.epoch

            # Matchora API auto-refresh token
            if self.matchora_ch:
                api_url = f"https://matchora.to/api/play/{self.matchora_ch}"
                try:
                    print(f"[*] Obnovuji Matchora token z {api_url}...")
                    resp = session.get(api_url, headers=get_headers_for_url(api_url), timeout=8)
                    if resp.status_code == 200:
                        data = resp.json()
                        new_url = data.get("url")
                        expires_in = data.get("expires_in", 600)
                        if new_url:
                            self.active_m3u8_url = new_url
                            self.token_expiry = now + expires_in
                            print(f"[+] Matchora token uspesne obnoven: {new_url[:60]}...")
                            return self.active_m3u8_url, self.epoch
                except Exception as e:
                    print(f"[!] Chyba pri volani /api/play: {e}")

            self.active_m3u8_url = self.raw_input
            self.token_expiry = now + 3600
            return self.active_m3u8_url, self.epoch

stream_manager = StreamManager()
playlist_engine = PlaylistEngine(max_window=7)

def get_local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip

class UniversalHLSHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        msg = args[0] if args else ""
        if "segment" not in msg:
            super().log_message(format, *args)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/set_stream":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length).decode('utf-8')
            params = urllib.parse.parse_qs(post_data)
            new_url = params.get('url', [''])[0].strip()
            if new_url:
                stream_manager.set_stream(new_url, save_file=True)
            self.send_response(303)
            self.send_header('Location', '/')
            self.end_headers()
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        host_header = self.headers.get("Host", f"localhost:{DEFAULT_PORT}")
        base_proxy_url = f"http://{host_header}"

        # Webová administrace pro snadné přepnutí streamu z mobilu / PC
        if path == "/":
            stream_manager.check_file_change()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            status_text = "Stream je aktivní" if stream_manager.raw_input else "Zatím nenastaven žádný stream"
            html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>HLS Seamless Proxy pro TV</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #121214; color: #e1e1e6; margin: 0; padding: 25px; }}
.box {{ max-width: 720px; margin: 20px auto; background: #202024; padding: 25px; border-radius: 12px; border: 1px solid #323238; }}
h1 {{ color: #00b37e; margin-top: 0; font-size: 24px; }}
.status {{ display: inline-block; padding: 4px 10px; border-radius: 6px; font-size: 13px; font-weight: bold; background: #00875f; color: #fff; margin-bottom: 15px; }}
.highlight {{ background: #121214; padding: 14px; border-radius: 8px; font-family: monospace; font-size: 16px; color: #04d361; word-break: break-all; border: 1px solid #00b37e; }}
input[type=text] {{ width: 100%; box-sizing: border-box; padding: 12px; background: #121214; border: 1px solid #323238; color: #fff; border-radius: 6px; font-size: 14px; margin-top: 8px; margin-bottom: 12px; }}
input[type=submit] {{ background: #00b37e; color: #fff; border: 0; padding: 12px 22px; border-radius: 6px; font-weight: bold; cursor: pointer; font-size: 14px; transition: 0.2s; }}
input[type=submit]:hover {{ background: #04d361; }}
.sub {{ color: #a8a8b3; font-size: 13px; line-height: 1.6; margin-top: 15px; }}
.badge {{ background: #29292e; padding: 2px 6px; border-radius: 4px; font-family: monospace; }}
</style>
</head>
<body>
<div class="box">
  <h1>🏒 HLS Seamless Proxy pro VLC TV</h1>
  <div class="status">{status_text} (Epoch {stream_manager.epoch})</div>
  <p>Tuto adresu máte trvale uloženou ve <strong>VLC na televizi</strong>:</p>
  <div class="highlight">{base_proxy_url}/live.m3u8</div>
  <p class="sub">✨ <strong>Plynulé přepínání (Seamless Switching):</strong> Při změně streamu níže televizní VLC <strong>nespadne</strong>. Proxy automaticky vloží <em>EXT-X-DISCONTINUITY</em> a plynule přehraje nový stream.</p>
  
  <hr style="border: 0; border-top: 1px solid #323238; margin: 25px 0;">
  
  <h3>Změnit stream (zápas):</h3>
  <form method="POST" action="/set_stream">
    <input type="text" name="url" placeholder="Vložte nové m3u8 nebo embed URL..." value="{stream_manager.raw_input}">
    <input type="submit" value="Přepnout stream na televizi">
  </form>
  <div class="sub">
    Podporuje chráněné HTTP/HLS streamy, tokenované relace i maskované video formáty a běžné <span class="badge">.m3u8</span> playlisty.
  </div>
</div>
</body>
</html>"""
            self.wfile.write(html.encode("utf-8"))
            return

        # Hlavní stream pro VLC na TV
        if path in ["/live", "/live.m3u8", "/index.m3u8", "/playlist"]:
            stream_manager.check_file_change()
            target_url, epoch = stream_manager.get_active_url()

            if not target_url:
                cached = playlist_engine.build_m3u8(base_proxy_url)
                if cached:
                    self.send_playlist(cached)
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain; charset=utf-8")
                    self.end_headers()
                    self.wfile.write("Zadny stream neni nastaven. Zadejte stream do stream_url.txt nebo pres web na http://localhost:8080/".encode())
                return

            headers = get_headers_for_url(target_url)
            try:
                resp = session.get(target_url, headers=headers, timeout=8)
                if (resp.status_code == 403 or "bad token" in resp.text) and stream_manager.matchora_ch:
                    print("[!] Token expiroval, obnovuji token...")
                    target_url, epoch = stream_manager.get_active_url(force_refresh=True)
                    resp = session.get(target_url, headers=get_headers_for_url(target_url), timeout=8)

                if resp.status_code == 200:
                    real_url, content = resolve_variant_if_master(target_url, resp.text)
                    playlist_engine.update(epoch, content, real_url)
            except Exception as e:
                print(f"[!] Chyba stahovani playlistu ze zdroje: {e}")

            # Vygenerujeme virtuální playlist pro VLC
            playlist_content = playlist_engine.build_m3u8(base_proxy_url)
            if playlist_content:
                self.send_playlist(playlist_content)
            else:
                self.send_response(503)
                self.end_headers()
                self.wfile.write(b"Cekam na data streamu...")

        elif path == "/segment":
            seg_urls = query.get("url")
            if not seg_urls:
                self.send_response(400)
                self.end_headers()
                return

            seg_url = seg_urls[0]
            headers = get_headers_for_url(seg_url)
            try:
                resp = session.get(seg_url, headers=headers, timeout=12)
                if resp.status_code != 200:
                    self.send_response(resp.status_code)
                    self.end_headers()
                    return

                # Čištění maskovaného PNG formátu
                clean_data = strip_png_header_if_needed(resp.content)

                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(clean_data)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(clean_data)
            except (ConnectionResetError, BrokenPipeError):
                pass
            except Exception as e:
                print(f"[!] Chyba segmentu: {e}")
        else:
            self.send_response(404)
            self.end_headers()

    def send_playlist(self, content_str):
        content_bytes = content_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.apple.mpegurl")
        self.send_header("Content-Length", str(len(content_bytes)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(content_bytes)

def main():
    if len(sys.argv) > 1:
        stream_manager.set_stream(sys.argv[1], save_file=True)

    local_ip = get_local_ip()
    server = ThreadingHTTPServer(("0.0.0.0", DEFAULT_PORT), UniversalHLSHandler)

    active_display = (stream_manager.raw_input[:65] + "...") if stream_manager.raw_input else "(nenastaven - zadejte pres web nebo do stream_url.txt)"
    print("=" * 65)
    print("   UNIVERZALNI HLS SEAMLESS PROXY PRO VLC NA SMART TV")
    print("=" * 65)
    print(f"Aktivni stream: {active_display}")
    print("-" * 65)
    print("ZADEJTE TENTO ODKAZ DO VLC NA TV (Otevřít síťový proud):")
    print()
    print(f"   http://{local_ip}:{DEFAULT_PORT}/live.m3u8")
    print()
    print(f"Web pro zmenu streamu z mobilu/PC: http://{local_ip}:{DEFAULT_PORT}/")
    print("-" * 65)
    print("Proxy bezi se seamless switchingem (TV pri zmene m3u nespadne).")
    print("Ukoncit muzete stiskem Ctrl + C...")
    print("=" * 65)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nUkoncuji proxy server...")
        server.server_close()

if __name__ == "__main__":
    main()
