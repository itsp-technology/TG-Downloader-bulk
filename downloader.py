import os
import re
import time
import asyncio
from typing import Any, List, Dict, Optional, Tuple, AsyncGenerator
from telethon import TelegramClient
from telethon.errors import SessionPasswordNeededError
from telethon.tl.types import DocumentAttributeVideo

from config import get_session_dir, get_session_credentials, save_session_credentials, clear_session_storage, BASE_SESSION_DIR
from state import AppState, prevent_sleep, allow_sleep, DownloadCancelledException
from utils import sanitize_filename, format_seconds, parse_telegram_url

try:
    import cryptg
    print("[Speed Boost] cryptg detected: Hardware C-acceleration active.")
except ImportError:
    print("[Warning] cryptg not found: Falling back to pure-Python AES.")

CHUNK_SIZE = 512 * 1024
MAX_PARALLEL_STREAMS = 4

class SessionContext:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.session_dir = get_session_dir(session_id)
        self.state = AppState(self.session_dir)
        self.client: Any = None

    def resolve_active_dir(self) -> str:
        """Allows mobile devices connected over LAN to seamlessly share the active PC login."""
        local_session = os.path.join(self.session_dir, "telegram.session")
        local_cred = os.path.join(self.session_dir, "credentials.json")
        if os.path.exists(local_session) and os.path.exists(local_cred):
            return self.session_dir

        if os.path.exists(BASE_SESSION_DIR):
            for folder in sorted(os.listdir(BASE_SESSION_DIR)):
                cand = os.path.join(BASE_SESSION_DIR, folder)
                if os.path.exists(os.path.join(cand, "telegram.session")) and os.path.exists(os.path.join(cand, "credentials.json")):
                    return cand
        return self.session_dir

    async def get_client(self) -> Any:
        active_dir = self.resolve_active_dir()
        if self.client is None or not self.client.is_connected():
            cred_file = os.path.join(active_dir, "credentials.json")
            api_id, api_hash = 0, ""
            if os.path.exists(cred_file):
                import json
                try:
                    with open(cred_file, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        api_id = int(data.get("api_id", 0))
                        api_hash = str(data.get("api_hash", "")).strip()
                except Exception:
                    pass

            if not api_id or not api_hash:
                raise ValueError("Telegram credentials not configured. Click 'Telegram Account' at the top to sign in.")
            
            session_file = os.path.join(active_dir, "telegram.session")
            self.client = TelegramClient(session_file, api_id, api_hash)
            await self.client.connect()
        return self.client

_sessions_pool: Dict[str, SessionContext] = {}

def get_session_ctx(session_id: str) -> SessionContext:
    if session_id not in _sessions_pool:
        _sessions_pool[session_id] = SessionContext(session_id)
    return _sessions_pool[session_id]

async def get_telegram_auth_status(session_id: str) -> dict:
    ctx = get_session_ctx(session_id)
    active_dir = ctx.resolve_active_dir()
    cred_file = os.path.join(active_dir, "credentials.json")
    api_id = 0

    if os.path.exists(cred_file):
        import json
        try:
            with open(cred_file, "r", encoding="utf-8") as f:
                api_id = int(json.load(f).get("api_id", 0))
        except Exception:
            pass

    if not api_id:
        return {
            "configured": False,
            "authorized": False,
            "masked_api_id": "",
            "user_info": "Not configured"
        }

    masked_id = f"{str(api_id)[:3]}****" if len(str(api_id)) > 3 else "****"

    try:
        cl: Any = await ctx.get_client()
        is_auth = await cl.is_user_authorized()
        user_info = "Connected"
        if is_auth:
            me: Any = await cl.get_me()
            first_name = getattr(me, "first_name", "") or ""
            last_name = getattr(me, "last_name", "") or ""
            name = f"{first_name} {last_name}".strip()
            uname = getattr(me, "username", None)
            username = f"(@{uname})" if uname else ""
            phone = getattr(me, "phone", None)
            user_info = f"{name} {username}".strip() or str(phone or "Authorized")
        return {
            "configured": True,
            "authorized": is_auth,
            "masked_api_id": masked_id,
            "user_info": user_info
        }
    except Exception as e:
        return {
            "configured": True,
            "authorized": False,
            "masked_api_id": masked_id,
            "user_info": f"Unauthenticated: {str(e)}"
        }

async def setup_api_credentials(session_id: str, api_id: int, api_hash: str):
    ctx = get_session_ctx(session_id)
    cl: Any = ctx.client
    if cl is not None and hasattr(cl, "is_connected") and cl.is_connected():
        try:
            dis = cl.disconnect()
            if asyncio.iscoroutine(dis):
                await dis
        except Exception:
            pass
        ctx.client = None

    save_session_credentials(session_id, api_id, api_hash)
    await ctx.get_client()

async def send_telegram_login_code(session_id: str, phone: str) -> dict:
    ctx = get_session_ctx(session_id)
    cl: Any = await ctx.get_client()
    sent = await cl.send_code_request(phone.strip())
    ctx.state.auth_phone = phone.strip()
    ctx.state.phone_code_hash = sent.phone_code_hash
    return {"status": "ok", "message": f"Verification code sent to {phone}"}

async def complete_telegram_sign_in(session_id: str, code: str, password: str = "") -> dict:
    ctx = get_session_ctx(session_id)
    cl: Any = await ctx.get_client()
    if not ctx.state.auth_phone or not ctx.state.phone_code_hash:
        raise ValueError("Please request a login code first.")

    try:
        await cl.sign_in(
            phone=ctx.state.auth_phone,
            code=code.strip(),
            phone_code_hash=ctx.state.phone_code_hash
        )
    except SessionPasswordNeededError:
        if not password:
            return {"status": "2fa_required", "message": "Two-Factor Authentication (2FA) password required."}
        await cl.sign_in(password=password.strip())

    me: Any = await cl.get_me()
    first_name = getattr(me, "first_name", "") or ""
    last_name = getattr(me, "last_name", "") or ""
    name = f"{first_name} {last_name}".strip() or "Telegram User"
    return {"status": "ok", "user": name}

async def logout_telegram_session(session_id: str):
    ctx = get_session_ctx(session_id)
    active_dir = ctx.resolve_active_dir()
    cl: Any = ctx.client
    if cl is not None:
        try:
            if hasattr(cl, "is_connected") and cl.is_connected():
                if await cl.is_user_authorized():
                    await cl.log_out()
                else:
                    dis = cl.disconnect()
                    if asyncio.iscoroutine(dis):
                        await dis
        except Exception:
            pass
        ctx.client = None

    ctx.state.reset()
    clear_session_storage(os.path.basename(active_dir))
    clear_session_storage(session_id)
    if session_id in _sessions_pool:
        del _sessions_pool[session_id]

# ==============================================================================
# ZERO-DISK PASSTHROUGH STREAM GENERATOR (Direct to Downloads Folder)
# ==============================================================================
async def get_file_stream_generator(session_id: str, msg_id: int) -> Tuple[AsyncGenerator[bytes, None], str, int]:
    """Streams file directly from Telegram to browser. ZERO cloud storage used."""
    ctx = get_session_ctx(session_id)
    cl: Any = await ctx.get_client()

    if not await cl.is_user_authorized():
        raise PermissionError("Telegram is not logged in. Click 'Telegram Account' at top to sign in first.")

    # Auto-recover peer from active_peer, target_url, or saved state
    peer = ctx.state.active_peer
    if not peer:
        if ctx.state.target_url:
            peer, _ = parse_telegram_url(ctx.state.target_url)
            ctx.state.active_peer = peer
        else:
            ctx.state.load_from_disk()
            if ctx.state.target_url:
                peer, _ = parse_telegram_url(ctx.state.target_url)
                ctx.state.active_peer = peer

    # Check shared session state if mobile connected to host
    if not peer:
        active_dir = ctx.resolve_active_dir()
        state_file = os.path.join(active_dir, "downloads_state.json")
        if os.path.exists(state_file):
            import json
            try:
                with open(state_file, "r", encoding="utf-8") as f:
                    t_url = json.load(f).get("target_url", "")
                    if t_url:
                        peer, _ = parse_telegram_url(t_url)
                        ctx.state.active_peer = peer
            except Exception:
                pass

    if not peer:
        raise ValueError("No lecture topic found. Please paste your Telegram URL and click 'Scan Lectures' first.")

    raw_entity = await cl.get_entity(peer)
    entity = raw_entity[0] if isinstance(raw_entity, list) else raw_entity

    raw_msg = await cl.get_messages(entity, ids=msg_id)
    msg: Any = raw_msg[0] if isinstance(raw_msg, list) else raw_msg
    if not msg or not msg.file:
        raise ValueError("Lecture file not found or has no media attached.")

    file_entity = msg.document if msg.document else msg
    file_size = msg.file.size or 0

    is_pdf = bool(msg.document and msg.document.mime_type == 'application/pdf')
    ext = ".pdf" if is_pdf else ".mp4"

    raw_name = msg.file.name
    if not raw_name:
        caption = msg.text or ""
        title_candidate = caption.split('\n')[0].strip() if caption else f"Lecture_{msg_id}"
        clean_title = sanitize_filename(title_candidate)[:50]
        file_name = f"{clean_title}{ext}"
    else:
        file_name = sanitize_filename(raw_name)
        if not file_name.lower().endswith(('.mp4', '.pdf', '.mkv', '.avi')):
            file_name += ext

    async def stream_chunks():
        async for chunk in cl.iter_download(
            file_entity,
            chunk_size=CHUNK_SIZE,
            request_size=CHUNK_SIZE
        ):
            yield chunk

    return stream_chunks(), file_name, file_size

# ==============================================================================
# STANDARD PARALLEL BATCH ENGINE (For local disk storage)
# ==============================================================================
def update_aggregate_progress(state: AppState, total_size: int, stream_progress: List[int], stream_totals: List[int]):
    now = time.time()
    dt = now - state.last_time
    total_downloaded = sum(stream_progress)

    if dt >= 0.8 or total_downloaded == total_size:
        bytes_delta = total_downloaded - state.last_bytes
        speed = (bytes_delta / dt) / (1024 * 1024) if dt > 0 else 0.0
        state.speed_mbps = round(speed, 2)

        remaining_bytes = max(0, total_size - total_downloaded)
        state.eta_str = format_seconds(remaining_bytes / (speed * 1024 * 1024)) if speed > 0 else "--:--"

        state.last_time = now
        state.last_bytes = total_downloaded

    if total_size > 0:
        state.percent = round((total_downloaded / total_size) * 100, 1)
        state.downloaded_mb = total_downloaded // (1024 * 1024)
        state.total_mb = total_size // (1024 * 1024)

    stream_data = []
    for idx, (curr, tot) in enumerate(zip(stream_progress, stream_totals), start=1):
        pct = round((curr / tot) * 100, 1) if tot > 0 else 0.0
        stream_data.append({
            "id": idx,
            "percent": pct,
            "downloaded_mb": round(curr / (1024 * 1024), 1),
            "total_mb": round(tot / (1024 * 1024), 1)
        })
    state.streams = stream_data

async def stream_worker(
    cl: Any,
    state: AppState,
    worker_idx: int,
    file_entity: Any,
    part_path: str,
    start_byte: int,
    stream_total_bytes: int,
    stream_progress: List[int],
    stream_totals: List[int],
    total_file_size: int
):
    already_downloaded = 0
    if os.path.exists(part_path):
        cur_size = os.path.getsize(part_path)
        if cur_size >= stream_total_bytes:
            stream_progress[worker_idx] = stream_total_bytes
            update_aggregate_progress(state, total_file_size, stream_progress, stream_totals)
            return

        already_downloaded = (cur_size // CHUNK_SIZE) * CHUNK_SIZE
        with open(part_path, "r+b") as fp:
            fp.truncate(already_downloaded)

    stream_progress[worker_idx] = already_downloaded
    update_aggregate_progress(state, total_file_size, stream_progress, stream_totals)

    fetch_offset = start_byte + already_downloaded
    mode = "ab" if already_downloaded > 0 else "wb"

    with open(part_path, mode) as fp:
        async for chunk in cl.iter_download(
            file_entity,
            offset=fetch_offset,
            chunk_size=CHUNK_SIZE,
            request_size=CHUNK_SIZE
        ):
            if state.cancel_requested:
                raise DownloadCancelledException("Download paused by user.")

            needed = min(len(chunk), stream_total_bytes - stream_progress[worker_idx])
            if needed > 0:
                fp.write(chunk[:needed])
                stream_progress[worker_idx] += needed
                update_aggregate_progress(state, total_file_size, stream_progress, stream_totals)

            if stream_progress[worker_idx] >= stream_total_bytes:
                break

async def download_file_parallel(cl: Any, state: AppState, msg: Any, final_path: str):
    total_size = msg.file.size if msg.file else 0
    file_entity = msg.document if msg.document else msg

    if os.path.exists(final_path):
        if total_size == 0 or os.path.getsize(final_path) == total_size:
            return True

    num_streams = MAX_PARALLEL_STREAMS if total_size >= 8 * 1024 * 1024 else 1

    total_chunks = (total_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    chunks_per_stream = (total_chunks + num_streams - 1) // num_streams

    start_bytes = []
    stream_totals = []
    part_paths = []

    for i in range(num_streams):
        s_chunk = i * chunks_per_stream
        e_chunk = min((i + 1) * chunks_per_stream, total_chunks)
        s_byte = s_chunk * CHUNK_SIZE
        e_byte = min(e_chunk * CHUNK_SIZE, total_size)
        size_bytes = max(0, e_byte - s_byte)

        start_bytes.append(s_byte)
        stream_totals.append(size_bytes)
        part_paths.append(f"{final_path}.part{i}")

    stream_progress = [0] * num_streams
    state.last_time = time.time()
    state.last_bytes = 0

    update_aggregate_progress(state, total_size, stream_progress, stream_totals)

    worker_tasks = [
        asyncio.create_task(
            stream_worker(
                cl, state, i, file_entity, part_paths[i], start_bytes[i],
                stream_totals[i], stream_progress, stream_totals, total_size
            )
        )
        for i in range(num_streams)
        if stream_totals[i] > 0
    ]

    try:
        await asyncio.gather(*worker_tasks)
    except DownloadCancelledException:
        for t in worker_tasks:
            if not t.done():
                t.cancel()
        raise

    state.log = "Merging parallel streams..."
    tmp_final = final_path + ".tmp"
    with open(tmp_final, "wb") as outfile:
        for p in part_paths:
            if os.path.exists(p):
                with open(p, "rb") as infile:
                    while chunk := infile.read(1024 * 1024):
                        outfile.write(chunk)
                try:
                    os.remove(p)
                except Exception:
                    pass

    if os.path.exists(final_path):
        os.remove(final_path)
    os.rename(tmp_final, final_path)
    state.streams = []
    return True

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

async def scan_channel_or_topic(session_id: str, url: str):
    ctx = get_session_ctx(session_id)
    cl: Any = await ctx.get_client()

    if not await cl.is_user_authorized():
        raise PermissionError("Telegram session is not authorized. Please click 'Telegram Account' to log in.")

    peer, topic_id = parse_telegram_url(url)
    ctx.state.active_peer = peer
    ctx.state.active_topic = topic_id
    ctx.state.target_url = url.strip()

    await cl.get_dialogs()
    raw_entity = await cl.get_entity(peer)
    entity = raw_entity[0] if isinstance(raw_entity, list) else raw_entity

    items: List[Dict[str, Any]] = []
    messages_iter = cl.iter_messages(entity, reply_to=topic_id) if topic_id is not None else cl.iter_messages(entity)

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
    ctx.state.scanned_items = items
    ctx.state.default_folder = f"Lectures_Topic_{topic_id}" if topic_id else "Lectures_Channel"
    ctx.state.save_to_disk()
    return items, ctx.state.default_folder

async def run_batch_download(session_id: str, selected_ids: List[int], download_dir: str):
    ctx = get_session_ctx(session_id)
    cl: Any = await ctx.get_client()
    state = ctx.state

    state.is_downloading = True
    state.cancel_requested = False
    state.is_paused = False
    state.can_resume = False
    state.selected_ids = selected_ids
    state.download_folder = download_dir
    state.save_to_disk()
    prevent_sleep()

    try:
        os.makedirs(download_dir, exist_ok=True)
        raw_entity = await cl.get_entity(state.active_peer)
        entity = raw_entity[0] if isinstance(raw_entity, list) else raw_entity

        id_to_meta = {item['id']: item for item in state.scanned_items if item['id'] in selected_ids}
        total = len(selected_ids)
        state.total_files = total

        downloaded_records: List[Dict[str, str]] = []
        pending_items = []

        for index, msg_id in enumerate(selected_ids, start=1):
            meta = id_to_meta.get(msg_id, {})
            filename = f"{index:02d}_{meta.get('clean_filename', f'file_{msg_id}.mp4')}"
            final_path = os.path.join(download_dir, filename)

            if os.path.exists(final_path):
                downloaded_records.append({"filename": filename, "title": meta.get('title', filename)})
                if msg_id not in state.completed_ids:
                    state.completed_ids.append(msg_id)
            else:
                pending_items.append((index, msg_id, filename, final_path, meta))

        state.save_to_disk()

        if not pending_items:
            state.active_id = None
            state.current_index = total
            state.percent = 100.0
            state.streams = []
            state.log = f"All {total} lectures already downloaded!"
            generate_offline_portal(download_dir, downloaded_records)
            state.can_resume = False
            state.is_paused = False
            state.save_to_disk()
            return

        first_index, first_id, first_filename, _, _ = pending_items[0]
        state.current_index = first_index
        state.current_file = first_filename
        state.active_id = first_id
        state.save_to_disk()

        for index, msg_id, filename, final_path, meta in pending_items:
            if state.cancel_requested:
                state.is_paused = True
                state.can_resume = True
                state.active_id = msg_id
                state.current_index = index
                state.current_file = filename
                state.log = f"Paused at [{index}/{total}]: {filename}. Click 'Resume Download' to continue."
                state.save_to_disk()
                break

            state.active_id = msg_id
            state.current_index = index
            state.current_file = filename
            state.speed_mbps = 0.0
            state.eta_str = "--:--"
            state.log = f"[{index}/{total}] Multi-Stream Downloading: {filename}"
            state.save_to_disk()

            raw_msg = await cl.get_messages(entity, ids=msg_id)
            msg: Any = raw_msg[0] if isinstance(raw_msg, list) else raw_msg
            if not msg:
                continue

            try:
                await download_file_parallel(cl, state, msg, final_path)
            except (DownloadCancelledException, asyncio.CancelledError):
                state.is_paused = True
                state.can_resume = True
                state.active_id = msg_id
                state.current_index = index
                state.current_file = filename
                state.log = f"Paused at [{index}/{total}]. Click 'Resume Download' to continue."
                state.save_to_disk()
                break
            except Exception as e:
                if state.cancel_requested:
                    state.is_paused = True
                    state.can_resume = True
                    state.active_id = msg_id
                    state.current_index = index
                    state.current_file = filename
                    state.log = f"Paused at [{index}/{total}]."
                    state.save_to_disk()
                    break
                else:
                    state.log = f"Skipping error on {filename}: {str(e)}"
                    state.can_resume = True
                    state.is_paused = True
                    continue

            if state.cancel_requested:
                state.is_paused = True
                state.can_resume = True
                state.active_id = msg_id
                state.current_index = index
                state.current_file = filename
                state.log = f"Paused at [{index}/{total}]. Click 'Resume Download' to continue."
                state.save_to_disk()
                break

            if os.path.exists(final_path):
                downloaded_records.append({"filename": filename, "title": meta.get('title', filename)})
                if msg_id not in state.completed_ids:
                    state.completed_ids.append(msg_id)
                state.save_to_disk()

        if state.cancel_requested or state.is_paused:
            state.can_resume = True
            state.is_paused = True
        else:
            state.can_resume = False
            state.is_paused = False
            state.active_id = None
            state.streams = []
            state.current_index = total
            state.percent = 100.0
            if downloaded_records:
                generate_offline_portal(download_dir, downloaded_records)
                state.log = f"All {total} lectures downloaded! Offline hub created in '{download_dir}'."

    except Exception as e:
        state.can_resume = True
        state.is_paused = True
        state.log = f"Halted: {str(e)}. Click Resume Download to retry."
    finally:
        state.is_downloading = False
        state.cancel_requested = False
        state.speed_mbps = 0.0
        state.eta_str = "--:--"
        state.save_to_disk()
        allow_sleep()