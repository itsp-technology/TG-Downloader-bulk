import re
from typing import Union, Optional

def sanitize_filename(name: str) -> str:
    """Strips illegal Windows filesystem characters from names."""
    clean = re.sub(r'[\\/*?:"<>|]', "_", name)
    return clean.strip().rstrip('.')

def format_seconds(seconds: float) -> str:
    if seconds <= 0 or seconds > 360000:
        return "--:--"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h > 0 else f"{m:02d}:{s:02d}"

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