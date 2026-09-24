import os
import logging
from typing import List
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse

from state import state
from downloader import client, scan_channel_or_topic, run_batch_download

# ==============================================================================
# LOG FILTER: Silences routine polling heartbeat from terminal stdout
# ==============================================================================
class NoiseFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "/api/status" in msg or "/api/initial-state" in msg or "com.chrome.devtools" in msg:
            return False
        return True

logging.getLogger("uvicorn.access").addFilter(NoiseFilter())

app = FastAPI(title="Lecture Batch Downloader Pro")

class ScanRequest(BaseModel):
    url: str

class DownloadRequest(BaseModel):
    selected_ids: List[int]
    folder_name: str

@app.on_event("startup")
async def startup_event():
    await client.connect()
    if await client.is_user_authorized():
        print("[Telegram] Ready & Authenticated.")
    else:
        print("[Telegram] Warning: Not authorized. Please run initial login script.")

@app.get("/")
async def serve_index():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return FileResponse(template_path)

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
async def handle_scan(req: ScanRequest):
    try:
        items, default_folder = await scan_channel_or_topic(req.url)
        return {"status": "ok", "items": items, "default_folder": default_folder}
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)

@app.post("/api/start-download")
async def handle_start_download(req: DownloadRequest, bg_tasks: BackgroundTasks):
    if state.is_downloading:
        return JSONResponse({"status": "error", "message": "A download is already in progress!"}, status_code=400)
    if not req.selected_ids:
        return JSONResponse({"status": "error", "message": "No files selected."}, status_code=400)

    state.selected_ids = req.selected_ids
    state.download_folder = req.folder_name
    bg_tasks.add_task(run_batch_download, req.selected_ids, req.folder_name)
    return {"status": "started"}

@app.post("/api/resume")
async def handle_resume(bg_tasks: BackgroundTasks):
    if state.is_downloading:
        return JSONResponse({"status": "error", "message": "Download is already running!"}, status_code=400)

    if not state.selected_ids and state.scanned_items:
        state.selected_ids = [item['id'] for item in state.scanned_items]
    
    folder = state.download_folder or state.default_folder or "Downloads"
    state.download_folder = folder

    if not state.selected_ids:
        return JSONResponse({"status": "error", "message": "No download found to resume. Scan or select lectures first."}, status_code=400)

    state.is_paused = False
    state.can_resume = False
    bg_tasks.add_task(run_batch_download, state.selected_ids, folder)
    return {"status": "resumed"}

@app.post("/api/cancel")
async def handle_cancel():
    if state.is_downloading:
        state.cancel_requested = True
        state.is_paused = True
        state.can_resume = True
        state.log = "Pausing download... Click 'Resume Download' to continue."
        state.save_to_disk()
        return {"status": "cancelling"}
    return {"status": "idle"}

@app.get("/api/status")
async def handle_status():
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=False)