import os
import re
import time
import socket
import secrets
import logging
import asyncio
import urllib.request
from typing import List, Optional
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks, Request, Response, Depends
from fastapi.responses import FileResponse, JSONResponse

from downloader import (
    get_session_ctx,
    get_telegram_auth_status,
    setup_api_credentials,
    send_telegram_login_code,
    complete_telegram_sign_in,
    logout_telegram_session,
    scan_channel_or_topic,
    run_batch_download
)

class NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/api/status" in msg or "/api/initial-state" in msg or "com.chrome.devtools" in msg:
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(NoiseFilter())

app = FastAPI(title="Lecture Batch Downloader Pro")

def resolve_session_id(request: Request, response: Response) -> str:
    """Extracts or mints a secure per-browser session token via HttpOnly cookie."""
    token = request.cookies.get("downloader_session")
    if not token or not re.match(r"^[a-zA-Z0-9_-]{16,64}$", token):
        token = secrets.token_urlsafe(32)
        response.set_cookie(
            key="downloader_session",
            value=token,
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30  # 30 days
        )
    return token

class ScanRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    selected_ids: List[int]
    folder_name: str

class CredentialsRequest(BaseModel):
    api_id: int
    api_hash: str

class SendCodeRequest(BaseModel):
    phone: str

class SignInRequest(BaseModel):
    code: str
    password: Optional[str] = ""

@app.get("/")
async def serve_index():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return FileResponse(template_path)

@app.get("/speedtest")
async def serve_speedtest():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "speedtest.html")
    return FileResponse(template_path)

# ==============================================================================
# TELEGRAM AUTHENTICATION & CREDENTIALS APIS
# ==============================================================================
@app.get("/api/telegram-auth/status")
async def handle_auth_status(sid: str = Depends(resolve_session_id)):
    return await get_telegram_auth_status(sid)

@app.post("/api/telegram-auth/save-credentials")
async def handle_save_credentials(req: CredentialsRequest, sid: str = Depends(resolve_session_id)):
    if req.api_id <= 0 or not req.api_hash.strip():
        return JSONResponse({"status": "error", "message": "Invalid API ID or API Hash."}, status_code=400)
    try:
        await setup_api_credentials(sid, req.api_id, req.api_hash)
        return {"status": "ok", "message": "API credentials saved to your private session."}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.post("/api/telegram-auth/send-code")
async def handle_send_code(req: SendCodeRequest, sid: str = Depends(resolve_session_id)):
    if not req.phone.strip():
        return JSONResponse({"status": "error", "message": "Valid phone number required."}, status_code=400)
    try:
        return await send_telegram_login_code(sid, req.phone.strip())
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.post("/api/telegram-auth/sign-in")
async def handle_sign_in(req: SignInRequest, sid: str = Depends(resolve_session_id)):
    if not req.code.strip():
        return JSONResponse({"status": "error", "message": "Verification code required."}, status_code=400)
    try:
        return await complete_telegram_sign_in(sid, req.code.strip(), req.password or "")
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.post("/api/telegram-auth/logout")
async def handle_logout(response: Response, sid: str = Depends(resolve_session_id)):
    try:
        await logout_telegram_session(sid)
        response.delete_cookie("downloader_session")
        return {"status": "ok", "message": "Logged out successfully. All session tokens cleared."}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

# ==============================================================================
# FOLDER PICKER & SPEED TEST APIS
# ==============================================================================
def choose_folder_dialog(default_dir: str = "") -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
        
        root = tk.Tk()
        root.withdraw()
        root.lift()
        root.focus_force()
        root.attributes('-topmost', True)
        
        initial = default_dir if (default_dir and os.path.exists(default_dir) and os.path.isdir(default_dir)) else os.getcwd()
        selected = filedialog.askdirectory(initialdir=initial, title="Select Destination Folder")
        root.destroy()
        return selected or ""
    except Exception as e:
        print(f"[FolderPicker Error] {e}")
        return ""

@app.post("/api/browse-folder")
async def handle_browse_folder(sid: str = Depends(resolve_session_id)):
    ctx = get_session_ctx(sid)
    current = ctx.state.download_folder or ctx.state.default_folder or os.getcwd()
    selected_dir = await asyncio.to_thread(choose_folder_dialog, current)
    if selected_dir:
        normalized = os.path.normpath(selected_dir)
        ctx.state.download_folder = normalized
        ctx.state.save_to_disk()
        return {"status": "ok", "path": normalized}
    return {"status": "cancelled", "path": ""}

