import sys
import ctypes
from typing import Optional, List, Dict, Any

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
    """Raised to immediately stop download stream on user request."""
    pass

class AppState:
    def __init__(self):
        self.is_downloading: bool = False
        self.cancel_requested: bool = False
        self.current_file: str = ""
        self.current_index: int = 0
        self.total_files: int = 0
        self.percent: float = 0.0
        self.downloaded_mb: int = 0
        self.total_mb: int = 0
        self.speed_mbps: float = 0.0
        self.eta_str: str = "--:--"
        self.log: str = "Ready."
        
        # State preservation across browser refresh
        self.target_url: str = ""
        self.default_folder: str = ""
        self.scanned_items: List[Dict[str, Any]] = []
        self.active_peer: Any = None
        self.active_topic: Optional[int] = None
        
        self.last_bytes: int = 0
        self.last_time: float = 0.0

    def __setitem__(self, key: str, value: Any):
        setattr(self, key, value)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)

state = AppState()