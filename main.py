import os
from typing import List
from pydantic import BaseModel
from fastapi import FastAPI, BackgroundTasks
from fastapi.responses import FileResponse, JSONResponse

from state import state
from downloader import client, scan_channel_or_topic, run_batch_download

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
        print("[Telegram] Warning: Not authorized. Run your initial login script once.")

@app.get("/")
async def serve_index():
    template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
    return FileResponse(template_path)

@app.get("/api/initial-state")
async def get_initial_state():
    return {
        "target_url": state.target_url,
        "default_folder": state.default_folder,
        "scanned_items": state.scanned_items,
        "is_downloading": state.is_downloading
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

    bg_tasks.add_task(run_batch_download, req.selected_ids, req.folder_name)
    return {"status": "started"}

@app.post("/api/cancel")
async def handle_cancel():
    if state.is_downloading:
        state.cancel_requested = True
        state.log = "Aborting active download..."
        return {"status": "cancelling"}
    return {"status": "idle"}

@app.get("/api/status")
async def handle_status():
    return {
        "is_downloading": state.is_downloading,
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