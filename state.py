import os
import sys
import json
import ctypes
from typing import Optional, List, Dict, Any

STATE_FILE = "downloads_state.json"
ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

def prevent_sleep():
    """Tells Windows not to sleep while a download is active."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
        except Exception:
            pass

def allow_sleep():
    """Restores default system sleep timers."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        except Exception:
            pass

class DownloadCancelledException(Exception):
    """Raised to immediately break download stream on user request."""
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
        
        # Per-file live status trackers
        self.completed_ids: List[int] = []
        self.active_id: Optional[int] = None

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
                "completed_ids": self.completed_ids,
                "active_id": self.active_id,
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
                    self.completed_ids = data.get("completed_ids", [])
                    self.active_id = data.get("active_id", None)
                    if self.can_resume:
                        self.log = f"Paused at [{self.current_index}/{self.total_files}]. Click 'Resume Download' to continue."
            except Exception:
                pass

state = AppState()