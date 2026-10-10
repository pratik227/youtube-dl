"""Clipdrop — paste a YouTube, Instagram or TikTok link, pick a quality, download it."""

import os
import re
import shutil
import tempfile
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, Response, jsonify, request, send_file, send_from_directory

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = Path(tempfile.gettempdir()) / "clipdrop"
WORK_DIR.mkdir(exist_ok=True)

# Optional: export browser cookies to cookies.txt (Netscape format) to unlock
# login-gated Instagram posts or age-restricted YouTube videos.
COOKIES_FILE = os.environ.get("COOKIES_FILE") or str(BASE_DIR / "cookies.txt")

ALLOWED_HOSTS = {
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "youtube-nocookie.com": "youtube",
    "instagram.com": "instagram",
    "instagr.am": "instagram",
    "tiktok.com": "tiktok",
}
HEIGHTS = [2160, 1440, 1080, 720, 480, 360, 240, 144]
MAX_ACTIVE_JOBS = 4
JOB_TTL = 30 * 60  # seconds a finished file is kept around
INFO_TTL = 60 * 60
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

app = Flask(__name__, static_folder=None)

jobs = {}
infos = {}
lock = threading.Lock()


class UserError(Exception):
    pass


def detect_platform(url):
    """Return the platform name for a supported link, else raise."""
    if not isinstance(url, str) or len(url) > 2048:
        raise UserError("That doesn't look like a valid link.")
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    for domain, platform in ALLOWED_HOSTS.items():
        if host == domain or host.endswith("." + domain):
            return url, platform
    raise UserError("Only YouTube, Instagram and TikTok links are supported.")


def base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
    }
    node = shutil.which("node")
    if node:
        opts["js_runtimes"] = {"node": {"path": node}}
    if os.path.isfile(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    return opts


def friendly_error(exc):
    msg = str(exc)
    low = msg.lower()
    if "private" in low:
        return "This content is private, so it can't be downloaded."
    if "login" in low or "sign in" in low or "cookies" in low or "rate-limit" in low:
        return (
            "This one needs a logged-in session. Add a cookies.txt file next to "
            "app.py and try again."
        )
    if "timed out" in low or "connection" in low or "getaddrinfo" in low:
        return "Couldn't reach that site from this network. It may be blocked here."
    if "unavailable" in low or "not available" in low or "removed" in low:
        return "This content is unavailable or has been removed."
    if "unsupported url" in low or "no video" in low:
        return "No downloadable video was found at that link."
    if "live" in low and "stream" in low:
        return "Live streams can't be downloaded while they're still live."
    return "Couldn't fetch that link. Double-check it and try again."


def pick_entry(info):
    """Playlists/carousels: use the first entry that has media."""
    if info.get("_type") == "playlist" or "entries" in info:
        for entry in info.get("entries") or []:
            if entry and entry.get("formats"):
                return entry
        raise UserError("No downloadable video was found at that link.")
    return info


def size_of(fmt):
    return fmt.get("filesize") or fmt.get("filesize_approx") or 0


def build_options(info):
    formats = info.get("formats") or []
    videos = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    audios = [
        f for f in formats
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]
    best_audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    audio_size = size_of(best_audio) if best_audio else 0

    options = []
    seen = set()
    for f in sorted(videos, key=lambda f: f["height"], reverse=True):
        # Bucket odd heights (e.g. 1350 for portrait reels) into a standard label.
        short_side = min(f["height"], f.get("width") or f["height"])
        label_h = min(HEIGHTS, key=lambda h: abs(h - short_side))
        if label_h in seen:
            continue
        seen.add(label_h)
        same = [v for v in videos if v["height"] == f["height"]]
        vsize = max((size_of(v) for v in same), default=0)
        has_audio = f.get("acodec") not in (None, "none")
        total = vsize + (0 if has_audio else audio_size) if vsize else 0
        badge = {2160: "4K", 1440: "2K", 1080: "HD", 720: "HD"}.get(label_h, "")
        options.append({
            "id": f"v{f['height']}",
            "kind": "video",
            "label": f"{label_h}p",
            "badge": badge,
            "ext": "MP4",
            "size": total or None,
        })
    if not options and formats:
        options.append({"id": "vbest", "kind": "video", "label": "Best", "badge": "",
                        "ext": "MP4", "size": None})
    if formats:
        options.append({
            "id": "audio",
            "kind": "audio",
            "label": "Audio",
            "badge": "MP3",
            "ext": "MP3",
            "size": int((info.get("duration") or 0) * 192_000 / 8) or None,
        })
    return options


def cleanup_loop():
    while True:
        time.sleep(60)
        now = time.time()
        with lock:
            stale = [j for j, job in jobs.items()
                     if job["status"] in ("done", "error") and now - job["updated"] > JOB_TTL]
            for jid in stale:
                shutil.rmtree(jobs.pop(jid)["dir"], ignore_errors=True)
            for token in [t for t, i in infos.items() if now - i["created"] > INFO_TTL]:
                infos.pop(token)


def update(job_id, **fields):
    with lock:
        job = jobs.get(job_id)
        if job:
            job.update(fields, updated=time.time())


def run_job(job_id, url, option, title):
    job_dir = jobs[job_id]["dir"]
    seen_files = []

    def on_progress(d):
        if d["status"] == "downloading":
            name = d.get("filename")
            if name not in seen_files:
                seen_files.append(name)
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            frac = (d.get("downloaded_bytes") or 0) / total if total else 0
            # First stream (video) fills 0-88%, the audio stream 88-97%.
            pct = frac * 88 if len(seen_files) == 1 else 88 + frac * 9
            with lock:
                job = jobs.get(job_id)
                if job:
                    job.update(
                        progress=max(job["progress"], round(pct, 1)),
                        speed=d.get("speed"),
                        eta=d.get("eta"),
                        stage="Downloading",
                        updated=time.time(),
                    )

    def on_post(d):
        if d["status"] == "started":
            update(job_id, stage="Processing", progress=98, speed=None, eta=None)

    opts = base_opts()
    opts.update({
        "outtmpl": str(Path(job_dir) / "media.%(ext)s"),
        "progress_hooks": [on_progress],
        "postprocessor_hooks": [on_post],
        "playlist_items": "1",
    })
    if option == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }]
        ext = "mp3"
    else:
        opts["format"] = "bv*+ba/b"
        sort = ["vcodec:h264", "acodec:aac"]
        if option != "vbest":
            sort.insert(0, f"height:{int(option[1:])}")
        opts["format_sort"] = sort
        opts["merge_output_format"] = "mp4"
        ext = "mp4"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = [p for p in Path(job_dir).iterdir() if p.suffix.lower() == f".{ext}"]
        if not files:
            files = [p for p in Path(job_dir).iterdir() if p.is_file() and p.suffix != ".part"]
        if not files:
            raise RuntimeError("no output file")
        out = max(files, key=lambda p: p.stat().st_size)
        safe = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", title).strip()[:120] or "clipdrop"
        update(job_id, status="done", progress=100, stage="Ready", file=str(out),
               filename=f"{safe}{out.suffix}", size=out.stat().st_size)
    except Exception as exc:  # noqa: BLE001 - surfaced to the user as a message
        app.logger.warning("download failed: %s", exc)
        update(job_id, status="error", error=friendly_error(exc))