def perform_bandwidth_test() -> dict:
    ping_ms = 0.0
    try:
        t0 = time.time()
        s = socket.create_connection(("149.154.167.50", 443), timeout=3.0)
        s.close()
        ping_ms = round((time.time() - t0) * 1000, 1)
    except Exception:
        ping_ms = 145.0

    test_url = "https://speed.cloudflare.com/__down?bytes=20971520"
    downloaded_bytes = 0
    t_start = time.time()

    try:
        req = urllib.request.Request(test_url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req, timeout=12) as response:
            while chunk := response.read(256 * 1024):
                downloaded_bytes += len(chunk)
                if time.time() - t_start >= 6.0:
                    break
    except Exception:
        pass

    duration = max(0.1, time.time() - t_start)
    speed_mbps = round(((downloaded_bytes * 8) / duration) / 1_000_000, 2)
    speed_mb_s = round((downloaded_bytes / duration) / (1024 * 1024), 2)

    return {
        "status": "ok",
        "speed_mbps": speed_mbps,
        "speed_mb_s": speed_mb_s,
        "ping_ms": ping_ms,
        "transferred_mb": round(downloaded_bytes / (1024 * 1024), 2),
        "duration_sec": round(duration, 2)
    }

@app.get("/api/speedtest/run")
async def handle_speedtest_run():
    return await asyncio.to_thread(perform_bandwidth_test)

# ==============================================================================
# DOWNLOAD QUEUE APIS
# ==============================================================================
@app.get("/api/initial-state")
async def get_initial_state(sid: str = Depends(resolve_session_id)):
    ctx = get_session_ctx(sid)
    st = ctx.state
    return {
        "target_url": st.target_url,
        "default_folder": st.default_folder,
        "download_folder": st.download_folder,
        "scanned_items": st.scanned_items,
        "completed_ids": st.completed_ids,
        "active_id": st.active_id,
        "streams": st.streams,
        "is_downloading": st.is_downloading,
        "can_resume": st.can_resume,
        "is_paused": st.is_paused,
        "current_index": st.current_index,
        "total_files": st.total_files,
        "log": st.log
    }

@app.post("/api/scan")
async def handle_scan(req: ScanRequest, sid: str = Depends(resolve_session_id)):
    try:
        items, default_folder = await scan_channel_or_topic(sid, req.url)
        return {"status": "ok", "items": items, "default_folder": default_folder}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.post("/api/start-download")
async def handle_start_download(req: DownloadRequest, bg_tasks: BackgroundTasks, sid: str = Depends(resolve_session_id)):
    ctx = get_session_ctx(sid)
    if ctx.state.is_downloading:
        return JSONResponse({"status": "error", "message": "A download is already in progress!"}, status_code=400)
    if not req.selected_ids:
        return JSONResponse({"status": "error", "message": "No files selected."}, status_code=400)

    ctx.state.selected_ids = req.selected_ids
    ctx.state.download_folder = req.folder_name
    bg_tasks.add_task(run_batch_download, sid, req.selected_ids, req.folder_name)
    return {"status": "started"}

@app.post("/api/resume")
async def handle_resume(bg_tasks: BackgroundTasks, sid: str = Depends(resolve_session_id)):
    ctx = get_session_ctx(sid)
    st = ctx.state

    if st.is_downloading:
        return JSONResponse({"status": "error", "message": "Download is already running!"}, status_code=400)

    if not st.selected_ids and st.scanned_items:
        st.selected_ids = [item['id'] for item in st.scanned_items]
    
    folder = st.download_folder or st.default_folder or "Downloads"
    st.download_folder = folder

    if not st.selected_ids:
        return JSONResponse({"status": "error", "message": "No download found to resume. Scan or select lectures first."}, status_code=400)

    st.is_paused = False
    st.can_resume = False
    bg_tasks.add_task(run_batch_download, sid, st.selected_ids, folder)
    return {"status": "resumed"}

@app.post("/api/cancel")
async def handle_cancel(sid: str = Depends(resolve_session_id)):
    ctx = get_session_ctx(sid)
    if ctx.state.is_downloading:
        ctx.state.cancel_requested = True
        ctx.state.is_paused = True
        ctx.state.can_resume = True
        ctx.state.log = "Pausing download... Click 'Resume Download' to continue."
        ctx.state.save_to_disk()
        return {"status": "cancelling"}
    return {"status": "idle"}

@app.get("/api/status")
async def handle_status(sid: str = Depends(resolve_session_id)):
    ctx = get_session_ctx(sid)
    st = ctx.state
    return {
        "is_downloading": st.is_downloading,
        "can_resume": st.can_resume,
        "is_paused": st.is_paused,
        "completed_ids": st.completed_ids,
        "active_id": st.active_id,
        "streams": st.streams,
        "current_file": st.current_file,
        "current_index": st.current_index,
        "total_files": st.total_files,
        "percent": st.percent,
        "downloaded_mb": st.downloaded_mb,
        "total_mb": st.total_mb,
        "speed_mbps": st.speed_mbps,
        "eta": st.eta_str,
        "log": st.log
    }

if __name__ == "__main__":
    import uvicorn
    print("\n" + "=" * 65)
    print(" [FastAPI Server Started - Multi-User Isolated Sessions]")
    print(" Local Machine Access: http://127.0.0.1:8000")
    print(" LAN / Wi-Fi Access:   http://192.168.1.2:8000")
    print(" Speed Test Page:      http://192.168.1.2:8000/speedtest")
    print("=" * 65 + "\n")
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)