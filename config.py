import os
import json
from typing import Tuple

BASE_SESSION_DIR = "user_sessions"
os.makedirs(BASE_SESSION_DIR, exist_ok=True)

def get_session_dir(session_id: str) -> str:
    """Returns and ensures the private directory for a given session."""
    clean_id = "".join(c for c in session_id if c.isalnum() or c in ("-", "_"))
    path = os.path.join(BASE_SESSION_DIR, clean_id)
    os.makedirs(path, exist_ok=True)
    return path

def get_session_credentials(session_id: str) -> Tuple[int, str]:
    """Loads private API_ID and API_HASH for a specific session."""
    s_dir = get_session_dir(session_id)
    cred_file = os.path.join(s_dir, "credentials.json")

    if os.path.exists(cred_file):
        try:
            with open(cred_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                api_id = int(data.get("api_id", 0))
                api_hash = str(data.get("api_hash", "")).strip()
                return api_id, api_hash
        except Exception:
            pass

    return 0, ""

def save_session_credentials(session_id: str, api_id: int, api_hash: str):
    """Saves API_ID and API_HASH securely inside the user's private session directory."""
    s_dir = get_session_dir(session_id)
    cred_file = os.path.join(s_dir, "credentials.json")
    
    with open(cred_file, "w", encoding="utf-8") as f:
        json.dump({"api_id": api_id, "api_hash": api_hash.strip()}, f)

def clear_session_storage(session_id: str):
    """Completely deletes credentials and session files on logout."""
    s_dir = get_session_dir(session_id)
    if os.path.exists(s_dir):
        for fname in os.listdir(s_dir):
            fpath = os.path.join(s_dir, fname)
            try:
                if os.path.isfile(fpath):
                    os.remove(fpath)
            except Exception:
                pass