from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
import yt_dlp
import os, re, tempfile, shutil, subprocess, sys, asyncio, logging
from typing import Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("socialdl")

UPDATE_INTERVAL_HOURS = 12
_last_update: datetime | None = None
_current_version: str = "unknown"

def get_ytdlp_version() -> str:
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return "unknown"

def update_ytdlp() -> dict:
    global _last_update, _current_version
    before = get_ytdlp_version()
    logger.info(f"[yt-dlp] Current: {before} — checking for updates…")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "--quiet"],
                       capture_output=True, text=True, timeout=120)
        after = get_ytdlp_version()
        _last_update = datetime.utcnow()
        _current_version = after
        if before != after:
            logger.info(f"[yt-dlp] Updated: {before} → {after}")
            return {"updated": True, "from": before, "to": after}
        logger.info(f"[yt-dlp] Already up to date: {after}")
        return {"updated": False, "version": after}
    except subprocess.TimeoutExpired:
        return {"updated": False, "error": "timeout"}
    except Exception as e:
        return {"updated": False, "error": str(e)}

async def scheduled_updater():
    while True:
        await asyncio.sleep(UPDATE_INTERVAL_HOURS * 3600)
        await asyncio.get_event_loop().run_in_executor(None, update_ytdlp)

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _current_version
    _current_version = get_ytdlp_version()
    logger.info(f"[SocialDL] Startup. yt-dlp: {_current_version}")
    asyncio.create_task(asyncio.get_event_loop().run_in_executor(None, update_ytdlp))
    task = asyncio.create_task(scheduled_updater())
    yield
    task.cancel()

# Read allowed origins from env for production CORS
ALLOWED_ORIGINS = os.environ.get("ALLOWED_ORIGINS", "*").split(",")
# On HF Spaces, set ALLOWED_ORIGINS to your Netlify frontend URL in Space settings

app = FastAPI(title="SocialDL API", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPPORTED_PLATFORMS = {
    "youtube.com": "YouTube", "youtu.be": "YouTube",
    "instagram.com": "Instagram", "tiktok.com": "TikTok",
    "twitter.com": "Twitter/X", "x.com": "Twitter/X",
    "facebook.com": "Facebook", "fb.watch": "Facebook",
    "vimeo.com": "Vimeo", "twitch.tv": "Twitch",
    "reddit.com": "Reddit", "pinterest.com": "Pinterest",
    "linkedin.com": "LinkedIn", "dailymotion.com": "Dailymotion",
    "soundcloud.com": "SoundCloud", "bilibili.com": "Bilibili",
}

def detect_platform(url: str) -> str:
    for domain, name in SUPPORTED_PLATFORMS.items():
        if domain in url:
            return name
    return "Unknown"

class InfoRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    url: str
    format_id: Optional[str] = "best"
    audio_only: Optional[bool] = False

@app.get("/")
async def root():
    return {"message": "SocialDL API is running", "version": "1.0.0"}

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "ytdlp_version": _current_version,
        "last_update_check": _last_update.isoformat() if _last_update else None,
        "next_update_check": (
            (_last_update + timedelta(hours=UPDATE_INTERVAL_HOURS)).isoformat()
            if _last_update else "on startup"
        ),
    }

@app.post("/api/update-ytdlp")
async def trigger_update():
    result = await asyncio.get_event_loop().run_in_executor(None, update_ytdlp)
    return result

@app.get("/api/platforms")
async def list_platforms():
    return {"platforms": list(set(SUPPORTED_PLATFORMS.values()))}

@app.post("/api/info")
async def get_info(req: InfoRequest):
    platform = detect_platform(req.url)
    ydl_opts = {"quiet": True, "no_warnings": True, "skip_download": True}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(req.url, download=False)
        if not info:
            raise HTTPException(status_code=400, detail="Could not extract video info")

        formats, seen_labels = [], set()
        for f in reversed(info.get("formats", [])):
            vcodec, acodec = f.get("vcodec", "none"), f.get("acodec", "none")
            height = f.get("height")
            if vcodec != "none" and height:
                label = f"{height}p"
                if label not in seen_labels:
                    seen_labels.add(label)
                    formats.append({
                        "format_id": f.get("format_id", ""),
                        "label": label, "ext": f.get("ext", "mp4"),
                        "height": height,
                        "filesize": f.get("filesize") or f.get("filesize_approx"),
                        "type": "video", "has_audio": acodec != "none",
                    })
        formats.append({"format_id": "bestaudio", "label": "Audio only (MP3)",
                        "ext": "mp3", "height": None, "filesize": None,
                        "type": "audio", "has_audio": True})

        video_fmts = sorted([f for f in formats if f["type"] == "video"],
                            key=lambda x: x["height"] or 0, reverse=True)
        unique, seen = [], set()
        for f in video_fmts + [f for f in formats if f["type"] == "audio"]:
            if f["label"] not in seen:
                seen.add(f["label"]); unique.append(f)

        thumbnail = info.get("thumbnail", "")
        thumbs = info.get("thumbnails", [])
        if thumbs:
            best = max(thumbs, key=lambda t: (t.get("width") or 0)*(t.get("height") or 0), default=None)
            if best: thumbnail = best.get("url", thumbnail)

        return {
            "platform": platform, "title": info.get("title", "Untitled"),
            "description": (info.get("description") or "")[:300],
            "duration": info.get("duration"), "thumbnail": thumbnail,
            "uploader": info.get("uploader") or info.get("channel", ""),
            "view_count": info.get("view_count"), "upload_date": info.get("upload_date"),
            "formats": unique, "url": req.url,
        }
    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Unsupported URL" in msg:
            raise HTTPException(status_code=400, detail="Unsupported or private URL.")
        raise HTTPException(status_code=400, detail=f"Could not fetch info: {msg[:200]}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:200]}")

@app.post("/api/download")
async def download_video(req: DownloadRequest):
    tmpdir = tempfile.mkdtemp()
    try:
        if req.audio_only or req.format_id == "bestaudio":
            ydl_opts = {
                "quiet": True, "no_warnings": True, "format": "bestaudio/best",
                "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
                "postprocessors": [{"key": "FFmpegExtractAudio",
                                    "preferredcodec": "mp3", "preferredquality": "192"}],
            }
        else:
            fmt = req.format_id if req.format_id and req.format_id != "best" else "bestvideo+bestaudio/best"
            ydl_opts = {
                "quiet": True, "no_warnings": True,
                "format": f"{fmt}+bestaudio/{fmt}/best",
                "outtmpl": os.path.join(tmpdir, "%(title)s.%(ext)s"),
                "merge_output_format": "mp4",
            }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(req.url, download=True)

        files = os.listdir(tmpdir)
        if not files:
            raise HTTPException(status_code=500, detail="Download produced no file")

        filepath = os.path.join(tmpdir, files[0])
        safe_name = re.sub(r'[^\w\s\-.]', '', files[0]).strip() or "download.mp4"

        def file_iterator():
            try:
                with open(filepath, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk
            finally:
                shutil.rmtree(tmpdir, ignore_errors=True)

        media_type = "audio/mpeg" if (req.audio_only or req.format_id == "bestaudio") else "video/mp4"
        return StreamingResponse(file_iterator(), media_type=media_type,
                                 headers={"Content-Disposition": f'attachment; filename="{safe_name}"',
                                          "X-Filename": safe_name})
    except yt_dlp.utils.DownloadError as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Download error: {str(e)[:300]}")
    except HTTPException:
        shutil.rmtree(tmpdir, ignore_errors=True); raise
    except Exception as e:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"Server error: {str(e)[:200]}")
