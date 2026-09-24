import os
import re
import sys
import json
import time
import ctypes
import asyncio
from typing import Optional, Union, List, Dict, Any
from dotenv import load_dotenv
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from telethon import TelegramClient
from telethon.tl.types import DocumentAttributeVideo

# ==============================================================================
# 1. LOAD CONFIGURATION FROM ENVIRONMENT (.env)
# ==============================================================================
load_dotenv()

raw_api_id = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()

# Fallback reader if python-dotenv is not present
if not raw_api_id or not API_HASH:
    if os.path.exists(".env"):
        with open(".env", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ[k.strip()] = v.strip().strip("'").strip('"')
        raw_api_id = os.getenv("TELEGRAM_API_ID")
        API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()

if not raw_api_id or not API_HASH:
    sys.exit("[Error] TELEGRAM_API_ID or TELEGRAM_API_HASH missing in .env file.")

try:
    API_ID = int(raw_api_id.strip())
except ValueError:
    sys.exit("[Error] TELEGRAM_API_ID in .env must be an integer.")

# ==============================================================================
# 2. WINDOWS POWER MANAGEMENT (PREVENTS SLEEP WHILE DOWNLOADING)
# ==============================================================================
STATE_FILE = "downloads_state.json"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

def prevent_sleep():
    """Keeps CPU and Wi-Fi active during download."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            pass

def allow_sleep():
    """Restores default Windows sleep timers."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass

def sanitize_filename(name: str) -> str:
    clean = re.sub(r'[\\/*?:"<>|]', "_", name)
    return clean.strip().rstrip('.')

def format_seconds(seconds: float) -> str:
    if seconds <= 0 or seconds > 360000:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

# ==============================================================================
# 3. APP STATE & DISK PERSISTENCE
# ==============================================================================
class DownloadCancelledException(Exception):
    pass

class AppState:
    def __init__(self):
        self.is_downloading: bool = False
        self.cancel_requested: bool = False
        self.can_resume: bool = False
        self.is_paused: bool = False
        
        self.current_file: str = ""
        self.current_index: int = 0
        self.total_files: int = 0
        self.percent: float = 0.0
        self.downloaded_mb: int = 0
        self.total_mb: int = 0
        self.speed_mbps: float = 0.0
        self.eta_str: str = "--:--"
        self.log: str = "Ready."
        
        self.target_url: str = ""
        self.default_folder: str = ""
        self.download_folder: str = ""
        self.selected_ids: List[int] = []
        self.scanned_items: List[Dict[str, Any]] = []
        self.active_peer: Any = None
        self.active_topic: Optional[int] = None
        
        self.last_bytes: int = 0
        self.last_time: float = 0.0

        self.load_from_disk()

    def __setitem__(self, key: str, value: Any):
        setattr(self, key, value)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

    def save_to_disk(self):
        try:
            data = {
                "target_url": self.target_url,
                "default_folder": self.default_folder,
                "download_folder": self.download_folder,
                "selected_ids": self.selected_ids,
                "can_resume": self.can_resume,
                "is_paused": self.is_paused,
                "current_index": self.current_index,
                "total_files": self.total_files,
                "scanned_items": self.scanned_items,
                "log": self.log
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f)
        except Exception:
            pass

    def load_from_disk(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.target_url = data.get("target_url", "")
                    self.default_folder = data.get("default_folder", "")
                    self.download_folder = data.get("download_folder", "")
                    self.selected_ids = data.get("selected_ids", [])
                    self.can_resume = data.get("can_resume", False)
                    self.is_paused = data.get("is_paused", False)
                    self.current_index = data.get("current_index", 0)
                    self.total_files = data.get("total_files", 0)
                    self.scanned_items = data.get("scanned_items", [])
                    if self.can_resume:
                        self.log = f"Paused at [{self.current_index}/{self.total_files}]. Click 'Resume Download' to continue."
            except Exception:
                pass

state = AppState()
app = FastAPI(title="Lecture Batch Downloader Pro")

client: Any = TelegramClient('telegram_session', API_ID, API_HASH)

class ScanRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    selected_ids: List[int]
    folder_name: str

def parse_telegram_url(url: str) -> tuple[Union[int, str], Optional[int]]:
    clean_url = url.strip()
    m_private = re.search(r"t\.me/c/(\d+)(?:/(\d+))?", clean_url)
    if m_private:
        peer: Union[int, str] = int(f"-100{m_private.group(1)}")
        topic_id = int(m_private.group(2)) if m_private.group(2) else None
        return peer, topic_id

    m_public = re.search(r"t\.me/([a-zA-Z0-9_]+)(?:/(\d+))?", clean_url)
    if m_public:
        peer = str(m_public.group(1))
        topic_id = int(m_public.group(2)) if m_public.group(2) else None
        return peer, topic_id

    raise ValueError("Invalid Telegram URL format.")

@app.on_event("startup")
async def startup_event():
    await client.connect()
    if await client.is_user_authorized():
        print("[Telegram] Ready & Authenticated.")
    else:
        print("[Telegram] Session not authorized. Run login once.")

def speed_progress_callback(current: int, total: int):
    if state.cancel_requested:
        raise DownloadCancelledException("Download cancelled by user.")

    now = time.time()
    dt = now - state.last_time

    if dt >= 0.8 or current == total:
        bytes_delta = current - state.last_bytes
        speed = (bytes_delta / dt) / (1024 * 1024) if dt > 0 else 0.0
        state.speed_mbps = round(speed, 2)
        
        remaining_bytes = total - current
        state.eta_str = format_seconds(remaining_bytes / (speed * 1024 * 1024)) if speed > 0 else "--:--"

        state.last_time = now
        state.last_bytes = current

    if total > 0:
        state.percent = round((current / total) * 100, 1)
        state.downloaded_mb = current // (1024 * 1024)
        state.total_mb = total // (1024 * 1024)

# ==============================================================================
# 4. DOWNLOAD ENGINE & OFFLINE PORTAL BUILDER
# ==============================================================================
def generate_offline_portal(download_dir: str, file_list: List[Dict[str, str]]):
    first_file = file_list[0]['filename'] if file_list else ''
    first_title = file_list[0]['title'] if file_list else 'No lectures'
    total_count = len(file_list)

    items_html = []
    for idx, item in enumerate(file_list):
        active_class = "bg-blue-600/20 text-blue-300 font-medium" if idx == 0 else "text-slate-300"
        safe_file = item['filename'].replace('"', '&quot;')
        safe_title = item['title'].replace('"', '&quot;').replace("'", "\\'")
        display_title = item['title'].replace('<', '&lt;').replace('>', '&gt;')
        
        row = (
            f'<button onclick="playLecture(\'{safe_file}\', \'{safe_title}\', this)" '
            f'class="playlist-btn w-full text-left p-3 rounded-lg text-xs hover:bg-slate-800 transition flex items-start gap-2 {active_class}">'
            f'<span class="mt-0.5 font-mono opacity-60">#{idx+1:02d}</span>'
            f'<span class="line-clamp-2 leading-relaxed">{display_title}</span>'
            f'</button>'
        )
        items_html.append(row)

    playlist_markup = "\n".join(items_html)

    html_template = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Lecture Study Dashboard</title>
    <script src="https://cdn.tailwindcss.com"></script>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen flex flex-col">
    <header class="bg-slate-900 border-b border-slate-800 p-4 px-6 flex justify-between items-center">
        <h1 class="text-lg font-bold text-blue-400">Offline Lecture Study Player</h1>
        <span class="text-xs bg-blue-950 border border-blue-800 text-blue-300 px-3 py-1 rounded-full">__TOTAL__ Lectures</span>
    </header>
    <div class="flex flex-1 overflow-hidden">
        <main class="flex-1 p-6 flex flex-col items-center justify-center bg-black/40">
            <video id="player" controls class="w-full max-w-5xl rounded-xl shadow-2xl bg-black max-h-[75vh]" src="__FIRST_FILE__"></video>
            <div id="videoTitle" class="mt-4 text-base font-semibold text-slate-300 text-center">__FIRST_TITLE__</div>
        </main>
        <aside class="w-96 bg-slate-900 border-l border-slate-800 flex flex-col">
            <div class="p-4 border-b border-slate-800 font-semibold text-sm text-slate-400 uppercase tracking-wider">Lectures Playlist</div>
            <div class="overflow-y-auto flex-1 p-2 space-y-1">
                __PLAYLIST__
            </div>
        </aside>
    </div>
    <script>
        function playLecture(file, title, elem) {
            const player = document.getElementById('player');
            player.src = file;
            player.play();
            document.getElementById('videoTitle').innerText = title;
            document.querySelectorAll('.playlist-btn').forEach(b => {
                b.classList.remove('bg-blue-600/20', 'text-blue-300', 'font-medium');
                b.classList.add('text-slate-300');
            });
            elem.classList.add('bg-blue-600/20', 'text-blue-300', 'font-medium');
            elem.classList.remove('text-slate-300');
        }
    </script>
</body>
</html>"""

    final_html = (
        html_template
        .replace("__TOTAL__", str(total_count))
        .replace("__FIRST_FILE__", first_file)
        .replace("__FIRST_TITLE__", first_title)
        .replace("__PLAYLIST__", playlist_markup)
    )

    try:
        with open(os.path.join(download_dir, "study_index.html"), "w", encoding="utf-8") as f:
            f.write(final_html)
    except Exception as e:
        print(f"[Portal] Failed to create study_index.html: {e}")

async def run_batch_download(selected_ids: List[int], download_dir: str):
    state.is_downloading = True
    state.cancel_requested = False
    state.can_resume = False
    state.is_paused = False
    state.selected_ids = selected_ids
    state.download_folder = download_dir
    state.log = "Starting download queue..."
    state.save_to_disk()
    prevent_sleep()

    downloaded_records: List[Dict[str, str]] = []

    try:
        os.makedirs(download_dir, exist_ok=True)
        raw_entity = await client.get_entity(state.active_peer)
        entity = raw_entity[0] if isinstance(raw_entity, list) else raw_entity

        id_to_meta = {item['id']: item for item in state.scanned_items if item['id'] in selected_ids}
        total = len(selected_ids)
        state.total_files = total

        for index, msg_id in enumerate(selected_ids, start=1):
            if state.cancel_requested:
                state.is_paused = True
                state.can_resume = True
                state.log = f"Paused at [{index}/{total}]. Click 'Resume Download' to continue."
                break

            raw_msg = await client.get_messages(entity, ids=msg_id)
            msg: Any = raw_msg[0] if isinstance(raw_msg, list) else raw_msg
            if not msg:
                continue

            meta = id_to_meta.get(msg_id, {})
            filename = f"{index:02d}_{meta.get('clean_filename', f'file_{msg_id}.mp4')}"
            final_path = os.path.join(download_dir, filename)
            part_path = final_path + ".part"

            state.current_index = index
            state.current_file = filename
            state.percent = 0.0
            state.speed_mbps = 0.0
            state.eta_str = "--:--"

            # Fast skip finished files
            if os.path.exists(final_path):
                state.log = f"[{index}/{total}] Already downloaded: {filename}"
                downloaded_records.append({"filename": filename, "title": meta.get('title', filename)})
                await asyncio.sleep(0.1)
                continue

            state.log = f"[{index}/{total}] Downloading: {filename}"
            state.last_time = time.time()
            state.last_bytes = 0

            try:
                await client.download_media(msg, file=part_path, progress_callback=speed_progress_callback)
            except (DownloadCancelledException, asyncio.CancelledError):
                state.is_paused = True
                state.can_resume = True
                state.log = f"Paused at [{index}/{total}]. Click 'Resume Download' to continue."
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                break
            except Exception as e:
                if state.cancel_requested:
                    state.is_paused = True
                    state.can_resume = True
                    state.log = f"Paused at [{index}/{total}]. Click 'Resume Download' to continue."
                    if os.path.exists(part_path):
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass
                    break
                else:
                    state.log = f"Skipping error on {filename}: {str(e)}"
                    state.can_resume = True
                    state.is_paused = True
                    continue

            if state.cancel_requested:
                state.is_paused = True
                state.can_resume = True
                state.log = f"Paused at [{index}/{total}]. Click 'Resume Download' to continue."
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except Exception:
                        pass
                break

            if os.path.exists(part_path):
                if os.path.exists(final_path):
                    os.remove(final_path)
                os.rename(part_path, final_path)
                downloaded_records.append({"filename": filename, "title": meta.get('title', filename)})

        if state.cancel_requested or state.is_paused:
            state.can_resume = True
            state.is_paused = True
        else:
            state.can_resume = False
            state.is_paused = False
            if downloaded_records:
                generate_offline_portal(download_dir, downloaded_records)
                state.log = f"All done! Offline study hub generated at '{download_dir}/study_index.html'."

    except Exception as e:
        state.can_resume = True
        state.is_paused = True
        state.log = f"Halted: {str(e)}. Click Resume Download to retry."
    finally:
        state.is_downloading = False
        state.cancel_requested = False
        state.speed_mbps = 0.0
        state.eta_str = "--:--"
        if state.current_index < state.total_files:
            state.can_resume = True
            state.is_paused = True
        state.save_to_disk()
        allow_sleep()

# ==============================================================================
# 5. REST APIS
# ==============================================================================
@app.get("/api/initial-state")
async def get_initial_state():
    return {
        "target_url": state.target_url,
        "default_folder": state.default_folder,
        "download_folder": state.download_folder,
        "scanned_items": state.scanned_items,
        "is_downloading": state.is_downloading,
        "can_resume": state.can_resume,
        "is_paused": state.is_paused,
        "current_index": state.current_index,
        "total_files": state.total_files,
        "log": state.log
    }

@app.post("/api/scan")
async def scan_topic(req: ScanRequest):
    try:
        peer, topic_id = parse_telegram_url(req.url)
        state.active_peer = peer
        state.active_topic = topic_id
        state.target_url = req.url.strip()

        await client.get_dialogs()
        raw_entity = await client.get_entity(peer)
        entity = raw_entity[0] if isinstance(raw_entity, list) else raw_entity

        items: List[Dict[str, Any]] = []
        messages_iter = client.iter_messages(entity, reply_to=topic_id) if topic_id is not None else client.iter_messages(entity)

        async for raw_m in messages_iter:
            msg: Any = raw_m
            is_video = bool(msg.video)
            is_pdf = bool(msg.document and msg.document.mime_type == 'application/pdf')
            
            if not is_video and not is_pdf:
                if msg.document and msg.document.mime_type and msg.document.mime_type.startswith('video/'):
                    is_video = True

            if not (is_video or is_pdf):
                continue

            file_name = msg.file.name if msg.file and msg.file.name else None
            caption = msg.text or ""
            duration_str = ""

            if is_video and msg.document:
                for attr in msg.document.attributes:
                    if isinstance(attr, DocumentAttributeVideo):
                        duration_str = format_seconds(attr.duration)
                        break

            title_candidate = caption.split('\n')[0].strip() if caption else (file_name or f"Lecture_{msg.id}")
            title = sanitize_filename(title_candidate)
            ext = ".pdf" if is_pdf else ".mp4"

            clean_filename = f"{title[:40]}{ext}" if not file_name else sanitize_filename(file_name)

            items.append({
                "id": msg.id,
                "title": title[:70],
                "clean_filename": clean_filename,
                "size_mb": round((msg.file.size or 0) / (1024 * 1024), 1) if msg.file else 0,
                "duration": duration_str,
                "type": "video" if is_video else "pdf"
            })

        items.reverse()
        state.scanned_items = items
        default_folder = f"Lectures_Topic_{topic_id}" if topic_id else "Lectures_Channel"
        state.default_folder = default_folder
        state.save_to_disk()
        return {"status": "ok", "items": items, "default_folder": default_folder}

    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.post("/api/start-download")
async def start_download(req: DownloadRequest, bg_tasks: BackgroundTasks):
    if state.is_downloading:
        return JSONResponse({"status": "error", "message": "Download in progress!"}, status_code=400)
    if not req.selected_ids:
        return JSONResponse({"status": "error", "message": "No files selected."}, status_code=400)

    bg_tasks.add_task(run_batch_download, req.selected_ids, req.folder_name)
    return {"status": "started"}

@app.post("/api/resume")
async def resume_download(bg_tasks: BackgroundTasks):
    if state.is_downloading:
        return JSONResponse({"status": "error", "message": "Download is already running!"}, status_code=400)

    if not state.selected_ids and state.scanned_items:
        state.selected_ids = [item['id'] for item in state.scanned_items]
        if not state.download_folder:
            state.download_folder = state.default_folder or "Downloads"

    if not state.selected_ids or not state.download_folder:
        return JSONResponse({"status": "error", "message": "No paused download found to resume."}, status_code=400)

    state.is_paused = False
    state.can_resume = False
    bg_tasks.add_task(run_batch_download, state.selected_ids, state.download_folder)
    return {"status": "resumed"}

@app.post("/api/cancel")
async def cancel_download():
    if state.is_downloading:
        state.cancel_requested = True
        state.is_paused = True
        state.can_resume = True
        state.log = "Pausing download... Click 'Resume Download' to continue."
        state.save_to_disk()
        return {"status": "cancelling"}
    return {"status": "idle"}

@app.get("/api/status")
async def get_status():
    return {
        "is_downloading": state.is_downloading,
        "can_resume": state.can_resume,
        "is_paused": state.is_paused,
        "current_file": state.current_file,
        "current_index": state.current_index,
        "total_files": state.total_files,
        "percent": state.percent,
        "downloaded_mb": state.downloaded_mb,
        "total_mb": state.total_mb,
        "speed_mbps": state.speed_mbps,
        "eta": state.eta_str,
        "log": state.log
    }

# ==============================================================================
# 6. DASHBOARD FRONTEND UI
# ==============================================================================
@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Lecture Downloader & Offline Study Hub</title>
        <script src="https://cdn.tailwindcss.com"></script>
    </head>
    <body class="bg-slate-950 text-slate-100 min-h-screen p-6 font-sans">
        <div class="max-w-4xl mx-auto space-y-6">
            
            <div class="flex items-center justify-between border-b border-slate-800 pb-5">
                <div>
                    <h1 class="text-2xl font-bold tracking-tight text-white flex items-center gap-2">
                        <span class="p-2 bg-blue-600 rounded-lg text-lg">⚡</span> Batch Lecture Studio
                    </h1>
                    <p class="text-slate-400 text-xs mt-1">Sequential lecture downloader with pause/resume and offline video player.</p>
                </div>
                <div id="liveBadge" class="px-3 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-300">Idle</div>
            </div>

            <div class="bg-slate-900 border border-slate-800 rounded-2xl p-5 shadow-xl space-y-4">
                <label class="text-xs font-bold text-slate-300 uppercase tracking-wider">Telegram Topic or Channel URL</label>
                <div class="flex gap-2">
                    <input id="urlInput" type="text" placeholder="https://t.me/c/2341143200/1869 or https://t.me/Gate_cse_2026/620" 
                        class="flex-1 px-4 py-3 rounded-xl bg-slate-950 border border-slate-700 text-slate-200 focus:outline-none focus:border-blue-500 font-mono text-sm" />
                    <button id="scanBtn" onclick="scanTopic()" 
                        class="bg-blue-600 hover:bg-blue-500 font-semibold px-6 py-3 rounded-xl transition flex items-center gap-2 text-sm shadow-lg shadow-blue-600/30">
                        Scan Lectures
                    </button>
                </div>
            </div>

            <div id="monitorCard" class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4">
                <div class="flex justify-between items-center text-sm">
                    <span id="monFile" class="font-medium text-slate-200 truncate max-w-[420px]">Ready to scan</span>
                    <span id="monCounter" class="font-mono text-xs text-slate-400">0 / 0 Files</span>
                </div>

                <div class="space-y-1">
                    <div class="w-full bg-slate-950 h-3 rounded-full overflow-hidden border border-slate-800">
                        <div id="progressBar" class="bg-gradient-to-r from-blue-600 to-indigo-500 h-full transition-all duration-300 w-0"></div>
                    </div>
                    <div class="flex justify-between text-[11px] font-mono text-slate-400 pt-1">
                        <span id="sizeDetails">0 MB / 0 MB (0%)</span>
                        <div class="flex gap-4">
                            <span id="speedMeter">0.0 MB/s</span>
                            <span id="etaMeter">ETA: --:--</span>
                        </div>
                    </div>
                </div>

                <div class="flex justify-between items-center border-t border-slate-800/80 pt-4">
                    <p id="logText" class="text-xs text-blue-400 font-mono truncate max-w-[460px]">Idle</p>
                    <div class="flex items-center gap-2">
                        <!-- Dynamic Smart Action Button -->
                        <button id="actionBtn" onclick="handleActionClick()" disabled
                            class="bg-slate-800 text-slate-500 px-4 py-1.5 rounded-lg text-xs font-semibold transition cursor-not-allowed">
                            Stop Download
                        </button>
                    </div>
                </div>
            </div>

            <div id="queueSection" class="bg-slate-900 border border-slate-800 rounded-2xl p-6 shadow-xl space-y-4 hidden">
                <div class="flex flex-col sm:flex-row sm:items-center justify-between gap-4 border-b border-slate-800 pb-4">
                    <div>
                        <h2 class="text-base font-bold text-white">Discovered Lectures & Materials</h2>
                        <p class="text-xs text-slate-400" id="queueSummary">Select lectures to download.</p>
                    </div>
                    <div class="flex items-center gap-2">
                        <input id="folderInput" type="text" placeholder="Folder Name" 
                            class="px-3 py-2 rounded-lg bg-slate-950 border border-slate-700 text-xs text-slate-200 font-mono focus:outline-none focus:border-blue-500" />
                        <button id="startDownloadBtn" onclick="startDownload()" 
                            class="bg-blue-600 hover:bg-blue-500 text-white font-semibold px-4 py-2 rounded-lg text-xs transition shadow-lg shadow-blue-600/30">
                            Download Selected
                        </button>
                    </div>
                </div>

                <div class="flex justify-between items-center text-xs text-slate-400 pb-2">
                    <div class="flex gap-3">
                        <button onclick="toggleAll(true)" class="hover:text-white underline">Select All</button>
                        <button onclick="toggleAll(false)" class="hover:text-white underline">Deselect All</button>
                    </div>
                    <div class="flex items-center gap-4">
                        <label class="flex items-center gap-1.5 cursor-pointer">
                            <input id="filterPdf" type="checkbox" checked onchange="filterType('pdf', this.checked)" class="rounded bg-slate-950 border-slate-700">
                            <span>Include Notes (PDFs)</span>
                        </label>
                    </div>
                </div>

                <div class="max-h-[380px] overflow-y-auto space-y-1.5 pr-1" id="itemsList"></div>
            </div>

        </div>

        <script>
            let scannedItems = [];
            let currentMode = "idle"; // "downloading" | "paused" | "idle"

            window.addEventListener('DOMContentLoaded', async () => {
                try {
                    const res = await fetch('/api/initial-state');
                    const data = await res.json();

                    if (data.target_url) document.getElementById('urlInput').value = data.target_url;
                    if (data.default_folder) document.getElementById('folderInput').value = data.default_folder;
                    if (data.log) document.getElementById('logText').innerText = data.log;

                    if (data.scanned_items && data.scanned_items.length > 0) {
                        scannedItems = data.scanned_items;
                        renderQueue();
                        document.getElementById('queueSection').classList.remove('hidden');
                    }

                    if (data.can_resume || data.is_paused) {
                        setActionButtonMode("paused");
                    }
                } catch (e) {
                    console.error("State restore error:", e);
                }
            });

            function setActionButtonMode(mode) {
                currentMode = mode;
                const btn = document.getElementById('actionBtn');
                const badge = document.getElementById('liveBadge');

                if (mode === "downloading") {
                    btn.disabled = false;
                    btn.innerText = "⏹ Stop Download";
                    btn.className = "bg-red-500/20 hover:bg-red-500/30 text-red-300 border border-red-500/30 px-4 py-1.5 rounded-lg text-xs font-semibold transition cursor-pointer";
                    badge.innerText = "Downloading...";
                    badge.className = "px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/30";
                } else if (mode === "paused") {
                    btn.disabled = false;
                    btn.innerHTML = "<span>▶</span> Resume Download";
                    btn.className = "bg-emerald-600 hover:bg-emerald-500 text-white px-4 py-1.5 rounded-lg text-xs font-semibold transition shadow-lg shadow-emerald-600/30 cursor-pointer flex items-center gap-1.5";
                    badge.innerText = "Paused";
                    badge.className = "px-3 py-1 rounded-full text-xs font-semibold bg-amber-500/20 text-amber-300 border border-amber-500/30";
                } else {
                    btn.disabled = true;
                    btn.innerText = "Stop Download";
                    btn.className = "bg-slate-800 text-slate-500 px-4 py-1.5 rounded-lg text-xs font-semibold cursor-not-allowed";
                    badge.innerText = "Idle";
                    badge.className = "px-3 py-1 rounded-full text-xs font-semibold bg-slate-800 text-slate-300";
                }
            }

            async function handleActionClick() {
                if (currentMode === "downloading") {
                    const btn = document.getElementById('actionBtn');
                    btn.disabled = true;
                    btn.innerText = "Pausing...";
                    await fetch('/api/cancel', { method: 'POST' });
                    setActionButtonMode("paused");
                } else if (currentMode === "paused") {
                    const btn = document.getElementById('actionBtn');
                    btn.disabled = true;
                    btn.innerText = "Resuming...";
                    try {
                        const res = await fetch('/api/resume', { method: 'POST' });
                        const data = await res.json();
                        if (!res.ok) {
                            alert(data.message || 'Failed to resume.');
                            setActionButtonMode("paused");
                        }
                    } catch(e) {
                        alert('Resume error: ' + e);
                        setActionButtonMode("paused");
                    }
                }
            }

            async function scanTopic() {
                const url = document.getElementById('urlInput').value.trim();
                if (!url) return alert('Enter a Telegram URL!');

                const btn = document.getElementById('scanBtn');
                btn.disabled = true;
                btn.innerText = "Scanning...";

                try {
                    const res = await fetch('/api/scan', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ url: url })
                    });
                    const data = await res.json();
                    if (!res.ok) {
                        alert(data.message || 'Scan failed.');
                    } else {
                        scannedItems = data.items;
                        document.getElementById('folderInput').value = data.default_folder;
                        renderQueue();
                        document.getElementById('queueSection').classList.remove('hidden');
                    }
                } catch(e) {
                    alert('Server error: ' + e);
                } finally {
                    btn.disabled = false;
                    btn.innerText = "Scan Lectures";
                }
            }

            function renderQueue() {
                const container = document.getElementById('itemsList');
                container.innerHTML = '';
                let totalMB = 0;

                scannedItems.forEach((item, index) => {
                    totalMB += item.size_mb;
                    const row = document.createElement('div');
                    row.className = `flex items-center justify-between p-3 rounded-xl bg-slate-950/60 border border-slate-800/80 text-xs item-row ${item.type}`;
                    row.innerHTML = `
                        <div class="flex items-center gap-3 truncate max-w-[550px]">
                            <input type="checkbox" value="${item.id}" checked class="item-chk rounded bg-slate-900 border-slate-700" />
                            <span class="font-mono text-slate-500 font-semibold">#${(index + 1).toString().padStart(2, '0')}</span>
                            <span class="px-1.5 py-0.5 rounded text-[10px] font-bold ${item.type === 'video' ? 'bg-blue-600/20 text-blue-400' : 'bg-red-600/20 text-red-400'}">${item.type.toUpperCase()}</span>
                            <span class="text-slate-200 truncate font-medium">${item.title}</span>
                        </div>
                        <div class="flex items-center gap-4 text-slate-400 font-mono text-[11px]">
                            <span>${item.duration || ''}</span>
                            <span>${item.size_mb} MB</span>
                        </div>
                    `;
                    container.appendChild(row);
                });

                document.getElementById('queueSummary').innerText = `${scannedItems.length} lectures found (~${Math.round(totalMB / 1024 * 10) / 10} GB total).`;
            }

            function toggleAll(status) {
                document.querySelectorAll('.item-chk').forEach(chk => {
                    if (chk.closest('.item-row').style.display !== 'none') {
                        chk.checked = status;
                    }
                });
            }

            function filterType(type, isChecked) {
                document.querySelectorAll(`.item-row.${type}`).forEach(el => {
                    el.style.display = isChecked ? 'flex' : 'none';
                    const chk = el.querySelector('.item-chk');
                    if (chk) chk.checked = isChecked;
                });
            }

            async function startDownload() {
                const checked = Array.from(document.querySelectorAll('.item-chk:checked')).map(c => parseInt(c.value));
                if (checked.length === 0) return alert('Select at least one lecture!');

                const folder = document.getElementById('folderInput').value.trim() || 'Downloads';

                try {
                    const res = await fetch('/api/start-download', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ selected_ids: checked, folder_name: folder })
                    });
                    const data = await res.json();
                    if (!res.ok) alert(data.message);
                } catch(e) {
                    alert('Error: ' + e);
                }
            }

            setInterval(async () => {
                try {
                    const res = await fetch('/api/status');
                    const d = await res.json();

                    document.getElementById('logText').innerText = d.log;

                    if (d.is_downloading) {
                        setActionButtonMode("downloading");
                        document.getElementById('monCounter').innerText = `${d.current_index} / ${d.total_files} Files`;
                        document.getElementById('monFile').innerText = d.current_file;
                        document.getElementById('progressBar').style.width = `${d.percent}%`;
                        document.getElementById('sizeDetails').innerText = `${d.downloaded_mb} MB / ${d.total_mb} MB (${d.percent}%)`;
                        document.getElementById('speedMeter').innerText = `${d.speed_mbps} MB/s`;
                        document.getElementById('etaMeter').innerText = `ETA: ${d.eta}`;
                    } else {
                        document.getElementById('speedMeter').innerText = "0.0 MB/s";
                        document.getElementById('etaMeter').innerText = "ETA: --:--";

                        if (d.can_resume || d.is_paused) {
                            setActionButtonMode("paused");
                        } else if (d.total_files > 0 && d.current_index === d.total_files) {
                            const btn = document.getElementById('actionBtn');
                            btn.disabled = true;
                            btn.innerText = "Completed";
                            btn.className = "bg-slate-800 text-slate-500 px-4 py-1.5 rounded-lg text-xs font-semibold cursor-not-allowed";

                            const badge = document.getElementById('liveBadge');
                            badge.innerText = "Completed";
                            badge.className = "px-3 py-1 rounded-full text-xs font-semibold bg-emerald-500/20 text-emerald-300 border border-emerald-500/30";
                            document.getElementById('progressBar').style.width = '100%';
                        } else {
                            setActionButtonMode("idle");
                        }
                    }
                } catch(e) {}
            }, 1000);
        </script>
    </body>
    </html>
    """

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=False)