@app.get("/")
def index():
    return send_from_directory(BASE_DIR / "static", "index.html")


@app.get("/manifest.webmanifest")
def manifest():
    return send_from_directory(BASE_DIR / "static", "manifest.webmanifest",
                               mimetype="application/manifest+json")


@app.get("/sw.js")
def service_worker():
    return send_from_directory(BASE_DIR / "static", "sw.js", mimetype="text/javascript")


@app.get("/icons/<path:name>")
def icons(name):
    return send_from_directory(BASE_DIR / "static" / "icons", name)


@app.post("/api/info")
def api_info():
    body = request.get_json(silent=True) or {}
    try:
        url, platform = detect_platform(body.get("url", ""))
        with yt_dlp.YoutubeDL({**base_opts(), "skip_download": True}) as ydl:
            info = pick_entry(ydl.extract_info(url, download=False))
        if info.get("is_live"):
            raise UserError("Live streams can't be downloaded while they're still live.")
        options = build_options(info)
        if not options:
            raise UserError("No downloadable video was found at that link.")
    except UserError as exc:
        return jsonify(error=str(exc)), 400
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("info failed: %s", exc)
        return jsonify(error=friendly_error(exc)), 422

    title = info.get("title") or "Untitled"
    if platform == "instagram":
        # Instagram "titles" are generic; the caption's first line reads better.
        caption = (info.get("description") or "").strip().splitlines()
        title = (caption[0] if caption else title) or title
    title = title[:140]
    token = uuid.uuid4().hex
    with lock:
        infos[token] = {
            "url": url,
            "title": title,
            "thumbnail": info.get("thumbnail"),
            "options": {o["id"] for o in options},
            "created": time.time(),
        }
    return jsonify(
        token=token,
        platform=platform,
        title=title,
        author=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        views=info.get("view_count"),
        has_thumbnail=bool(info.get("thumbnail")),
        options=options,
    )


@app.get("/api/thumb/<token>")
def api_thumb(token):
    """Proxy the thumbnail — Instagram's CDN blocks cross-origin hotlinking."""
    with lock:
        info = infos.get(token)
    if not info or not info["thumbnail"]:
        return "", 404
    try:
        req = urllib.request.Request(info["thumbnail"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=10) as resp:
            ctype = resp.headers.get("Content-Type", "image/jpeg")
            if not ctype.startswith("image/"):
                return "", 404
            data = resp.read(5 * 1024 * 1024)
    except Exception:  # noqa: BLE001
        return "", 404
    return Response(data, mimetype=ctype, headers={"Cache-Control": "private, max-age=3600"})


@app.post("/api/download")
def api_download():
    body = request.get_json(silent=True) or {}
    option = body.get("option")
    with lock:
        info = infos.get(body.get("token"))
        if not info or option not in info["options"]:
            return jsonify(error="This link expired. Paste it again."), 400
        active = sum(1 for j in jobs.values() if j["status"] == "running")
        if active >= MAX_ACTIVE_JOBS:
            return jsonify(error="The server is busy. Try again in a moment."), 429
        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "status": "running", "progress": 0, "stage": "Starting", "speed": None,
            "eta": None, "error": None, "file": None, "filename": None, "size": None,
            "dir": tempfile.mkdtemp(dir=WORK_DIR), "updated": time.time(),
        }
    threading.Thread(
        target=run_job, args=(job_id, info["url"], option, info["title"]), daemon=True
    ).start()
    return jsonify(job=job_id)


@app.get("/api/progress/<job_id>")
def api_progress(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify(error="Download not found."), 404
        return jsonify({k: job[k] for k in
                        ("status", "progress", "stage", "speed", "eta", "error", "size")})


@app.get("/api/file/<job_id>")
def api_file(job_id):
    with lock:
        job = jobs.get(job_id)
        if not job or job["status"] != "done" or not os.path.isfile(job["file"]):
            return "File not found or expired.", 404
        path, name = job["file"], job["filename"]
    return send_file(path, as_attachment=True, download_name=name)


threading.Thread(target=cleanup_loop, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5050))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"\n  Clipdrop running at http://localhost:{port}\n")
    app.run(host=host, port=port, threaded=True)
