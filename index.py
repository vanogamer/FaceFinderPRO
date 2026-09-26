import os
import cv2
import shutil
import time
import queue
import threading
import sqlite3
import psutil
import json
import hashlib
import atexit
import signal
import tempfile
import logging
import csv
import uuid
import re
import random
import base64
import subprocess
import ctypes

from dataclasses import dataclass, field
from typing import List, Set, Dict, Optional, Tuple, Union, Any, Callable
from pathlib import Path
from contextlib import contextmanager

import numpy as np
from tkinter import *  # pyright: ignore[reportWildcardImportFromLibrary]
from tkinter import filedialog, messagebox, simpledialog
from tkinter.ttk import Progressbar, Style
from tqdm import tqdm
from PIL import Image
import imagehash
from statistics import mean
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_COMPLETION_MUSIC = BASE_DIR / "assets" / "completion.mp3"
CUSTOM_COMPLETION_MUSIC = BASE_DIR / "music" / "music.mp3"

# ----------- LOGGING SETUP ------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / 'face_scanner.log', encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)


def _completion_music_path() -> Optional[Path]:
    """Prefer the user's music/music.mp3, then use the bundled chime."""
    if CUSTOM_COMPLETION_MUSIC.is_file():
        return CUSTOM_COMPLETION_MUSIC
    if DEFAULT_COMPLETION_MUSIC.is_file():
        return DEFAULT_COMPLETION_MUSIC
    return None


def _play_completion_music() -> None:
    """Play an MP3 asynchronously through the native Windows MCI API."""
    music_path = _completion_music_path()
    if music_path is None or os.name != "nt":
        return

    def _play() -> None:
        alias = f"face_scanner_done_{uuid.uuid4().hex}"
        winmm = ctypes.windll.winmm
        quoted_path = str(music_path.resolve()).replace('"', '""')
        try:
            result = winmm.mciSendStringW(
                f'open "{quoted_path}" type mpegvideo alias {alias}', None, 0, None
            )
            if result != 0:
                raise RuntimeError(f"MCI open error {result}")
            winmm.mciSendStringW(f"play {alias} wait", None, 0, None)
        except Exception as exc:
            logger.warning("Completion music failed: %s", exc)
        finally:
            try:
                winmm.mciSendStringW(f"close {alias}", None, 0, None)
            except Exception:
                pass

    threading.Thread(target=_play, daemon=True, name="CompletionMusic").start()


def _show_windows_completion_notification(processed: int, total: int) -> None:
    """Show a native Windows toast without blocking the Tk main thread."""
    if os.name != "nt":
        return
    title = "Face Scanner — სკანირება დასრულდა"
    message = f"დამუშავებულია {processed}/{total} ფოტო."
    script = f"""
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text>{title}</text><text>{message}</text></binding></visual></toast>')
$toast = New-Object Windows.UI.Notifications.ToastNotification $xml
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Face Scanner').Show($toast)
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")

    def _notify() -> None:
        try:
            creation_flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creation_flags, timeout=15,
            )
        except Exception as exc:
            logger.warning("Windows completion notification failed: %s", exc)

    threading.Thread(target=_notify, daemon=True, name="CompletionNotification").start()


def notify_scan_completed(processed: int, total: int) -> None:
    """Play the selected completion music and show the Windows notification."""
    _play_completion_music()
    _show_windows_completion_notification(processed, total)

# ----------- CONFIGURATION ------------
@dataclass
class Config:
    """Application configuration"""
    extensions: Tuple[str, ...] = (".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".dng", ".cr2", ".nef", ".arw", ".rw2")
    det_size: Tuple[int, int] = (640, 640)
    worker_count: int = 4
    threshold_default: int = 46
    threshold_min: int = 20
    threshold_max: int = 60
    worker_min: int = 1
    worker_max: int = 20
    cpu_threshold: int = 85
    memory_threshold: int = 85
    time_records_max: int = 50
    db_path: str = "results.db"
    scan_db_path: str = "scan_results/scan.db"
    router_db_path: str = "router_results/router.db"
    state_dir_name: str = "scan_state"
    scan_state_dir_name: str = "scan_state/scan"
    router_state_dir_name: str = "scan_state/router"
    db_commit_every: int = 25
    db_commit_interval: float = 1.0
    state_flush_every: int = 20
    state_flush_interval: float = 1.0
    progress_update_interval: float = 0.12
    system_check_interval: float = 0.50
    review_margin: float = 0.04
    ambiguity_margin: float = 0.035
    duplicate_phash_distance: int = 4
    duplicate_aspect_tolerance: float = 0.02
    live_watch_interval: float = 2.0


@dataclass
class ScanStats:
    """Statistics for current scan"""
    resumed: int = 0
    duplicates: int = 0
    errors: int = 0


@dataclass
class ScannerState:
    """Global scanner state container"""
    ref_embs: List[np.ndarray] = field(default_factory=list)
    ref_embs_matrix: Optional[np.ndarray] = None
    ref_files: List[str] = field(default_factory=list)
    ref_db_value: str = ""
    matched: Set[str] = field(default_factory=set)
    processed: Set[str] = field(default_factory=set)
    processed_content_hashes: Set[str] = field(default_factory=set)  # phash without size
    nonmatched: Set[str] = field(default_factory=set)
    src_folder: str = ""
    out_folder: str = ""
    time_records: List[float] = field(default_factory=list)
    scan_running: bool = False
    scan_threads: List[threading.Thread] = field(default_factory=list)
    stop_removed_pending: int = 0
    close_after_stop: bool = False
    scan_state: Dict = field(default_factory=dict)
    scan_state_path: Optional[Path] = None
    current_profile: Optional[Dict] = None
    stats: ScanStats = field(default_factory=ScanStats)
    progress_completed: int = 0
    ui_update_scheduled: bool = False
    last_ui_update_at: float = 0.0
    last_system_check_at: float = 0.0
    should_pause_for_resources: bool = False
    scan_started_at: float = 0.0
    exact_hashes: Set[str] = field(default_factory=set)
    phash_items: List[Tuple[str, str, float, int, int]] = field(default_factory=list)
    active_scan_id: str = ""
    current_ref_signature: str = ""
    current_identity_signature: str = ""
    reference_content_signature: str = ""
    current_threshold: float = 0.0
    review_count: int = 0
    live_watch_thread: Optional[threading.Thread] = None
    precomputed_quick_hashes: Dict[str, Tuple[int, int, str]] = field(default_factory=dict)
    main_identities: List[Dict[str, Any]] = field(default_factory=list)
    reference_model_signature: str = ""
    current_matching_signature: str = ""
    current_recognition_signature: str = ""
    duplicate_mode: str = "მხოლოდ ანგარიშში"
    performance_profile: str = "ავტომატური"
    resource_cpu_load: float = 0.0
    resource_memory_load: float = 0.0
    live_session_id: str = ""
    resume_tracker: Any = None
    checked_nonmatch_index: Any = None
    duplicate_index: Any = None

    # Threading primitives
    set_lock: threading.Lock = field(default_factory=threading.Lock)
    state_lock: threading.RLock = field(default_factory=threading.RLock)
    db_lock: threading.Lock = field(default_factory=threading.Lock)
    progress_lock: threading.Lock = field(default_factory=threading.Lock)
    system_check_lock: threading.Lock = field(default_factory=threading.Lock)
    file_q: queue.Queue = field(default_factory=queue.Queue)
    stop_requested: threading.Event = field(default_factory=threading.Event)
    pause_requested: threading.Event = field(default_factory=threading.Event)
    live_watch_stop: threading.Event = field(default_factory=threading.Event)

    def reset(self) -> None:
        """Reset runtime state for new scan"""
        previous_tracker = getattr(self, "resume_tracker", None)
        try:
            if previous_tracker is not None and hasattr(previous_tracker, "close"):
                previous_tracker.close()
        except Exception:
            pass
        self.resume_tracker = None
        self.matched = set()
        self.nonmatched = set()
        self.processed = set()
        self.processed_content_hashes = set()
        self.time_records = []
        self.file_q = queue.Queue()
        self.stats = ScanStats()
        self.current_profile = None
        self.scan_threads = []
        self.stop_removed_pending = 0
        self.close_after_stop = False
        self.progress_completed = 0
        self.ui_update_scheduled = False
        self.last_ui_update_at = 0.0
        self.last_system_check_at = 0.0
        self.should_pause_for_resources = False
        self.scan_started_at = 0.0
        self.exact_hashes = set()
        self.phash_items = []
        self.review_count = 0
        self.precomputed_quick_hashes = {}
        self.pause_requested.clear()
        self.stop_requested.clear()


# Initialize config and state
config = Config()
state = ScannerState()
STATE_DIR = Path(__file__).resolve().parent / config.state_dir_name
STATE_DIR.mkdir(exist_ok=True)
SCAN_STATE_DIR = Path(__file__).resolve().parent / config.scan_state_dir_name
SCAN_STATE_DIR.mkdir(parents=True, exist_ok=True)
ROUTER_STATE_DIR = Path(__file__).resolve().parent / config.router_state_dir_name
ROUTER_STATE_DIR.mkdir(parents=True, exist_ok=True)
# Output folders
SCAN_OUT_DIR = Path(__file__).resolve().parent / "scan_results"
SCAN_OUT_DIR.mkdir(exist_ok=True)
ROUTER_OUT_DIR = Path(__file__).resolve().parent / "router_results"
ROUTER_OUT_DIR.mkdir(exist_ok=True)

# ----------- FACE ENGINE ------------
import insightface

providers = ["CPUExecutionProvider"]
try:
    import onnxruntime as ort
    if "DmlExecutionProvider" in ort.get_available_providers():
        providers = ["DmlExecutionProvider", "CPUExecutionProvider"]
        logger.info("AMD DirectML Enabled with CPU fallback!")
except ImportError as e:
    logger.warning(f"ONNX Runtime import failed: {e}")
except Exception as e:
    logger.warning(f"Failed to check ONNX providers: {e}")

logger.info(f"Using providers: {providers}")
app = insightface.app.FaceAnalysis(name="buffalo_l", providers=providers)
app.prepare(ctx_id=-1, det_size=config.det_size)


# ----------- DATABASE MANAGER ------------
class DatabaseManager:
    """Context manager for database operations"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._connection: Optional[sqlite3.Connection] = None
        self._cursor: Optional[sqlite3.Cursor] = None

    def initialize(self) -> None:
        """Initialize database and create tables"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA cache_size=-32000")  # ~32MB page cache
            cursor.execute("PRAGMA mmap_size=268435456")  # 256MB, no-op if unsupported
            cursor.execute("""CREATE TABLE IF NOT EXISTS matches(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ref TEXT,
                file TEXT,
                time TEXT
            )""")
            conn.commit()
            logger.info("Database initialized successfully")

    @contextmanager
    def get_connection(self):
        """Get database connection with automatic cleanup"""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def get_persistent_connection(self) -> Tuple[sqlite3.Connection, sqlite3.Cursor]:
        """Get persistent connection for long-running operations.

        NOTE: synchronous/temp_store/cache_size/mmap_size/busy_timeout are
        per-connection pragmas in SQLite (unlike journal_mode=WAL, which is
        stored in the database file and persists). They must be set here,
        on the actual connection used for scanning, not just on the
        throwaway connection opened by initialize() - otherwise every
        commit on the hot path silently falls back to synchronous=FULL
        (fsync on every commit), which is a major, easy-to-miss slowdown.
        """
        connection = self._connection
        if connection is None:
            connection = sqlite3.connect(self.db_path, check_same_thread=False)
            cursor = connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA temp_store=MEMORY")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA cache_size=-32000")
            cursor.execute("PRAGMA mmap_size=268435456")
            self._connection = connection
            self._cursor = cursor
            return connection, cursor

        cursor = self._cursor
        if cursor is None:
            cursor = connection.cursor()
            self._cursor = cursor

        return connection, cursor

    def close_persistent(self) -> None:
        """Close persistent connection"""
        if self._connection:
            self._connection.close()
            self._connection = None
            self._cursor = None


# Scan DB
scan_db_path = Path(__file__).resolve().parent / config.scan_db_path
scan_db_path.parent.mkdir(parents=True, exist_ok=True)
scan_db_manager = DatabaseManager(str(scan_db_path))
scan_db_manager.initialize()
scan_con, scan_cur = scan_db_manager.get_persistent_connection()

# Router DB
router_db_path = Path(__file__).resolve().parent / config.router_db_path
router_db_path.parent.mkdir(parents=True, exist_ok=True)
router_db_manager = DatabaseManager(str(router_db_path))
router_db_manager.initialize()
router_con, router_cur = router_db_manager.get_persistent_connection()

# Legacy fallback db_manager (kept for on_app_close compatibility)
db_manager = scan_db_manager
con, cur = scan_con, scan_cur

DB_PENDING_WRITES = 0
DB_LAST_COMMIT_AT = 0.0
JSON_SAVE_TRACKERS: Dict[str, Dict[str, float]] = {}
JSON_SAVE_FAILURE_TRACKERS: Dict[str, Dict[str, Any]] = {}
JSON_SAVE_WARNING_COOLDOWN: float = 20.0


def flush_db_writes() -> None:
    """Flush pending SQLite writes."""
    global DB_PENDING_WRITES, DB_LAST_COMMIT_AT
    with state.db_lock:
        if DB_PENDING_WRITES > 0:
            scan_con.commit()
            DB_PENDING_WRITES = 0
            DB_LAST_COMMIT_AT = time.monotonic()


def db_insert_match(ref_value: str, file_value: str, force_commit: bool = False) -> None:
    """Insert a match row and commit in batches for better throughput."""
    global DB_PENDING_WRITES, DB_LAST_COMMIT_AT
    with state.db_lock:
        scan_cur.execute(
            "INSERT INTO matches(ref,file,time) VALUES (?,?,datetime('now'))",
            (ref_value, file_value)
        )
        DB_PENDING_WRITES += 1
        now = time.monotonic()
        if force_commit or DB_PENDING_WRITES >= config.db_commit_every or (now - DB_LAST_COMMIT_AT) >= config.db_commit_interval:
            scan_con.commit()
            DB_PENDING_WRITES = 0
            DB_LAST_COMMIT_AT = now


def persist_json_state(path: Path, data: Dict[str, Any], force: bool = False) -> bool:
    """Persist JSON state with small batching to reduce disk I/O."""
    try:
        resolved = str(Path(path).resolve())
        tracker = JSON_SAVE_TRACKERS.setdefault(resolved, {"pending": 0, "last_flush": 0.0})
        tracker["pending"] = int(tracker.get("pending", 0)) + 1
        now = time.monotonic()
        if not force and tracker["pending"] < config.state_flush_every and (now - float(tracker.get("last_flush", 0.0))) < config.state_flush_interval:
            return True

        save_json_atomic(path, data)
        tracker["pending"] = 0
        tracker["last_flush"] = now
        JSON_SAVE_FAILURE_TRACKERS.pop(resolved, None)
        return True
    except Exception as e:
        resolved = str(Path(path).resolve())
        fail_tracker = JSON_SAVE_FAILURE_TRACKERS.setdefault(resolved, {"count": 0, "last_warned": 0.0})
        fail_tracker["count"] = int(fail_tracker.get("count", 0)) + 1
        now = time.monotonic()
        if now - float(fail_tracker.get("last_warned", 0.0)) >= JSON_SAVE_WARNING_COOLDOWN:
            fail_tracker["last_warned"] = now
            is_permission_error = isinstance(e, PermissionError) or "Permission denied" in str(e)
            hint = (
                " — ფაილი მუდმივად დაბლოკილია (ხშირად ანტივირუსის ან OneDrive/Dropbox-ის მსგავსი "
                "სინქრონიზაციის მიზეზით). გადამოწმება: (1) გახსენი ეს საქაღალდე ისეთ ადგილას, "
                "რომელიც არ სინქრონდება ღრუბელთან (მაგ. Downloads ხშირად OneDrive-ზეა მიბმული), "
                "(2) დაამატე გამონაკლისი ანტივირუსში scan_state საქაღალდისთვის, "
                "(3) დარწმუნდი, რომ ეს ფაილი სხვა პროგრამაში (ტექსტ ედიტორი და სხვ.) არ არის გახსნილი. "
                "სკანირება მაინც გრძელდება — მხოლოდ პროგრესის შენახვა ფერხდება ამ ხნის განმავლობაში."
            ) if is_permission_error else ""
            logger.warning(
                f"State save warning ({fail_tracker['count']}x): {e}{hint}"
            )
        return False


def build_reference_db_value(files: List[str]) -> str:
    """Build stable, compact DB value for selected reference files."""
    if not files:
        return ""

    resolved_files = sorted(str(Path(p).resolve()) for p in files)
    signature = ref_signature(resolved_files)
    preview_names = [Path(p).name for p in resolved_files[:5]]
    preview = ", ".join(preview_names)
    if len(resolved_files) > 5:
        preview += f" ... (+{len(resolved_files) - 5})"
    return f"refs:{signature}|count={len(resolved_files)}|files={preview}"








# ----------- HELPERS --------------
def now_iso() -> str:
    """Return current timestamp in ISO format"""
    return datetime.now().isoformat(timespec="seconds")


def sanitize_filename(text: str) -> str:
    """Sanitize text for use as filename"""
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in text)
    return safe.strip("._") or "წყარო"


def get_state_path(src_path: Path) -> Path:
    """Get state file path for given source directory"""
    src_path = src_path.resolve()
    src_hash = hashlib.sha1(str(src_path).lower().encode("utf-8")).hexdigest()[:12]
    return SCAN_STATE_DIR / f"{sanitize_filename(src_path.name)}_{src_hash}.json"


def ref_signature(files: List[str]) -> str:
    """Generate signature for reference files"""
    parts = []
    for f in sorted(str(Path(p).resolve()) for p in files):
        p = Path(f)
        try:
            st = p.stat()
            parts.append(f"{f}|{st.st_size}|{int(st.st_mtime_ns)}")
        except OSError as e:
            logger.warning(f"Could not stat file {f}: {e}")
            parts.append(f)
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def load_json(path: Path) -> Dict[str, Any]:
    """Load JSON file with error handling"""
    if not path.exists():
        return {
            "version": 2,
            "source_folder": "",
            "folders": {}
        }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("Invalid JSON structure")
        return data
    except json.JSONDecodeError as e:
        logger.error(f"JSON decode error in {path}: {e}")
        _backup_corrupted_file(path)
        return {"version": 2, "source_folder": "", "folders": {}}
    except Exception as e:
        logger.error(f"Error loading {path}: {e}")
        _backup_corrupted_file(path)
        return {"version": 2, "source_folder": "", "folders": {}}


def _backup_corrupted_file(path: Path) -> None:
    """Backup corrupted file"""
    backup = path.with_suffix(path.suffix + ".broken")
    try:
        shutil.copy2(path, backup)
        logger.info(f"Backed up corrupted file to {backup}")
    except OSError as e:
        logger.warning(f"Could not backup corrupted file: {e}")


def migrate_legacy_state(data: Dict, src_path: Path) -> Dict[str, Any]:
    """Migrate legacy state format to current version"""
    src_resolved = str(src_path.resolve())

    if not isinstance(data, dict):
        data = {}

    if "folders" in data and isinstance(data.get("folders"), dict):
        data.setdefault("version", 2)
        data["source_folder"] = src_resolved
        return data

    migrated: Dict[str, Any] = {
        "version": 2,
        "source_folder": src_resolved,
        "created_at": data.get("created_at", now_iso()),
        "updated_at": now_iso(),
        "folders": {}
    }

    profiles = data.get("profiles", {}) if isinstance(data.get("profiles", {}), dict) else {}
    for profile in profiles.values():
        folders = profile.get("folders", {}) if isinstance(profile, dict) else {}
        for folder_name, files in folders.items():
            if not isinstance(files, dict):
                continue
            bucket = migrated["folders"].setdefault(folder_name, {})
            for file_name, meta in files.items():
                if not isinstance(meta, dict):
                    continue
                bucket[file_name] = meta

    return migrated


def save_json_atomic(path: Path, data: Dict, retries: int = 12, base_delay: float = 0.08) -> None:
    """
    Atomically save JSON with retry logic for Windows file locks
    (e.g. antivirus real-time scanning or cloud-sync clients like OneDrive/Dropbox
    briefly holding an exclusive handle on the file).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    payload = json.dumps(data, ensure_ascii=False, indent=2)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.stem + "_",
        suffix=path.suffix + ".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        last_error: Optional[Exception] = None
        for attempt in range(retries):
            try:
                os.replace(tmp, path)
                return
            except PermissionError as e:
                last_error = e
                time.sleep(base_delay * (attempt + 1) + random.uniform(0, base_delay))
            except OSError as e:
                last_error = e
                time.sleep(base_delay * (attempt + 1) + random.uniform(0, base_delay))

        # Fallback: direct write
        for attempt in range(retries):
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(payload)
                    f.flush()
                    os.fsync(f.fileno())
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                return
            except (PermissionError, OSError) as e:
                last_error = e
                time.sleep(base_delay * (attempt + 1) + random.uniform(0, base_delay))

        if last_error:
            raise last_error
        raise RuntimeError(f"State save failed: {path}")
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def persist_state(force: bool = False) -> bool:
    """Persist current state to disk"""
    if not state.scan_state_path or not state.scan_state:
        return True
    return persist_json_state(state.scan_state_path, state.scan_state, force=force)


def clear_completed_json_state(path: Optional[Path]) -> bool:
    """Remove only the completed operation's resumable JSON state file."""
    if path is None:
        return False
    try:
        target = Path(path).resolve()
        allowed_roots = (SCAN_STATE_DIR.resolve(), ROUTER_STATE_DIR.resolve())
        if not any(target.parent == root for root in allowed_roots) or target.suffix.lower() != ".json":
            raise ValueError(f"Unsafe state path: {target}")
        target.unlink(missing_ok=True)
        JSON_SAVE_TRACKERS.pop(str(target), None)
        JSON_SAVE_FAILURE_TRACKERS.pop(str(target), None)
        logger.info("Completed resume JSON cleared: %s", target)
        return True
    except Exception as exc:
        logger.warning("Completed resume JSON could not be cleared: %s", exc)
        return False

def scan_state_file_name() -> str:
    """Return current scan-state filename safely for UI messages."""
    return state.scan_state_path.name if state.scan_state_path is not None else "უცნობია"



def folder_key_from_rel(rel: Path) -> str:
    """Get folder key from relative path"""
    folder = rel.parent.as_posix()
    return folder if folder not in ("", ".") else "__root__"


def ensure_profile() -> Dict:
    """Ensure current profile exists"""
    if state.current_profile is None:
        raise RuntimeError("სკანირების მდგომარეობა არ არის მომზადებული")
    state.current_profile.setdefault("folders", {})
    return state.current_profile


def get_completed_rel_paths(profile: Dict) -> Set[str]:
    """Get set of completed relative paths"""
    completed: Set[str] = set()
    for folder_name, files in profile.get("folders", {}).items():
        prefix = "" if folder_name == "__root__" else f"{folder_name}/"
        for file_name, meta in files.items():
            if meta.get("status") in {"matched", "nonmatched", "error", "duplicate"}:
                completed.add(prefix + file_name)
    return completed


def count_in_progress_entries(profile: Dict) -> int:
    """Count in-progress entries"""
    total = 0
    for files in profile.get("folders", {}).values():
        for meta in files.values():
            if meta.get("status") == "in_progress":
                total += 1
    return total


def prepare_scan_state(src_path: Path, out_path: Path, threshold: float, worker_count: int = 4) -> Dict:
    """Prepare scan state for new scan"""
    state.scan_state_path = get_state_path(src_path)
    loaded = load_json(state.scan_state_path)
    state.scan_state = migrate_legacy_state(loaded, src_path)

    if state.scan_state.get("source_folder") and state.scan_state.get("source_folder") != str(src_path.resolve()):
        state.scan_state = {
            "version": 2,
            "source_folder": str(src_path.resolve()),
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "folders": {}
        }

    state.scan_state.setdefault("created_at", now_iso())
    state.scan_state["mode"]             = "scan"
    state.scan_state["source_folder"]    = str(src_path.resolve())
    state.scan_state["updated_at"]       = now_iso()
    state.scan_state.setdefault("folders", {})
    # params block – mirrors router's top-level naming (no "last_" prefix)
    state.scan_state["params"] = {
        "output_folder":    str(out_path.resolve()),
        "threshold":        round(float(threshold), 4),
        "worker_count":     int(worker_count),
        "reference_files":  [str(Path(p).resolve()) for p in state.ref_files],
        "reference_names":  [Path(p).name for p in state.ref_files],
    }
    # keep legacy last_* keys so history restore still works
    state.scan_state["last_output_folder"]   = str(out_path.resolve())
    state.scan_state["last_threshold"]       = round(float(threshold), 4)
    state.scan_state["last_worker_count"]    = int(worker_count)
    state.scan_state["last_reference_files"] = [str(Path(p).resolve()) for p in state.ref_files]
    state.scan_state["last_reference_names"] = [Path(p).name for p in state.ref_files]
    # stats will be updated on finish / stop
    state.scan_state.setdefault("stats", {
        "total": 0, "matched": 0, "nonmatched": 0,
        "duplicates": 0, "errors": 0, "resumed": 0,
    })
    state.current_profile = state.scan_state
    persist_state(force=True)
    return state.current_profile



def get_router_state_path(src_path: Path) -> Path:
    """Get state file path for multi-person router mode."""
    src_path = src_path.resolve()
    src_hash = hashlib.sha1((str(src_path).lower() + "|multi_router").encode("utf-8")).hexdigest()[:12]
    return ROUTER_STATE_DIR / f"{sanitize_filename(src_path.name)}_{src_hash}.json"









def get_router_completed_rel_paths(router_state: Dict[str, Any]) -> Set[str]:
    """Get completed relative file paths for router mode."""
    completed: Set[str] = set()
    for rel_path, meta in router_state.get("files", {}).items():
        if isinstance(meta, dict) and meta.get("status") in {"matched", "unmatched", "error"}:
            completed.add(rel_path)
    return completed



def count_router_in_progress_entries(router_state: Dict[str, Any]) -> int:
    """Count in-progress files in router state."""
    total = 0
    for meta in router_state.get("files", {}).values():
        if isinstance(meta, dict) and meta.get("status") == "in_progress":
            total += 1
    return total



def mark_router_scan_in_progress(
    router_state: Dict[str, Any],
    router_state_path: Path,
    router_state_lock: threading.RLock,
    rel_path: Path,
    **extra: Any,
) -> None:
    """Mark router file as in-progress."""
    with router_state_lock:
        files_bucket = router_state.setdefault("files", {})
        key = rel_path.as_posix()
        existing = files_bucket.get(key, {})
        files_bucket[key] = {
            "relative_path": key,
            "status": "in_progress",
            "started_at": now_iso(),
            "last_seen_at": now_iso(),
            "attempts": int(existing.get("attempts", 0)) + 1,
            **extra,
        }
        router_state["updated_at"] = now_iso()
        persist_json_state(router_state_path, router_state)







def mark_scan_in_progress(rel_path: Path, **extra: Any) -> None:
    """Mark file as in-progress"""
    with state.state_lock:
        profile = ensure_profile()
        folder_name = folder_key_from_rel(rel_path)
        folder_bucket = profile["folders"].setdefault(folder_name, {})
        existing = folder_bucket.get(rel_path.name, {})
        attempts = int(existing.get("attempts", 0)) + 1
        entry = {
            "file_name": rel_path.name,
            "relative_path": rel_path.as_posix(),
            "status": "in_progress",
            "started_at": now_iso(),
            "last_seen_at": now_iso(),
            "attempts": attempts,
            **extra,
        }
        folder_bucket[rel_path.name] = entry
        profile["updated_at"] = now_iso()
        persist_state()


# ----------- FACE PROCESSING FUNCTIONS --------------


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Calculate cosine similarity"""
    return float(np.dot(a, b))


def phash(path: Union[str, Path, np.ndarray]) -> str:
    """Calculate perceptual hash of image"""
    if isinstance(path, np.ndarray):
        if path.ndim == 2:
            pil_img = Image.fromarray(path)
        else:
            pil_img = Image.fromarray(cv2.cvtColor(path, cv2.COLOR_BGR2RGB))
        return str(imagehash.phash(pil_img))

    with Image.open(path) as img:
        return str(imagehash.phash(img))


def phash_with_size(img: np.ndarray) -> str:
    """Calculate perceptual hash including image dimensions to detect same-content but different-size duplicates."""
    h, w = img.shape[:2]
    if img.ndim == 2:
        pil_img = Image.fromarray(img)
    else:
        pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    content_hash = str(imagehash.phash(pil_img))
    return f"{content_hash}_{w}x{h}"


def are_same_content_different_size(img1: np.ndarray, img2_hash_no_size: str) -> bool:
    """Check if two images have same visual content regardless of size."""
    if img1.ndim == 2:
        pil_img = Image.fromarray(img1)
    else:
        pil_img = Image.fromarray(cv2.cvtColor(img1, cv2.COLOR_BGR2RGB))
    content_hash = str(imagehash.phash(pil_img))
    return content_hash == img2_hash_no_size


def check_face_quality(image_path: Union[str, Path]) -> Tuple[int, Dict[str, str]]:
    """
    ულტრა-მკაცრი, მრავალფაქტორიანი ფოტო-ანალიზატორი საცნობარო ფოტოსთვის.

    ამოწმებს: რეზოლუციას, სახის ზომას/პოზიციას/კადრირებას, სიმკვეთრეს,
    სინათლე/კონტრასტს, დინამიკურ დიაპაზონს, გადაჭრილ შავ/თეთრ უბნებს,
    დეტექტორის სანდოობას, თავის კუთხეს (yaw/pitch/roll), თვალების
    მდგომარეობას (დახუჭული/დაფარული/ბუნდოვანი/ჩრდილიანი) და შეკუმშვის
    (JPEG) ბლოკურ არტეფაქტებს. თუ ერთდროულად რამდენიმე ხარვეზი გვაქვს,
    საბოლოო ქულა დამატებით მცირდება (კომბინირებული ეფექტი), რადგან
    რამდენიმე ერთდროული ხარვეზი რეალურ ამოცნობაზე გავლენას მრავლდება და
    არა უბრალოდ იკრიბება.

    Returns: (quality_percentage, details_dict)
    """
    try:
        img = load_image_bgr(image_path)
        if img is None:
            return 0, {"error": "ფოტო ვერ ჩაიტვირთა"}

        h, w = img.shape[:2]
        if h < 200 or w < 200:
            return 0, {
                "error": f"ფოტოს რეზოლუცია ძალიან დაბალია ({w}x{h})",
                "recommendation": "აირჩიე უფრო ხარისხიანი ფოტო"
            }

        faces = detect_faces(img)

        if not faces:
            return 0, {
                "error": "სახე ვერ მოიძებნა",
                "recommendation": "აირჩიე უფრო ნათელი, მკვეთრი და წინა მხრიდან გადაღებული ფოტო"
            }

        if len(faces) > 1:
            return 10, {
                "warning": f"აღმოჩენილია რამდენიმე სახე ({len(faces)})",
                "recommendation": "საცნობარო ფოტოში მხოლოდ ერთი ადამიანი უნდა იყოს"
            }

        face = faces[0]
        quality_score = 100.0
        details: Dict[str, str] = {}
        hard_fail_reasons: List[str] = []
        moderate_issue_count = 0  # ერთდროული საშუალო/მძიმე ხარვეზების დათვლა კომბინირებული ეფექტისთვის

        # --- სურათის საერთო ზომა ---
        min_side = min(h, w)
        if min_side < 360:
            hard_fail_reasons.append(f"ფოტოს რეზოლუცია ძალიან დაბალია ({w}x{h})")
            details["resolution"] = f"ძალიან დაბალი რეზოლუცია ({w}x{h})"
        elif min_side < 512:
            quality_score -= 20
            moderate_issue_count += 1
            details["resolution"] = f"დაბალი რეზოლუცია ({w}x{h})"
        elif min_side < 720:
            quality_score -= 8
            details["resolution"] = f"საშუალო რეზოლუცია ({w}x{h})"
        else:
            details["resolution"] = f"კარგი რეზოლუცია ({w}x{h})"

        # --- bbox უსაფრთხოდ ---
        bbox = np.array(face.bbox).astype(int)
        x1, y1, x2, y2 = bbox.tolist()
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(w, x2)
        y2 = min(h, y2)

        if x2 <= x1 or y2 <= y1:
            return 0, {
                "error": "სახის არე დაზიანებულია",
                "recommendation": "სხვა ფოტო სცადე"
            }

        face_w = x2 - x1
        face_h = y2 - y1
        face_area = face_w * face_h
        img_area = max(1, h * w)
        face_ratio = face_area / img_area

        # --- სახის ზომა ---
        if face_w < 140 or face_h < 140 or face_ratio < 0.06:
            hard_fail_reasons.append(
                f"სახე ძალიან პატარაა ({face_w}x{face_h}, სურათის {face_ratio*100:.1f}%)"
            )
            details["face_size"] = f"ძალიან პატარა სახე ({face_w}x{face_h}, {face_ratio*100:.1f}%)"
        elif face_w < 200 or face_h < 200 or face_ratio < 0.10:
            quality_score -= 28
            moderate_issue_count += 1
            details["face_size"] = f"პატარა სახე ({face_w}x{face_h}, {face_ratio*100:.1f}%)"
        elif face_w < 260 or face_h < 260 or face_ratio < 0.15:
            quality_score -= 14
            details["face_size"] = f"საშუალო ზომის სახე ({face_w}x{face_h}, {face_ratio*100:.1f}%)"
        else:
            details["face_size"] = f"კარგი ზომის სახეა ({face_w}x{face_h}, {face_ratio*100:.1f}%)"

        # --- კადრიდან მოჭრა ---
        margin_x = max(6, int(w * 0.02))
        margin_y = max(6, int(h * 0.02))
        touches_border = (
            x1 <= margin_x or
            y1 <= margin_y or
            x2 >= (w - margin_x) or
            y2 >= (h - margin_y)
        )
        if touches_border:
            quality_score -= 18
            moderate_issue_count += 1
            details["crop"] = "სახე ძალიან ახლოსაა კიდესთან ან ოდნავ მოჭრილია"
        else:
            details["crop"] = "სახე კადრში ნორმალურად ჯდება"

        # --- ცენტრთან სიახლოვე ---
        face_cx = (x1 + x2) / 2.0
        face_cy = (y1 + y2) / 2.0
        off_x = abs(face_cx - (w / 2)) / max(1.0, (w / 2))
        off_y = abs(face_cy - (h / 2)) / max(1.0, (h / 2))
        center_offset = max(off_x, off_y)

        if center_offset > 0.35:
            quality_score -= 15
            moderate_issue_count += 1
            details["position"] = "სახე ძალიან განზეა განთავსებული"
        elif center_offset > 0.22:
            quality_score -= 7
            details["position"] = "სახე ბოლომდე ცენტრში არ არის"
        else:
            details["position"] = "სახე კარგადაა განთავსებული"

        # --- face region ---
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        face_region = gray[y1:y2, x1:x2]
        if face_region.size == 0:
            return 0, {
                "error": "სახის ზონა ვერ დამუშავდა",
                "recommendation": "სხვა ფოტო სცადე"
            }

        # --- სიმკვეთრე ---
        laplacian_var = float(cv2.Laplacian(face_region, cv2.CV_64F).var())
        if laplacian_var < 70:
            hard_fail_reasons.append(f"ფოტო ძალიან ბუნდოვანია (სიმკვეთრე: {laplacian_var:.1f})")
            details["sharpness"] = f"ძალიან ბუნდოვანია ({laplacian_var:.1f})"
        elif laplacian_var < 120:
            quality_score -= 22
            moderate_issue_count += 1
            details["sharpness"] = f"ბუნდოვანია ({laplacian_var:.1f})"
        elif laplacian_var < 180:
            quality_score -= 10
            details["sharpness"] = f"საშუალო სიმკვეთრე ({laplacian_var:.1f})"
        else:
            details["sharpness"] = f"კარგი სიმკვეთრე ({laplacian_var:.1f})"

        # --- სიკაშკაშე ---
        face_brightness = float(np.mean(face_region))
        if face_brightness < 60 or face_brightness > 195:
            quality_score -= 18
            moderate_issue_count += 1
            details["brightness"] = f"არასწორი სიკაშკაშე ({face_brightness:.1f})"
        elif face_brightness < 75 or face_brightness > 180:
            quality_score -= 8
            details["brightness"] = f"საშუალო სიკაშკაშე ({face_brightness:.1f})"
        else:
            details["brightness"] = f"კარგი სიკაშკაშე ({face_brightness:.1f})"

        # --- კონტრასტი ---
        contrast = float(np.std(face_region))
        if contrast < 28:
            quality_score -= 18
            moderate_issue_count += 1
            details["contrast"] = f"ძალიან დაბალი კონტრასტი ({contrast:.1f})"
        elif contrast < 40:
            quality_score -= 8
            details["contrast"] = f"დაბალი კონტრასტი ({contrast:.1f})"
        else:
            details["contrast"] = f"კარგი კონტრასტი ({contrast:.1f})"

        # --- დინამიკური დიაპაზონი ---
        p5, p95 = np.percentile(face_region, [5, 95])
        dynamic_range = float(p95 - p5)
        if dynamic_range < 55:
            quality_score -= 12
            moderate_issue_count += 1
            details["dynamic_range"] = f"ცუდი ტონალური დიაპაზონი ({dynamic_range:.1f})"
        elif dynamic_range < 80:
            quality_score -= 5
            details["dynamic_range"] = f"საშუალო ტონალური დიაპაზონი ({dynamic_range:.1f})"
        else:
            details["dynamic_range"] = f"კარგი ტონალური დიაპაზონი ({dynamic_range:.1f})"

        # --- გადაჭრილი შავი/თეთრი უბნები ---
        dark_clip = float((face_region < 20).mean() * 100.0)
        bright_clip = float((face_region > 235).mean() * 100.0)

        if dark_clip > 35:
            quality_score -= 12
            moderate_issue_count += 1
            details["shadow_clip"] = f"ძალიან ბევრი ჩაბნელებული უბანია ({dark_clip:.1f}%)"
        elif dark_clip > 18:
            quality_score -= 5
            details["shadow_clip"] = f"ჩაბნელებული უბნები შეინიშნება ({dark_clip:.1f}%)"

        if bright_clip > 35:
            quality_score -= 12
            moderate_issue_count += 1
            details["highlight_clip"] = f"ძალიან ბევრი გადანათებული უბანია ({bright_clip:.1f}%)"
        elif bright_clip > 18:
            quality_score -= 5
            details["highlight_clip"] = f"გადანათებული უბნები შეინიშნება ({bright_clip:.1f}%)"

        # --- detector confidence ---
        det_score = float(getattr(face, "det_score", 1.0))
        if det_score < 0.65:
            hard_fail_reasons.append(f"სახის ამოცნობის სანდოობა ძალიან დაბალია ({det_score:.2f})")
            details["detection"] = f"ძალიან დაბალი detector confidence ({det_score:.2f})"
        elif det_score < 0.82:
            quality_score -= 14
            moderate_issue_count += 1
            details["detection"] = f"საშუალოზე დაბალი detector confidence ({det_score:.2f})"
        elif det_score < 0.90:
            quality_score -= 6
            details["detection"] = f"საშუალო detector confidence ({det_score:.2f})"
        else:
            details["detection"] = f"მაღალი detector confidence ({det_score:.2f})"

        # --- პოზა ---
        if hasattr(face, "pose") and face.pose is not None:
            pose = np.asarray(face.pose, dtype=float).flatten()
            yaw = abs(float(pose[0])) if len(pose) > 0 else 0.0
            pitch = abs(float(pose[1])) if len(pose) > 1 else 0.0
            roll = abs(float(pose[2])) if len(pose) > 2 else 0.0
            max_angle = max(yaw, pitch, roll)

            if max_angle > 32:
                hard_fail_reasons.append(
                    f"თავის კუთხე ძალიან დიდია (yaw: {yaw:.1f}, pitch: {pitch:.1f}, roll: {roll:.1f})"
                )
                details["pose"] = f"ძალიან ცუდი კუთხეა (yaw {yaw:.1f}, pitch {pitch:.1f}, roll {roll:.1f})"
            elif max_angle > 20:
                quality_score -= 16
                moderate_issue_count += 1
                details["pose"] = f"ცუდი კუთხეა (yaw {yaw:.1f}, pitch {pitch:.1f}, roll {roll:.1f})"
            elif max_angle > 12:
                quality_score -= 7
                details["pose"] = f"საშუალო კუთხეა (yaw {yaw:.1f}, pitch {pitch:.1f}, roll {roll:.1f})"
            else:
                details["pose"] = f"კარგი კუთხეა (yaw {yaw:.1f}, pitch {pitch:.1f}, roll {roll:.1f})"

        # --- თვალების ანალიზი: დახუჭული / დაფარული (სათვალე, თმა, ხელი) / ბუნდოვანი ---
        kps = getattr(face, "kps", None)
        if kps is not None:
            try:
                kps_arr = np.asarray(kps, dtype=float)
                if kps_arr.shape[0] >= 2:
                    left_eye = kps_arr[0]
                    right_eye = kps_arr[1]
                    interocular = float(np.linalg.norm(left_eye - right_eye))
                    eye_ratio = interocular / max(1.0, face_w)

                    if eye_ratio < 0.16:
                        quality_score -= 8
                        details["landmarks"] = (
                            f"თვალებს შორის მანძილი არაბუნებრივად მცირეა ({eye_ratio:.2f}) — "
                            f"შესაძლოა ძლიერი გვერდითი კუთხე ან არასწორი დეტექცია"
                        )
                    else:
                        details["landmarks"] = f"სახის პროპორციები ბუნებრივია (თვალთაშორისი შეფარდება {eye_ratio:.2f})"

                    eye_half = max(7, int(round(interocular * 0.30)))
                    eye_patch_stats = []
                    for (ex, ey) in (left_eye, right_eye):
                        ex_i, ey_i = int(round(ex)), int(round(ey))
                        ex0 = max(0, ex_i - eye_half)
                        ex1 = min(w, ex_i + eye_half)
                        ey0 = max(0, ey_i - eye_half)
                        ey1 = min(h, ey_i + eye_half)
                        patch = gray[ey0:ey1, ex0:ex1]
                        if patch.size == 0:
                            continue
                        p_var = float(cv2.Laplacian(patch, cv2.CV_64F).var())
                        p_std = float(np.std(patch))
                        p_mean = float(np.mean(patch))
                        eye_patch_stats.append((p_var, p_std, p_mean))

                    if eye_patch_stats:
                        avg_eye_var = float(np.mean([s[0] for s in eye_patch_stats]))
                        avg_eye_std = float(np.mean([s[1] for s in eye_patch_stats]))
                        avg_eye_mean = float(np.mean([s[2] for s in eye_patch_stats]))

                        if avg_eye_std < 9 or avg_eye_var < 12:
                            hard_fail_reasons.append(
                                "თვალების არეში დეტალი თითქმის არ იკვეთება — "
                                "სავარაუდოდ თვალები დახუჭულია ან დაფარულია (სათვალე/თმა/ხელი)"
                            )
                            details["eyes"] = (
                                f"თვალები გაურკვეველია (მკვეთრობა {avg_eye_var:.1f}, ვარიაცია {avg_eye_std:.1f})"
                            )
                        elif avg_eye_var < 40:
                            quality_score -= 16
                            moderate_issue_count += 1
                            details["eyes"] = f"თვალების არე ბუნდოვანია (მკვეთრობა {avg_eye_var:.1f})"
                        elif avg_eye_mean < 35:
                            quality_score -= 12
                            moderate_issue_count += 1
                            details["eyes"] = (
                                f"თვალების არე ძალიან მუქია — შესაძლოა ჩრდილი ან მუქი სათვალეა "
                                f"(სიკაშკაშე {avg_eye_mean:.1f})"
                            )
                        else:
                            details["eyes"] = f"თვალები კარგად ჩანს (მკვეთრობა {avg_eye_var:.1f})"
            except Exception:
                pass

        # --- შეკუმშვის ბლოკური არტეფაქტები (JPEG blockiness) ---
        try:
            fr_h, fr_w = face_region.shape
            if fr_h >= 24 and fr_w >= 24:
                fr = face_region.astype(np.float32)
                boundary_diffs = []
                interior_diffs = []
                for cx in range(8, fr_w - 1, 8):
                    boundary_diffs.append(float(np.mean(np.abs(fr[:, cx] - fr[:, cx - 1]))))
                for cx in range(4, fr_w - 1, 8):
                    interior_diffs.append(float(np.mean(np.abs(fr[:, cx] - fr[:, cx - 1]))))
                if boundary_diffs and interior_diffs:
                    b_mean = float(np.mean(boundary_diffs))
                    i_mean = max(0.15, float(np.mean(interior_diffs)))
                    blockiness = b_mean / i_mean
                    if blockiness > 2.1:
                        quality_score -= 14
                        moderate_issue_count += 1
                        details["compression"] = f"შესამჩნევია შეკუმშვის ბლოკური არტეფაქტები (ინდექსი {blockiness:.2f})"
                    elif blockiness > 1.5:
                        quality_score -= 6
                        details["compression"] = f"მსუბუქი შეკუმშვის კვალი (ინდექსი {blockiness:.2f})"
                    else:
                        details["compression"] = "შეკუმშვის არტეფაქტები არ შეინიშნება"
        except Exception:
            pass

        # --- კომბინირებული ეფექტი: რამდენიმე ერთდროული საშუალო/მძიმე ხარვეზი ---
        if moderate_issue_count >= 4:
            quality_score -= 15
            details["compounding"] = (
                f"აღმოჩენილია {moderate_issue_count} ერთდროული ხარისხის ხარვეზი — "
                f"საერთო სანდოობა დამატებით მცირდება"
            )
        elif moderate_issue_count == 3:
            quality_score -= 8
            details["compounding"] = f"აღმოჩენილია {moderate_issue_count} ერთდროული ხარისხის ხარვეზი"

        # --- საბოლოო გამკაცრება ---
        if hard_fail_reasons:
            quality_score = min(quality_score, 35)

        quality_score = int(max(0, min(100, round(quality_score))))

        if hard_fail_reasons:
            details["critical"] = "კრიტიკული პრობლემა: " + "; ".join(hard_fail_reasons)
            details["verdict"] = "ვერდიქტი: უარყოფილია საცნობარო ფოტოდ"
            details["recommendation"] = "ეს ფოტო არ ჩასვა reference-ად"
        elif quality_score >= 88:
            details["verdict"] = "ვერდიქტი: მიღებულია"
            details["recommendation"] = "ძალიან კარგი reference ფოტოა"
        elif quality_score >= 78:
            details["verdict"] = "ვერდიქტი: კარგია"
            details["recommendation"] = "გამოდგება reference ფოტოდ"
        elif quality_score >= 65:
            details["verdict"] = "ვერდიქტი: საზღვარზეა"
            details["recommendation"] = "სჯობს უკეთესი ფოტო მოძებნო"
        else:
            details["verdict"] = "ვერდიქტი: არ არის რეკომენდებული"
            details["recommendation"] = "ეს ფოტო არ ჩასვა reference-ად"

        return quality_score, details

    except Exception as e:
        logger.error(f"Quality check error: {e}")
        return 0, {"error": str(e), "recommendation": "სხვა ფოტო სცადე"}


def clear_pending_queue() -> int:
    """Clear pending items from queue"""
    removed = 0
    while True:
        try:
            item = state.file_q.get_nowait()
        except queue.Empty:
            break
        else:
            if item is not None:
                removed += 1
            state.file_q.task_done()
    return removed


def should_pause_for_resources() -> bool:
    """Throttle expensive psutil polling and reuse the last decision briefly."""
    now = time.monotonic()
    with state.system_check_lock:
        if (now - state.last_system_check_at) < config.system_check_interval:
            return state.should_pause_for_resources
        state.last_system_check_at = now
        pause_needed = (
            psutil.cpu_percent(interval=None) > config.cpu_threshold
            or psutil.virtual_memory().percent > config.memory_threshold
        )
        state.should_pause_for_resources = pause_needed
    return pause_needed


def resource_throttle_delay(profile: Optional[str] = None) -> float:
    """Return a small adaptive delay that leaves the desktop responsive."""
    profile = str(profile or "ავტომატური")
    if profile == "მაქსიმალური":
        return 0.0

    now = time.monotonic()
    with state.system_check_lock:
        if (now - state.last_system_check_at) >= config.system_check_interval:
            state.last_system_check_at = now
            cpu_load = float(psutil.cpu_percent(interval=None))
            memory_load = float(psutil.virtual_memory().percent)
            state.should_pause_for_resources = (
                cpu_load >= config.cpu_threshold or memory_load >= config.memory_threshold
            )
            state.resource_cpu_load = cpu_load
            state.resource_memory_load = memory_load
        else:
            cpu_load = float(getattr(state, "resource_cpu_load", 0.0))
            memory_load = float(getattr(state, "resource_memory_load", 0.0))

    pressure = max(cpu_load / max(1.0, config.cpu_threshold),
                   memory_load / max(1.0, config.memory_threshold))
    if profile == "ეკონომიური":
        return 0.20 if pressure < 0.80 else 0.45
    if profile == "დაბალანსებული":
        return 0.04 if pressure < 0.78 else (0.14 if pressure < 1.0 else 0.32)
    # Auto uses all available headroom, then yields quickly when another app
    # starts consuming CPU or memory.
    if pressure < 0.68:
        return 0.0
    if pressure < 0.85:
        return 0.025
    if pressure < 1.0:
        return 0.09
    return 0.28


def format_scan_duration(seconds: float) -> str:
    """Format scan duration like the 1-20 person router summary."""
    seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}ს {minutes}წ {secs}წმ"
    if minutes > 0:
        return f"{minutes}წ {secs}წმ"
    return f"{secs}წმ"


def get_scan_time_stats(total_files: int, done: int) -> Tuple[float, float, float]:
    """Return elapsed time, ETA and speed for the main-page summary."""
    start_time = float(state.scan_started_at or 0.0)
    elapsed = max(0.0, time.time() - start_time) if start_time > 0 else 0.0
    processed_now = max(0, int(done) - int(state.stats.resumed))
    remaining = max(0, int(total_files) - int(done))
    average = (elapsed / processed_now) if processed_now > 0 else 0.0
    eta = remaining * average if average > 0 else 0.0
    speed = (processed_now / elapsed) if elapsed > 0 and processed_now > 0 else 0.0
    return elapsed, eta, speed


def update_scan_summary(total_files: int, done: Optional[int] = None) -> None:
    """Update the main-page information box using the router-style layout."""
    summary_widget = globals().get("scan_summary_text")
    if summary_widget is None:
        return

    if done is None:
        with state.progress_lock:
            done = int(state.progress_completed)

    total_files = max(0, int(total_files))
    done = max(0, int(done))
    pct = (done / total_files * 100.0) if total_files > 0 else 0.0
    remaining_count = max(0, total_files - done)

    with state.set_lock:
        matched_count = len(state.matched)
        nonmatched_count = len(state.nonmatched)
    with state.progress_lock:
        duplicate_count = int(state.stats.duplicates)
        error_count = int(state.stats.errors)
        resumed_count = int(state.stats.resumed)

    elapsed, eta, speed = get_scan_time_stats(total_files, done)
    eta_text = format_scan_duration(eta) if speed > 0 else "ითვლება..."

    try:
        summary_widget.config(state=NORMAL)
        summary_widget.delete("1.0", END)
        summary_widget.insert(
            END,
            f"დამუშავებული: {done}/{total_files} ({pct:.1f}%) | "
            f"დარჩენილი: {remaining_count} ({max(0.0, 100.0 - pct):.1f}%)\n"
        )
        summary_widget.insert(
            END,
            f"დამთხვევა: {matched_count} | უდამთხვევო: {nonmatched_count} | "
            f"დუბლიკატი: {duplicate_count} | შეცდომა: {error_count}\n"
        )
        summary_widget.insert(
            END,
            f"წინადან გამოტოვებული: {resumed_count} | "
            f"გასული: {format_scan_duration(elapsed)} | დარჩენილი დრო: {eta_text} | "
            f"სიჩქარე: {speed:.2f} ფოტო/წმ\n"
        )
        summary_widget.config(state=DISABLED)
    except Exception:
        pass


def apply_scan_progress(
    pbar: tqdm,
    total_files: int,
    start_time: float,
    progress_bar: Progressbar,
    progress_label: Label
) -> None:
    """Apply scan progress on the Tk main thread."""
    with state.progress_lock:
        done = state.progress_completed
        state.ui_update_scheduled = False
        state.last_ui_update_at = time.monotonic()

    elapsed, eta, aggregate_speed = get_scan_time_stats(total_files, done)
    speed = aggregate_speed if aggregate_speed > 0 else 0.0
    delta = done - pbar.n

    if delta > 0:
        pbar.update(delta)

    pbar.set_postfix({
        "speed": f"{speed:.2f} სურ/წმ" if speed > 0 else "-",
        "ETA": f"{eta/60:.1f}წ",
        "elapsed": f"{elapsed/60:.1f}წ"
    })

    progress_bar['value'] = done
    pct = (done / total_files * 100) if total_files > 0 else 0.0
    remaining_count = max(0, total_files - done)
    progress_label.config(
        text=(
            f"პროგრესი: {done}/{total_files} ({pct:.1f}%) | "
            f"დარჩენილია: {remaining_count} ({100-pct:.1f}%) | "
            f"გამოტოვებულია: {state.stats.resumed} | "
            f"სიჩქარე: {speed:.2f} სურ/წმ | "
            f"დარჩენილი დრო: {format_scan_duration(eta) if speed > 0 else 'ითვლება...'} | "
            f"გასული დრო: {format_scan_duration(elapsed)}"
        )
    )
    update_scan_summary(total_files, done)


def schedule_scan_progress_update(
    pbar: tqdm,
    total_files: int,
    start_time: float,
    progress_bar: Progressbar,
    progress_label: Label,
    force: bool = False
) -> None:
    """Coalesce noisy progress updates into fewer Tk UI refreshes."""
    now = time.monotonic()
    with state.progress_lock:
        if state.ui_update_scheduled and not force:
            return
        if not force and (now - state.last_ui_update_at) < config.progress_update_interval:
            return
        state.ui_update_scheduled = True

    post_ui(lambda: apply_scan_progress(pbar, total_files, start_time, progress_bar, progress_label))




# ----------- GUI CALLBACKS --------------


def pick_src() -> None:
    """Pick source folder and restart the hidden Live Watch safely."""
    selected = filedialog.askdirectory()
    if selected:
        stop_live_watch(wait=True)
        state.src_folder = selected
        lbl_src.config(text=f"წყარო: {state.src_folder}")
        save_app_settings()
        root.after(200, ensure_live_watch_started)


def pick_out() -> None:
    """Pick output folder and restart the hidden Live Watch safely."""
    selected = filedialog.askdirectory()
    if selected:
        stop_live_watch(wait=True)
        state.out_folder = selected
        lbl_out.config(text=f"შედეგი: {state.out_folder}")
        save_app_settings()
        root.after(200, ensure_live_watch_started)








def flush_state_on_exit(*_args: Any) -> None:
    """Flush state on exit"""
    try:
        with state.state_lock:
            if state.scan_state_path and state.scan_state:
                persist_state(force=True)
        tracker = getattr(state, "resume_tracker", None)
        if isinstance(tracker, ExactResumeTracker):
            tracker.close()
        flush_db_writes()
    except Exception as e:
        logger.error(f"Error flushing state on exit: {e}")



def open_quality_checker() -> None:
    """Open quality checker window"""
    quality_window = Toplevel(root)
    quality_window.title("სახის ხარისხის შემოწმება")
    quality_window.configure(bg="#0B1120")
    configure_responsive_geometry(quality_window, 900, 920, minimum_width=560, minimum_height=500)
    register_window_theme(quality_window, "quality_checker")
    quality_page, _quality_canvas = create_scrollable_page(
        quality_window, minimum_content_width=540, horizontal=True
    )

    # Header
    header = Frame(quality_page, bg="#0B1120")
    header.pack(pady=15)

    Label(header,
          text="🔬  სახის ხარისხის შემოწმება",
          font=("Segoe UI", 18, "bold"),
          fg="#E8EDF5",
          bg="#0B1120").pack()

    Label(header,
          text="შეამოწმე, გამოდგება თუ არა ფოტო სახის ამოცნობისთვის",
          font=("Segoe UI", 9),
          fg="#7B93B8",
          bg="#0B1120").pack()

    Frame(quality_page, height=1, bg="#1F2D4A").pack(fill=X, padx=20, pady=5)

    content = Frame(quality_page, bg="#0B1120")
    content.pack(pady=20, padx=30, fill=BOTH, expand=True)

    selected_file = StringVar(value="")
    selected_files: List[str] = []

    def verdict_text(score: int, details: Dict[str, str]) -> str:
        if "verdict" in details:
            return str(details["verdict"]).replace("ვერდიქტი:", "").strip()
        if score >= 88:
            return "მიღებულია"
        if score >= 78:
            return "კარგია"
        if score >= 65:
            return "საზღვარზეა"
        return "არ არის რეკომენდებული"

    def score_role(score: int) -> str:
        return "success" if score >= 85 else "warning" if score >= 70 else "danger"

    def score_color(score: int) -> str:
        return get_window_design_colors("quality_checker")[score_role(score)]

    def hide_results() -> None:
        result_frame.pack_forget()
        multi_result_frame.pack_forget()

    def render_single_result(file_path: str, score: int, details: Dict[str, str]) -> None:
        hide_results()
        result_frame.pack(pady=20, fill=BOTH, expand=True)

        roles = getattr(score_label, "_gui_theme_roles", {})
        if isinstance(roles, dict):
            roles["foreground"] = score_role(score)
            score_label._gui_theme_roles = roles  # type: ignore[attr-defined]
        score_label.config(text=f"{score}%", fg=score_color(score))
        selected_result_title.config(text=f"ფოტო: {Path(file_path).name}")

        details_text.delete(1.0, END)
        if "error" in details:
            details_text.insert(END, f"შეცდომა: {details['error']}\n\n", "error")
        else:
            ordered_keys = ["critical", "verdict", "resolution", "face_size", "crop",
                            "position", "sharpness", "brightness", "contrast",
                            "dynamic_range", "shadow_clip", "highlight_clip",
                            "detection", "pose", "recommendation"]
            for key in ordered_keys:
                if key not in details:
                    continue
                value = details[key]
                if key == "critical":
                    details_text.insert(END, f"{value}\n\n", "error")
                elif key in {"verdict", "recommendation"}:
                    details_text.insert(END, f"{value}\n", "recommendation")
                else:
                    details_text.insert(END, f"- {value}\n", "normal")

            for key, value in details.items():
                if key not in ordered_keys:
                    details_text.insert(END, f"- {value}\n", "normal")

        details_text.tag_config("error", foreground="#E05555", font=("Segoe UI", 10, "bold"))
        details_text.tag_config("recommendation", foreground="#4D7CFF", font=("Segoe UI", 11, "bold"))
        details_text.tag_config("normal", foreground="#E8EDF5", font=("Segoe UI", 10))

    def render_multi_results(results: List[Tuple[str, int, Dict[str, str]]]) -> None:
        hide_results()
        multi_result_frame.pack(pady=20, fill=BOTH, expand=True)

        multi_text.delete(1.0, END)

        if not results:
            multi_text.insert(END, "შედეგები ვერ მოიძებნა\n", "bad")
            return

        results_sorted = sorted(results, key=lambda x: x[1], reverse=True)
        best_file, best_score, best_details = results_sorted[0]

        accepted_count = sum(1 for _, score, _ in results_sorted if score >= 85)
        usable_count = sum(1 for _, score, _ in results_sorted if 70 <= score < 85)
        weak_count = sum(1 for _, score, _ in results_sorted if score < 70)

        best_verdict = verdict_text(best_score, best_details)
        best_recommendation = best_details.get("recommendation", "")

        multi_text.insert(END, "საუკეთესო ფოტო\n", "section")
        multi_text.insert(
            END,
            f"{Path(best_file).name} — {best_score}% — {best_verdict}\n",
            "good" if best_score >= 85 else "warn" if best_score >= 70 else "bad"
        )
        if best_recommendation:
            multi_text.insert(END, f"{best_recommendation}\n\n", "recommendation")

        multi_text.insert(END, "ჯამური შეფასება\n", "section")
        multi_text.insert(END, f"მაღალი ხარისხის: {accepted_count}\n", "good")
        multi_text.insert(END, f"საშუალოდ კარგი: {usable_count}\n", "warn")
        multi_text.insert(END, f"სუსტი / დასაწუნი: {weak_count}\n\n", "bad" if weak_count else "normal")

        multi_text.insert(END, "ყველა ფოტო ქულის მიხედვით\n", "section")
        for idx, (file_path, score, details) in enumerate(results_sorted, 1):
            verdict = verdict_text(score, details)
            recommendation = details.get("recommendation", "")
            critical = details.get("critical")
            line_tag = "good" if score >= 85 else "warn" if score >= 70 else "bad"

            multi_text.insert(
                END,
                f"{idx}. {Path(file_path).name} — {score}% — {verdict}\n",
                line_tag
            )
            if critical:
                multi_text.insert(END, f"   {critical}\n", "bad")
            elif recommendation:
                multi_text.insert(END, f"   {recommendation}\n", "normal")
            multi_text.insert(END, "\n", "normal")

        multi_text.tag_config("section", foreground="#E8EDF5", font=("Segoe UI", 11, "bold"))
        multi_text.tag_config("good", foreground="#2ECC7A", font=("Segoe UI", 10, "bold"))
        multi_text.tag_config("warn", foreground="#F5A623", font=("Segoe UI", 10, "bold"))
        multi_text.tag_config("bad", foreground="#E05555", font=("Segoe UI", 10, "bold"))
        multi_text.tag_config("recommendation", foreground="#4D7CFF", font=("Segoe UI", 10, "bold"))
        multi_text.tag_config("normal", foreground="#E8EDF5", font=("Segoe UI", 10))

    # Select section
    select_frame = Frame(content, bg="#0B1120")
    select_frame.pack(pady=10, fill=X)

    single_select_frame = Frame(select_frame, bg="#1A2540", bd=0)
    single_select_frame.pack(fill=X, pady=(0, 8))

    def select_photo() -> None:
        file = filedialog.askopenfilename(
            title="აირჩიე ფოტო",
            filetypes=[("სურათები", "*.jpg *.png *.jpeg *.bmp *.webp")]
        )
        if file:
            selected_file.set(file)
            selected_files.clear()
            selection_label.config(text=f"არჩეულია 1 ფოტო: {Path(file).name}")
            hide_results()

    select_btn = Button(single_select_frame,
                        text="აირჩიე ერთი ფოტო",
                        command=select_photo,
                        font=("Segoe UI", 11, "bold"),
                        bg="#1A2540",
                        fg="#4D7CFF",
                        activebackground="#4D7CFF",
                        activeforeground="#0B1120",
                        relief=FLAT,
                        bd=0,
                        padx=25,
                        pady=15,
                        cursor="hand2")
    select_btn.pack(fill=X)

    multi_select_frame = Frame(select_frame, bg="#1A2540", bd=0)
    multi_select_frame.pack(fill=X)

    def select_multiple_photos() -> None:
        files = filedialog.askopenfilenames(
            title="აირჩიე რამდენიმე ფოტო",
            filetypes=[("სურათები", "*.jpg *.png *.jpeg *.bmp *.webp")]
        )
        if files:
            selected_file.set("")
            selected_files.clear()
            selected_files.extend(list(files))
            preview = ", ".join(Path(f).name for f in selected_files[:3])
            if len(selected_files) > 3:
                preview += f" ... (+{len(selected_files) - 3})"
            selection_label.config(text=f"არჩეულია {len(selected_files)} ფოტო: {preview}")
            hide_results()

    select_multi_btn = Button(multi_select_frame,
                              text="აირჩიე რამდენიმე ფოტო",
                              command=select_multiple_photos,
                              font=("Segoe UI", 11, "bold"),
                              bg="#1A2540",
                              fg="#4D7CFF",
                              activebackground="#4D7CFF",
                              activeforeground="#0B1120",
                              relief=FLAT,
                              bd=0,
                              padx=25,
                              pady=15,
                              cursor="hand2")
    select_multi_btn.pack(fill=X)

    selection_label = Label(content, text="ფოტოები არ არის არჩეული",
                            font=("Segoe UI", 10), fg="#7B93B8", bg="#0B1120",
                            wraplength=730, justify=LEFT)
    selection_label.pack(pady=10, anchor="w")

    check_frame = Frame(content, bg="#0B1120")
    check_frame.pack(pady=10)

    # Single result area
    result_frame = Frame(content, bg="#1A2540", bd=0)

    Label(result_frame,
          text="ხარისხის ქულა:",
          font=("Segoe UI", 12, "bold"),
          fg="#4D7CFF",
          bg="#1A2540").pack(pady=(12, 8))

    score_label = Label(result_frame,
                        text="0%",
                        font=("Segoe UI", 48, "bold"),
                        fg="#2ECC7A",
                        bg="#1A2540")
    score_label.pack()

    selected_result_title = Label(result_frame,
                                  text="",
                                  font=("Segoe UI", 10, "bold"),
                                  fg="#7B93B8",
                                  bg="#1A2540")
    selected_result_title.pack(pady=(0, 10))

    Label(result_frame,
          text="დეტალები:",
          font=("Segoe UI", 11, "bold"),
          fg="#4D7CFF",
          bg="#1A2540").pack(pady=(10, 5))

    details_frame = Frame(result_frame, bg="#0B1120")
    details_frame.pack(pady=10, padx=15, fill=BOTH, expand=True)

    details_scroll = Scrollbar(details_frame)
    details_scroll.pack(side=RIGHT, fill=Y)

    details_text = Text(details_frame,
                        height=14,
                        width=78,
                        bg="#0B1120",
                        fg="#7B93B8",
                        font=("Segoe UI", 10),
                        relief=FLAT,
                        bd=0,
                        wrap=WORD,
                        yscrollcommand=details_scroll.set)
    details_text.pack(fill=BOTH, expand=True)
    details_scroll.config(command=details_text.yview)

    # Multi result area
    multi_result_frame = Frame(content, bg="#1A2540", bd=0)

    Label(multi_result_frame,
          text="რამდენიმე ფოტოს შეფასება",
          font=("Segoe UI", 12, "bold"),
          fg="#4D7CFF",
          bg="#1A2540").pack(pady=(12, 8))

    multi_details_frame = Frame(multi_result_frame, bg="#0B1120")
    multi_details_frame.pack(pady=10, padx=15, fill=BOTH, expand=True)

    multi_scroll = Scrollbar(multi_details_frame)
    multi_scroll.pack(side=RIGHT, fill=Y)

    multi_text = Text(multi_details_frame,
                      height=20,
                      width=78,
                      bg="#0B1120",
                      fg="#7B93B8",
                      font=("Segoe UI", 10),
                      relief=FLAT,
                      bd=0,
                      wrap=WORD,
                      yscrollcommand=multi_scroll.set)
    multi_text.pack(fill=BOTH, expand=True)
    multi_scroll.config(command=multi_text.yview)

    def check_quality() -> None:
        if not selected_file.get():
            messagebox.showerror("შეცდომა", "გთხოვ, აირჩიე ფოტო!")
            return

        score, details = check_face_quality(selected_file.get())
        render_single_result(selected_file.get(), score, details)

    def check_multiple_quality() -> None:
        if not selected_files:
            messagebox.showerror("შეცდომა", "გთხოვ, აირჩიე რამდენიმე ფოტო!")
            return

        results: List[Tuple[str, int, Dict[str, str]]] = []
        for file_path in selected_files:
            score, details = check_face_quality(file_path)
            results.append((file_path, score, details))

        render_multi_results(results)

    check_btn = Button(check_frame,
                       text="🔬  ფოტოს შემოწმება",
                       command=check_quality,
                       font=("Segoe UI", 13, "bold"),
                       bg="#4D7CFF",
                       fg="white",
                       activebackground="#3A68E8",
                       activeforeground="white",
                       relief=FLAT,
                       bd=0,
                       padx=25,
                       pady=12,
                       cursor="hand2")
    check_btn.pack(side=LEFT, padx=6)

    check_multi_btn = Button(check_frame,
                             text="რამდენიმე ფოტოს შედარება",
                             command=check_multiple_quality,
                             font=("Segoe UI", 13, "bold"),
                             bg="#1A2540",
                             fg="#F5A623",
                             activebackground="#F5A623",
                             activeforeground="#0B1120",
                             relief=FLAT,
                             bd=0,
                             padx=25,
                             pady=12,
                             cursor="hand2")
    check_multi_btn.pack(side=LEFT, padx=6)


# Register cleanup handlers
atexit.register(flush_state_on_exit)
for _sig_name in ("SIGINT", "SIGTERM"):
    if hasattr(signal, _sig_name):
        try:
            signal.signal(getattr(signal, _sig_name), flush_state_on_exit)
        except (ValueError, OSError) as e:
            logger.warning(f"Could not register signal handler for {_sig_name}: {e}")




def unique_destination_path(path: Path) -> Path:
    """Return unique destination path if file already exists."""
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    parent = path.parent
    counter = 1
    while True:
        candidate = parent / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


_ACTIVE_ROUTER_CONTROLLER: Dict[str, Any] = {}
_APP_EXIT_AFTER_ROUTER = False


def open_multi_person_router() -> None:
    """Open one safe multi-person routing window (up to 20 people)."""
    existing_window = _ACTIVE_ROUTER_CONTROLLER.get("window")
    try:
        if existing_window is not None and existing_window.winfo_exists():
            existing_window.deiconify()
            existing_window.lift()
            existing_window.focus_force()
            return
    except Exception:
        _ACTIVE_ROUTER_CONTROLLER.clear()
    router_window = Toplevel(root)
    router_window.title("1-20 კაციანი გადანაწილება")
    router_window.configure(bg="#0B1120")
    register_window_theme(router_window, "router")

    # Responsive router window + full-page scrolling
    rw, rh = configure_responsive_geometry(
        router_window, 1320, 940, minimum_width=720, minimum_height=560,
        width_ratio=0.98, height_ratio=0.94,
    )
    router_page, _router_page_canvas = create_scrollable_page(
        router_window, minimum_content_width=900, horizontal=True
    )

    # ---- State ----
    slot_states: List[Dict[str, Any]] = []
    router_running = False
    router_threads: List[threading.Thread] = []
    router_stop_event = threading.Event()
    router_pause_event = threading.Event()
    router_duplicate_index = None
    router_resume_tracker: Optional[ExactResumeTracker] = None
    router_queue: queue.Queue = queue.Queue()
    router_lock = threading.Lock()
    router_state_lock = threading.RLock()
    router_state_path: Optional[Path] = None
    router_state_data: Dict[str, Any] = {}
    router_close_pending = False
    router_stats: Dict[str, Any] = {
        "matched": 0, "unmatched": 0, "review": 0, "duplicates": 0, "errors": 0,
        "resumed": 0, "per_slot": {}, "done": 0, "total": 0,
        "start_time": 0.0, "unmatched_dir": "", "state_file": "",
        "resume_policy": "source_folder_only",
    }

    # ---- Top fixed area (header + controls) ----
    top_fixed = Frame(router_page, bg="#0B1120")
    top_fixed.pack(fill=X)

    # Header
    Label(top_fixed, text="👥  1-20 კაციანი გადანაწილება",
          font=("Segoe UI", 16, "bold"), fg="#E8EDF5", bg="#0B1120").pack(pady=(10, 2))
    Label(top_fixed, text="თითო ბლოკში: ერთი ადამიანის რამდენიმე reference ფოტო + შედეგის საქაღალდე. შემდეგ — საერთო source folder.",
          font=("Segoe UI", 9), fg="#7B93B8", bg="#0B1120").pack()
    Frame(top_fixed, height=1, bg="#1F2D4A").pack(fill=X, padx=16, pady=(6, 4))

    # Source row
    source_var = StringVar(value="")
    people_count_var = StringVar(value="4")
    worker_count_var = StringVar(value="4")

    src_row = Frame(top_fixed, bg="#0B1120")
    src_row.pack(fill=X, padx=16, pady=(2, 0))

    def pick_router_source() -> None:
        folder = filedialog.askdirectory(title="საერთო source folder")
        if folder:
            source_var.set(folder)
            src_lbl.config(text=f"Source: {folder}")

    Button(src_row, text="საერთო Source Folder", command=pick_router_source,
           font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#4D7CFF",
           activebackground="#4D7CFF", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=16, pady=8, cursor="hand2").pack(side=LEFT, padx=(0, 8))

    src_lbl = Label(src_row, text="არ არის არჩეული", font=("Segoe UI", 9),
                    fg="#7B93B8", bg="#0B1120", anchor="w")
    src_lbl.pack(side=LEFT, fill=X, expand=True)

    Label(top_fixed,
          text="⚠  ვერ დახარისხებული ფოტოები source folder-ში დარჩება",
          font=("Segoe UI", 8), fg="#F5A623", bg="#0B1120").pack(anchor="w", padx=16, pady=(2, 4))

    # Auto-assign reference folder row (folder with one subfolder per person)
    auto_ref_row = Frame(top_fixed, bg="#0B1120")
    auto_ref_row.pack(fill=X, padx=16, pady=(0, 6))

    Button(auto_ref_row, text="📁  Reference საქაღალდე (ავტომატური განაწილება)",
           command=lambda: _auto_assign_refs_from_folder(),
           font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#2ECC7A",
           activebackground="#2ECC7A", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=16, pady=8, cursor="hand2").pack(side=LEFT, padx=(0, 8))

    Label(auto_ref_row,
          text="აირჩიე საქაღალდე — თუ შიგნით ქვესაქაღალდეებია (თითო = ერთი ადამიანი), გამოიყენება ისინი; "
               "თუ ფოტოები პირდაპირ საქაღალდეშია, ისინი ჯგუფდება ფაილის სახელის მიხედვით "
               "(მაგ. giorgi_1.jpg, giorgi_2.jpg → ერთი ბლოკი).",
          font=("Segoe UI", 8), fg="#7B93B8", bg="#0B1120", anchor="w",
          wraplength=760, justify=LEFT).pack(side=LEFT, fill=X, expand=True)

    # Settings row (people count, threshold, workers)
    settings_row = Frame(top_fixed, bg="#0B1120")
    settings_row.pack(fill=X, padx=16, pady=(0, 4))

    # People count
    pc_frame = Frame(settings_row, bg="#1A2540", padx=10, pady=8)
    pc_frame.pack(side=LEFT, fill=Y, padx=(0, 6))
    Label(pc_frame, text="ბლოკების რაოდენობა", font=("Segoe UI", 9, "bold"),
          fg="#E8EDF5", bg="#1A2540").pack()
    _pc_opts = [str(i) for i in range(1, 21)]
    pc_menu = OptionMenu(pc_frame, people_count_var, *_pc_opts)
    pc_menu.config(font=("Segoe UI", 10, "bold"), bg="#0B1120", fg="#4D7CFF",
                   activebackground="#4D7CFF", activeforeground="#0B1120",
                   relief=FLAT, bd=0, highlightthickness=0, width=4, cursor="hand2")
    pc_menu["menu"].config(font=("Segoe UI", 10), bg="#1A2540", fg="#4D7CFF",
                           activebackground="#4D7CFF", activeforeground="#0B1120", bd=0)
    pc_menu.pack(pady=(4, 0))

    # Threshold slider
    thr_frame = Frame(settings_row, bg="#1A2540", padx=10, pady=8)
    thr_frame.pack(side=LEFT, fill=Y, padx=(0, 6))
    Label(thr_frame, text="დამთხვევის ზღვარი", font=("Segoe UI", 9, "bold"),
          fg="#E8EDF5", bg="#1A2540").pack()
    router_slider = Scale(thr_frame, from_=config.threshold_min, to=config.threshold_max,
                          orient=HORIZONTAL, length=240, bg="#131C2E", fg="#7B93B8",
                          troughcolor="#1A2540", highlightthickness=0,
                          activebackground="#4D7CFF", font=("Segoe UI", 9))
    router_slider.set(max(config.threshold_default, 40))
    router_slider.pack(pady=(4, 0))
    Button(thr_frame, text="🎯 Auto", command=lambda: _auto_calibrate_router(),
           font=("Segoe UI", 8, "bold"), bg="#0B1120", fg="#4D7CFF",
           activebackground="#4D7CFF", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=10, pady=4, cursor="hand2").pack(pady=(3, 0))

    # Workers
    wkr_frame = Frame(settings_row, bg="#1A2540", padx=10, pady=8)
    wkr_frame.pack(side=LEFT, fill=Y)
    Label(wkr_frame, text="Worker-ები (1-20)", font=("Segoe UI", 9, "bold"),
          fg="#E8EDF5", bg="#1A2540").pack()
    _wkr_opts = [str(i) for i in range(1, 21)]
    wkr_menu = OptionMenu(wkr_frame, worker_count_var, *_wkr_opts)
    wkr_menu.config(font=("Segoe UI", 10, "bold"), bg="#0B1120", fg="#4D7CFF",
                    activebackground="#4D7CFF", activeforeground="#0B1120",
                    relief=FLAT, bd=0, highlightthickness=0, width=4, cursor="hand2")
    wkr_menu["menu"].config(font=("Segoe UI", 10), bg="#1A2540", fg="#4D7CFF",
                            activebackground="#4D7CFF", activeforeground="#0B1120", bd=0)
    wkr_menu.pack(pady=(4, 0))

    Frame(top_fixed, height=1, bg="#1A2540").pack(fill=X, padx=16, pady=(4, 0))

    # ---- Scrollable slots area (middle, expands) ----
    slots_outer = Frame(router_page, bg="#0B1120", height=max(300, int(rh * 0.38)))
    slots_outer.pack(fill=X, padx=16, pady=4)
    slots_outer.pack_propagate(False)

    slots_canvas = Canvas(slots_outer, bg="#0B1120", highlightthickness=0)
    slots_sb = Scrollbar(slots_outer, orient=VERTICAL, command=slots_canvas.yview)
    slots_canvas.configure(yscrollcommand=slots_sb.set)
    slots_sb.pack(side=RIGHT, fill=Y)
    slots_canvas.pack(side=LEFT, fill=BOTH, expand=True)

    slots_inner = Frame(slots_canvas, bg="#0B1120")
    _slots_win = slots_canvas.create_window((0, 0), window=slots_inner, anchor="nw")

    def _slots_configure(event=None):
        slots_canvas.configure(scrollregion=slots_canvas.bbox("all"))
    def _slots_width(event=None):
        slots_canvas.itemconfig(_slots_win, width=slots_canvas.winfo_width())

    slots_inner.bind("<Configure>", _slots_configure)
    slots_canvas.bind("<Configure>", _slots_width)

    def _slots_mw(event):
        slots_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        return "break"
    slots_canvas.bind("<MouseWheel>", _slots_mw)
    slots_inner.bind("<MouseWheel>", _slots_mw)

    # ---- Bottom fixed area (progress + summary + buttons) ----
    bottom_fixed = Frame(router_page, bg="#0B1120")
    bottom_fixed.pack(fill=X, padx=16, pady=(4, 8))

    # Progress label
    progress_label_router = Label(bottom_fixed, text="მზადაა გადანაწილებისთვის...",
                                  font=("Segoe UI", 9), fg="#4D7CFF", bg="#0B1120",
                                  anchor="w", justify=LEFT, wraplength=1100)
    progress_label_router.pack(fill=X, pady=(0, 4))

    # Progress bar
    progress_router = Progressbar(bottom_fixed, mode='determinate',
                                  style="Custom.Horizontal.TProgressbar")
    progress_router.pack(fill=X, pady=(0, 6))

    # Summary text
    summary_text = Text(bottom_fixed, height=5, bg="#1A2540", fg="#7B93B8",
                        font=("Segoe UI", 9), relief=FLAT, bd=0, wrap=WORD, state=DISABLED)
    summary_text.pack(fill=X, pady=(0, 6))
    summary_text.config(state=NORMAL)
    summary_text.insert(END, "აქ გამოჩნდება თითო ბლოკზე რამდენი ფოტო გადავიდა.\n")
    summary_text.config(state=DISABLED)

    # Router log panel
    _rlog_hdr = Frame(bottom_fixed, bg="#0B1120")
    _rlog_hdr.pack(fill=X)
    Label(_rlog_hdr, text="ლოგი", font=("Segoe UI", 8, "bold"),
          fg="#4D7CFF", bg="#0B1120").pack(side=LEFT)

    def _clear_rlog() -> None:
        router_log_text.config(state=NORMAL)
        router_log_text.delete("1.0", END)
        router_log_text.config(state=DISABLED)

    Button(_rlog_hdr, text="გასუფთავება", command=_clear_rlog,
           font=("Segoe UI", 8), bg="#1A2540", fg="#7B93B8",
           activebackground="#4D7CFF", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=6, pady=2, cursor="hand2").pack(side=RIGHT)

    _rlog_inner = Frame(bottom_fixed, bg="#0B1120")
    _rlog_inner.pack(fill=X, pady=(2, 6))
    _rlog_sb = Scrollbar(_rlog_inner)
    _rlog_sb.pack(side=RIGHT, fill=Y)
    router_log_text = Text(_rlog_inner, height=3, bg="#131C2E", fg="#7B93B8",
                           font=("Courier", 8), relief=FLAT, bd=0, wrap=WORD,
                           state=DISABLED, yscrollcommand=_rlog_sb.set)
    router_log_text.pack(side=LEFT, fill=X, expand=True)
    _rlog_sb.config(command=router_log_text.yview)
    router_log_text.tag_config("info",    foreground="#4D7CFF")
    router_log_text.tag_config("error",   foreground="#E05555")
    router_log_text.tag_config("warning", foreground="#F5A623")
    router_log_text.tag_config("success", foreground="#2ECC7A")

    _rlog_auto = True

    def _rlog_sb_cb(*args):
        nonlocal _rlog_auto
        _rlog_sb.set(*args)
        try:
            _rlog_auto = float(args[1]) >= 0.999
        except Exception:
            pass
    router_log_text.config(yscrollcommand=_rlog_sb_cb)

    def _append_rlog(msg: str, level: str = "info") -> None:
        router_log_text.config(state=NORMAL)
        ts = datetime.now().strftime("%H:%M:%S")
        router_log_text.insert(END, f"[{ts}] {msg}\n", level)
        router_log_text.config(state=DISABLED)
        if _rlog_auto:
            router_log_text.see(END)

    class _RouterLogHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            try:
                msg = self.format(record)
                lvl = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info"
                try:
                    post_ui(lambda m=msg, l=lvl: _append_rlog(m, l))
                except Exception:
                    pass
            except Exception:
                pass

    _rlog_hdl = _RouterLogHandler()
    _rlog_hdl.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
    logger.addHandler(_rlog_hdl)

    # Action buttons row: Start | Stop | History
    action_row = Frame(bottom_fixed, bg="#0B1120")
    action_row.pack(fill=X, pady=(2, 0))

    start_btn_router = Button(action_row, text="▶  გადანაწილების დაწყება",
                              font=("Segoe UI", 13, "bold"), bg="#4D7CFF", fg="white",
                              activebackground="#3A68E8", activeforeground="white",
                              relief=FLAT, bd=0, padx=22, pady=12, cursor="hand2")
    start_btn_router.pack(side=LEFT, padx=(0, 8))

    stop_btn_router = Button(action_row, text="გაჩერება",
                             font=("Segoe UI", 13, "bold"), bg="#1A2540", fg="#F5A623",
                             activebackground="#F5A623", activeforeground="#0B1120",
                             relief=FLAT, bd=0, padx=22, pady=12, cursor="hand2", state=DISABLED)
    stop_btn_router.pack(side=LEFT, padx=(0, 8))

    pause_btn_router = Button(action_row, text="⏸ პაუზა",
                              font=("Segoe UI", 11, "bold"), bg="#1A2540", fg="#7B93B8",
                              activebackground="#4D7CFF", activeforeground="white",
                              relief=FLAT, bd=0, padx=14, pady=12, cursor="hand2", state=DISABLED)
    pause_btn_router.pack(side=LEFT, padx=(0, 8))

    def open_router_history() -> None:
        _open_history_window_router(
            router_window, router_slider, people_count_var, worker_count_var,
            src_lbl, source_var, slot_states,
            start_callback=start_router,
        )

    Button(action_row, text="📋 ისტორია",
           command=open_router_history,
           font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#4D7CFF",
           activebackground="#4D7CFF", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=12, pady=12, cursor="hand2").pack(side=LEFT, padx=(0, 6))
    Button(action_row, text="🔍 Review", command=open_review_queue,
           font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#4D7CFF",
           activebackground="#4D7CFF", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=12, pady=12, cursor="hand2").pack(side=LEFT, padx=(0, 6))

    # ---- Slot cards ----
    def _update_slot_count() -> None:
        active_count = int(people_count_var.get())
        for idx, slot in enumerate(slot_states, start=1):
            if idx <= active_count:
                slot["frame"].grid()
                slot["active"] = True
            else:
                slot["frame"].grid_remove()
                slot["active"] = False

    def _choose_slot_ref(slot: Dict[str, Any]) -> None:
        files = filedialog.askopenfilenames(
            title=f"Reference ფოტოები #{slot['index']} — აირჩიე ერთი ან რამდენიმე",
            filetypes=[("სურათები", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff *.heic *.heif *.avif *.dng *.cr2 *.nef *.arw")])
        if files:
            refs = list(dict.fromkeys(files))
            slot["ref_paths"] = refs
            slot["ref_path"] = refs[0]
            names = ", ".join(Path(fp).name for fp in refs[:3])
            if len(refs) > 3:
                names += f" ... (+{len(refs)-3})"
            slot["ref_label"].config(text=f"{len(refs)} ფოტო: {names}")

    def _choose_slot_out(slot: Dict[str, Any]) -> None:
        fp = filedialog.askdirectory(title=f"შედეგის საქაღალდე #{slot['index']}")
        if fp:
            slot["out_folder"] = fp
            slot["out_label"].config(text=fp)

    def _extract_person_key(stem: str) -> str:
        """
        ფაილის სახელიდან ცდილობს ამოიღოს „წმინდა" პიროვნების სახელი და მოაცილოს
        ბოლოში მდგარი ნომრები/სპეც სიმბოლოები.
        მაგ: 'giorgi_02' -> 'giorgi', 'nino (3)' -> 'nino', 'luka-1' -> 'luka'
        """
        s = stem.strip()
        prev = None
        while prev != s:
            prev = s
            s = re.sub(r'[\s_\-]*\(?\d+\)?\s*$', '', s).strip(' _-')
        return s if s else stem.strip()

    def _auto_assign_refs_from_folder() -> None:
        """
        აირჩევს Reference საქაღალდეს და ავტომატურად ანაწილებს ფოტოებს ბლოკებში (1-20):
          1) თუ საქაღალდეში არის ქვესაქაღალდეები — თითო ქვესაქაღალდე ჩაითვლება
             ერთ ადამიანად (ქვესაქაღალდის სახელი = ადამიანის სახელი).
          2) თუ ფოტოები პირდაპირ ამ საქაღალდეშია (ქვესაქაღალდეების გარეშე) —
             ფოტოები ჯგუფდება ფაილის სახელის მიხედვით (მაგ. giorgi_1.jpg,
             giorgi_2.jpg, nino_1.jpg → 2 ცალკე ჯგუფი: giorgi და nino).
        """
        base_folder = filedialog.askdirectory(
            title="Reference საქაღალდე (ქვესაქაღალდეებით ან პირდაპირ ფოტოებით)")
        if not base_folder:
            return

        base_path = Path(base_folder)
        exts = tuple(ext.lower() for ext in config.extensions)

        try:
            subdirs = sorted(
                [d for d in base_path.iterdir() if d.is_dir() and not d.name.startswith(".")],
                key=lambda p: p.name.lower(),
            )
        except Exception as exc:
            messagebox.showerror("შეცდომა", f"საქაღალდის წაკითხვა ვერ მოხერხდა: {exc}", parent=router_window)
            return

        groups: List[Tuple[str, List[str]]] = []

        # რეჟიმი 1: ქვესაქაღალდეები = ცალკე ადამიანები
        for d in subdirs:
            try:
                imgs = sorted(
                    str(p) for p in d.iterdir()
                    if p.is_file() and p.suffix.lower() in exts
                )
            except Exception:
                imgs = []
            if imgs:
                groups.append((d.name, imgs))

        # რეჟიმი 2: ქვესაქაღალდეები ვერ მოიძებნა/ცარიელია — ფოტოები პირდაპირ საქაღალდეშია,
        # დაჯგუფდეს ფაილების სახელების მიხედვით
        if not groups:
            try:
                files = sorted(
                    [p for p in base_path.iterdir() if p.is_file() and p.suffix.lower() in exts],
                    key=lambda p: p.name.lower(),
                )
            except Exception as exc:
                messagebox.showerror("შეცდომა", f"საქაღალდის წაკითხვა ვერ მოხერხდა: {exc}", parent=router_window)
                return

            if not files:
                messagebox.showerror(
                    "შეცდომა",
                    "არჩეულ საქაღალდეში სურათები ვერ მოიძებნა.",
                    parent=router_window,
                )
                return

            buckets: Dict[str, List[str]] = {}
            display: Dict[str, str] = {}
            for p in files:
                key_raw = _extract_person_key(p.stem)
                key = key_raw.lower()
                buckets.setdefault(key, []).append(str(p))
                display.setdefault(key, key_raw)

            groups = [(display[k], v) for k, v in sorted(buckets.items(), key=lambda kv: kv[0])]

        if not groups:
            messagebox.showerror("შეცდომა", "ვერცერთი ადამიანის ფოტო ვერ დაჯგუფდა.", parent=router_window)
            return

        if len(groups) > 20:
            messagebox.showwarning(
                "გაფრთხილება",
                f"ნაპოვნია {len(groups)} ადამიანი, მაგრამ მაქსიმუმ 20 ბლოკია შესაძლებელი — "
                f"პირველი 20 (ანბანური რიგით) გამოიყენება.",
                parent=router_window,
            )
            groups = groups[:20]

        filled = 0
        for i, (name, images) in enumerate(groups):
            slot = slot_states[i]
            slot["ref_paths"] = images
            slot["ref_path"] = images[0]
            slot["display_name"] = name
            # reset any previously computed embeddings so they get recalculated on start
            slot["ref_emb"] = None
            slot["ref_embs"] = []
            slot["ref_matrix"] = None
            slot["centroid"] = None

            names_preview = ", ".join(Path(fp).name for fp in images[:3])
            if len(images) > 3:
                names_preview += f" ... (+{len(images) - 3})"
            slot["ref_label"].config(text=f"{len(images)} ფოტო: {names_preview}")
            if slot.get("title_label") is not None:
                slot["title_label"].config(text=f"ბლოკი #{slot['index']} — {name}")
            filled += 1

        # any slots beyond the ones we filled keep their previous data untouched
        people_count_var.set(str(max(filled, int(people_count_var.get() or 1))))
        _update_slot_count()

        summary = f"✅ ავტომატურად ჩაიტვირთა {filled} ადამიანის reference საქაღალდიდან „{base_path.name}“."
        _append_rlog(summary, "success")

        messagebox.showinfo(
            "დასრულდა",
            f"ავტომატურად შეივსო {filled} ბლოკი:\n" +
            "\n".join(f"#{i + 1} — {name} ({len(imgs)} ფოტო)" for i, (name, imgs) in enumerate(groups)),
            parent=router_window,
        )

    def _create_slot_card(index: int) -> None:
        row = (index - 1) // 2
        col = (index - 1) % 2
        card = Frame(slots_inner, bg="#1A2540", padx=10, pady=10)
        card.grid(row=row, column=col, sticky="nsew", padx=6, pady=6)

        title_lbl = Label(card, text=f"ბლოკი #{index}", font=("Segoe UI", 11, "bold"),
              fg="#4D7CFF", bg="#1A2540")
        title_lbl.pack(anchor="w")

        Button(card, text="Reference ფოტოები (1 ან მეტი)",
               command=lambda i=index-1: _choose_slot_ref(slot_states[i]),
               font=("Segoe UI", 9, "bold"), bg="#0B1120", fg="#4D7CFF",
               activebackground="#4D7CFF", activeforeground="#0B1120",
               relief=FLAT, bd=0, padx=14, pady=8, cursor="hand2").pack(fill=X, pady=(8, 4))

        ref_lbl = Label(card, text="არ არის არჩეული", font=("Segoe UI", 8),
                        fg="#7B93B8", bg="#1A2540", anchor="w", wraplength=440)
        ref_lbl.pack(fill=X)

        Button(card, text="შედეგის საქაღალდე",
               command=lambda i=index-1: _choose_slot_out(slot_states[i]),
               font=("Segoe UI", 9, "bold"), bg="#0B1120", fg="#F5A623",
               activebackground="#F5A623", activeforeground="#0B1120",
               relief=FLAT, bd=0, padx=14, pady=8, cursor="hand2").pack(fill=X, pady=(10, 4))

        out_lbl = Label(card, text="არ არის არჩეული", font=("Segoe UI", 8),
                        fg="#7B93B8", bg="#1A2540", anchor="w", wraplength=440)
        out_lbl.pack(fill=X)

        slot_states.append({
            "index": index, "frame": card,
            "ref_path": "", "ref_paths": [], "out_folder": "",
            "ref_emb": None, "ref_embs": [], "ref_matrix": None, "centroid": None,
            "ref_label": ref_lbl, "out_label": out_lbl, "title_label": title_lbl,
            "display_name": "",
            "active": index <= 4,
        })

    slots_inner.grid_columnconfigure(0, weight=1)
    slots_inner.grid_columnconfigure(1, weight=1)

    for i in range(1, 21):
        _create_slot_card(i)

    people_count_var.trace_add("write", lambda *_: _update_slot_count())
    _update_slot_count()

    # ---- Helper functions ----
    def _auto_calibrate_router() -> None:
        active = [slot for slot in slot_states if slot.get("active")]
        positives: List[float] = []
        matrices: List[np.ndarray] = []
        try:
            ensure_face_engine(model_profile_var.get())
            for slot in active:
                refs = [ref for ref in (slot.get("ref_paths") or [slot.get("ref_path")])
                        if isinstance(ref, (str, Path))]
                if not refs:
                    continue
                embs: List[np.ndarray] = []
                for ref in refs:
                    try:
                        embs.append(get_emb(ref))
                    except Exception:
                        continue
                if not embs:
                    continue
                matrix = np.vstack(embs).astype(np.float32)
                matrices.append(matrix)
                if len(embs) >= 2:
                    sims = matrix @ matrix.T
                    values = sims[np.triu_indices(len(embs), k=1)]
                    positives.extend(float(value) for value in values if float(value) > 0.20)
            if not matrices:
                messagebox.showerror("Auto Threshold", "ჯერ აქტიურ ბლოკებში reference ფოტოები აირჩიე", parent=router_window)
                return
            negatives: List[float] = []
            for first in range(len(matrices)):
                for second in range(first + 1, len(matrices)):
                    negatives.append(float(np.max(matrices[first] @ matrices[second].T)))
            if positives and negatives:
                positive_low = float(np.percentile(positives, 10))
                negative_high = float(np.percentile(negatives, 95))
                balanced = float(np.clip(
                    (positive_low + negative_high) / 2.0 if positive_low > negative_high
                    else max(negative_high + 0.045, np.median(positives) - 0.10),
                    0.42, 0.60,
                ))
                reason = f"შიდა P10={positive_low:.3f}, სხვა პირის P95={negative_high:.3f}"
            elif negatives:
                negative_high = float(np.percentile(negatives, 95))
                balanced = float(np.clip(negative_high + 0.07, 0.42, 0.60))
                reason = f"სხვა პირის P95={negative_high:.3f}"
            elif positives:
                positive_low = float(np.percentile(positives, 10))
                balanced = float(np.clip(positive_low - 0.09, 0.38, 0.56))
                reason = f"შიდა P10={positive_low:.3f}"
            else:
                balanced = 0.47
                reason = "მონაცემი მცირეა"
            router_slider.set(int(round(balanced * 100)))
            messagebox.showinfo(
                "Auto Threshold",
                f"რეკომენდებული დაბალანსებული ზღვარი: {balanced:.2f}\n"
                f"ფართო: {max(0.32, balanced-0.05):.2f} | მკაცრი: {min(0.62, balanced+0.06):.2f}\n{reason}",
                parent=router_window)
        except Exception as exc:
            messagebox.showerror("Auto Threshold", str(exc), parent=router_window)

    def format_router_duration(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h > 0: return f"{h}ს {m}წ {s}წმ"
        if m > 0: return f"{m}წ {s}წმ"
        return f"{s}წმ"

    def get_router_time_stats() -> Tuple[float, float, float]:
        total = max(0, int(router_stats.get("total", 0)))
        done = max(0, int(router_stats.get("done", 0)))
        resumed = max(0, int(router_stats.get("resumed", 0)))
        st = float(router_stats.get("start_time", 0.0) or 0.0)
        elapsed = max(0.0, time.time() - st) if st > 0 else 0.0
        processed = max(0, done - resumed)
        remaining = max(0, total - done)
        avg = (elapsed / processed) if processed > 0 else 0.0
        eta = remaining * avg if avg > 0 else 0.0
        speed = (processed / elapsed) if elapsed > 0 and processed > 0 else 0.0
        return elapsed, eta, speed

    def update_summary_box() -> None:
        summary_text.config(state=NORMAL)
        summary_text.delete("1.0", END)
        total = int(router_stats.get("total", 0))
        done = int(router_stats.get("done", 0))
        matched = int(router_stats.get("matched", 0))
        unmatched = int(router_stats.get("unmatched", 0))
        errors = int(router_stats.get("errors", 0))
        review = int(router_stats.get("review", 0))
        duplicates = int(router_stats.get("duplicates", 0))
        elapsed, eta, speed = get_router_time_stats()
        eta_txt = format_router_duration(eta) if speed > 0 else "ითვლება..."
        pct = (done / total * 100) if total > 0 else 0.0
        remaining_cnt = max(0, total - done)
        summary_text.insert(END, f"დამუშავებული: {done}/{total} ({pct:.1f}%) | დარჩენილი: {remaining_cnt} ({100-pct:.1f}%)\n")
        summary_text.insert(END, f"განაწილებული: {matched} | Review: {review} | დუბლიკატი: {duplicates} | ადგილზე დარჩა: {unmatched} | შეცდომა: {errors}\n")
        summary_text.insert(END, f"გასული: {format_router_duration(elapsed)} | დარჩენილი დრო: {eta_txt} | სიჩქარე: {speed:.2f} ფოტო/წმ\n")
        for idx in sorted(router_stats.get("per_slot", {}).keys()):
            summary_text.insert(END, f"  ბლოკი #{idx}: {router_stats['per_slot'][idx]} ფოტო\n")
        summary_text.config(state=DISABLED)

    def safe_set_progress(text_val: str, prog_val: int) -> None:
        def _apply():
            progress_label_router.config(text=text_val)
            progress_router['value'] = prog_val
            update_summary_box()
        post_ui(_apply)

    def validate_router_paths(active_slots: List[Dict[str, Any]], source_dir: Path) -> Optional[str]:
        all_outputs: List[Path] = []
        for slot in active_slots:
            try:
                out_path = Path(slot["out_folder"]).resolve()
            except Exception:
                return f"ბლოკი #{slot['index']} - შედეგის საქაღალდე არასწორია"
            if out_path == source_dir:
                return f"ბლოკი #{slot['index']} - შედეგი = source: დაუშვებელია"
            try:
                out_path.relative_to(source_dir)
                return f"ბლოკი #{slot['index']} - შედეგი source-ში არ უნდა იყოს"
            except ValueError:
                pass
            all_outputs.append(out_path)
        if len(set(str(p) for p in all_outputs)) != len(all_outputs):
            return "ყველა ბლოკს სხვადასხვა შედეგის საქაღალდე უნდა ჰქონდეს"
        return None

    # ---- Worker thread ----
    def router_worker(
        active_slots: List[Dict[str, Any]],
        _unused_ref_matrix: Any,
        threshold_value: float,
        source_dir: Path,
    ) -> None:
        nonlocal router_duplicate_index, router_resume_tracker
        while True:
            item = router_queue.get()
            try:
                if item is None:
                    return
                if router_stop_event.is_set():
                    continue
                while router_pause_event.is_set() and not router_stop_event.is_set():
                    time.sleep(0.12)
                if router_stop_event.is_set():
                    continue

                throttle = resource_throttle_delay(performance_profile_var.get())
                if throttle > 0:
                    time.sleep(throttle)

                image_path = Path(item)
                rel: Optional[Path] = None
                best_score = second_score = 0.0
                top_matches: List[Dict[str, Any]] = []
                fingerprint: Dict[str, Any] = {}
                face_count = 0
                try:
                    rel = image_path.resolve().relative_to(source_dir.resolve())
                    if router_state_path is not None:
                        mark_router_scan_in_progress(
                            router_state_data, router_state_path, router_state_lock,
                            rel, source_path=str(image_path),
                        )
                    image = _load_image_with_retry(image_path, attempts=3)
                    fingerprint = file_fingerprint(image_path, include_sha=False)
                    ensure_fingerprint_sha(fingerprint, image_path)
                    if not isinstance(router_duplicate_index, DuplicateIndex):
                        router_duplicate_index = DuplicateIndex()
                    duplicate, duplicate_status, duplicate_note = router_duplicate_index.check_and_add(fingerprint, image)
                    if duplicate:
                        destination = ""
                        if str(router_state_data.get("duplicate_mode", "მხოლოდ ანგარიშში")) == "ცალკე საქაღალდეში":
                            _set_operation_context(
                                str(router_state_data.get("active_scan_id", "")), "router", image_path, rel
                            )
                            try:
                                destination = str(
                                    move_file_safely(
                                        image_path,
                                        Path(active_slots[0]["out_folder"]) / "_duplicates" / rel,
                                        fingerprint,
                                    )
                                )
                            finally:
                                _clear_operation_context()
                        record_router_scan_result(
                            router_state_data, router_state_path, router_state_lock, rel,
                            duplicate_status, source_path=str(image_path), fingerprint=fingerprint,
                            destination_path=destination, note=duplicate_note,
                        )
                        with router_lock:
                            router_stats["duplicates"] += 1
                        continue

                    embeddings = _get_embeddings_with_retry(image_path, image, fingerprint, attempts=2)
                    face_count = int(embeddings.shape[0])
                    slot_best = [0.0 for _ in active_slots]
                    matched_positions: Set[int] = set()
                    review_candidates: List[Tuple[float, float, int]] = []
                    for embedding in embeddings:
                        scores = [
                            score_embedding_to_identity(embedding, slot["ref_matrix"], slot["centroid"])
                            for slot in active_slots
                        ]
                        if not scores:
                            continue
                        for position, score in enumerate(scores):
                            slot_best[position] = max(slot_best[position], float(score))
                        order = np.argsort(np.asarray(scores))[::-1]
                        top_position = int(order[0])
                        top_score = float(scores[top_position])
                        next_score = float(scores[int(order[1])]) if len(order) > 1 else 0.0
                        gap = top_score - next_score
                        if top_score >= threshold_value and (len(order) == 1 or gap >= config.ambiguity_margin):
                            matched_positions.add(top_position)
                        elif top_score >= max(0.0, threshold_value - _review_margin()):
                            review_candidates.append((top_score, next_score, top_position))

                    order_all = np.argsort(np.asarray(slot_best))[::-1][:3] if slot_best else []
                    top_matches = [
                        {
                            "slot": int(active_slots[int(position)]["index"]),
                            "name": str(active_slots[int(position)].get("display_name") or f"ბლოკი #{active_slots[int(position)]['index']}"),
                            "reference": str((active_slots[int(position)].get("ref_paths") or [""])[0]),
                            "score": round(float(slot_best[int(position)]), 6),
                        }
                        for position in order_all
                    ]
                    best_score = float(top_matches[0]["score"]) if top_matches else 0.0
                    second_score = float(top_matches[1]["score"]) if len(top_matches) > 1 else 0.0
                    matched_slots = [active_slots[position] for position in sorted(matched_positions)]
                    destination_paths: List[str] = []

                    if matched_slots:
                        _set_operation_context(
                            str(router_state_data.get("active_scan_id", "")), "router", image_path, rel
                        )
                        try:
                            primary = move_file_safely(
                                image_path, Path(matched_slots[0]["out_folder"]) / rel, fingerprint
                            )
                            destination_paths.append(str(primary))
                            for slot in matched_slots[1:]:
                                destination_paths.append(
                                    str(copy_file_safely(primary, Path(slot["out_folder"]) / rel, fingerprint))
                                )
                        finally:
                            _clear_operation_context()
                        record_router_scan_result(
                            router_state_data, router_state_path, router_state_lock, rel,
                            "matched", source_path=str(image_path), fingerprint=fingerprint,
                            destination_path=str(primary), destination_paths=destination_paths,
                            slot_indices=[int(slot["index"]) for slot in matched_slots],
                            best_similarity=best_score, second_similarity=second_score,
                            top_matches=top_matches, face_count=face_count,
                        )
                        with router_lock:
                            router_stats["matched"] += 1
                            for slot in matched_slots:
                                router_stats["per_slot"].setdefault(slot["index"], 0)
                                router_stats["per_slot"][slot["index"]] += 1
                    elif review_candidates:
                        top_score, next_score, slot_position = max(review_candidates, key=lambda value: value[0])
                        target_slot = active_slots[slot_position]
                        planned = [str(Path(target_slot["out_folder"]) / rel)]
                        review_id = db_add_review(
                            str(router_state_data.get("active_scan_id", "")), "router",
                            str(image_path), rel.as_posix(), top_score, next_score, top_matches,
                            {"destinations": planned, "action": "move", "slot": int(target_slot["index"])},
                        )
                        record_router_scan_result(
                            router_state_data, router_state_path, router_state_lock, rel,
                            "review", source_path=str(image_path), fingerprint=fingerprint,
                            best_similarity=top_score, second_similarity=next_score,
                            top_matches=top_matches, face_count=face_count, review_id=review_id,
                        )
                        with router_lock:
                            router_stats["review"] += 1
                    else:
                        record_router_scan_result(
                            router_state_data, router_state_path, router_state_lock, rel,
                            "unmatched", source_path=str(image_path), fingerprint=fingerprint,
                            best_similarity=best_score, second_similarity=second_score,
                            top_matches=top_matches, face_count=face_count, note="left_in_source",
                        )
                        with router_lock:
                            router_stats["unmatched"] += 1
                except Exception as exc:
                    _clear_operation_context()
                    logger.error(f"Routing error {image_path}: {exc}")
                    if rel is not None and router_state_path is not None:
                        record_router_scan_result(
                            router_state_data, router_state_path, router_state_lock, rel,
                            "error", source_path=str(image_path), fingerprint=fingerprint,
                            error=str(exc), best_similarity=best_score,
                            second_similarity=second_score, top_matches=top_matches,
                            face_count=face_count, retryable=True,
                        )
                    with router_lock:
                        router_stats["errors"] += 1
                finally:
                    with router_lock:
                        router_stats["done"] += 1
                        done = router_stats["done"]
                        total = max(1, router_stats["total"])
                        elapsed, eta, speed = get_router_time_stats()
                        eta_text = format_router_duration(eta) if speed > 0 else "ითვლება..."
                        percentage = done / total * 100
                        remaining = max(0, total - done)
                        text = (
                            f"პროგრესი: {done}/{total} ({percentage:.1f}%) | დარჩენილი: {remaining} ({100-percentage:.1f}%) | "
                            f"სიჩქარე: {speed:.2f}/წმ | დარჩ. დრო: {eta_text} | გასული: {format_router_duration(elapsed)} | "
                            f"განაწ.: {router_stats['matched']} | Review: {router_stats['review']} | "
                            f"დუბლ.: {router_stats['duplicates']} | დარჩა: {router_stats['unmatched']} | შეცდ.: {router_stats['errors']}"
                        )
                    if isinstance(router_resume_tracker, ExactResumeTracker) and rel is not None:
                        entry = dict(router_state_data.get("files", {}).get(rel.as_posix()) or {})
                        result_status = str(entry.get("status", "error"))
                        router_resume_tracker.append_result(rel, result_status, entry, done, total)
                        snapshot = router_resume_tracker.progress_snapshot()
                        pending_index = router_resume_tracker.first_pending_index()
                        with router_lock:
                            router_stats["done"] = int(snapshot["completed"])
                            router_stats["total"] = int(snapshot["total"])
                        router_state_data["resume_checkpoint"] = {
                            "version": RESUME_JOURNAL_VERSION,
                            "total": int(snapshot["total"]),
                            "completed": int(snapshot["completed"]),
                            "remaining": int(snapshot["remaining"]),
                            "source_remaining": int(snapshot["source_remaining"]),
                            "moved_count": int(snapshot["moved_out"]),
                            "checked_but_not_moved": int(snapshot["completed_in_source"]),
                            "progress_percent": round((int(snapshot["completed"]) / max(1, int(snapshot["total"]))) * 100.0, 6),
                            "last_completed_relative_path": rel.as_posix(),
                            "last_completed_status": result_status,
                            "next_index": min(int(snapshot["total"]), pending_index) + (1 if pending_index < int(snapshot["total"]) else 0),
                            "last_saved_at": now_iso(),
                            "manifest_file": router_resume_tracker.manifest_path.name,
                            "journal_file": router_resume_tracker.journal_path.name,
                        }
                    safe_set_progress(text, done)
            except Exception as exc:
                logger.exception(f"Router worker-ის მოულოდნელი შეცდომა: {exc}")
                with router_lock:
                    router_stats["errors"] += 1
                    router_stats["done"] += 1
            finally:
                router_queue.task_done()


    # ---- finish_router ----
    def finish_router(cancelled: bool) -> None:
        nonlocal router_running, router_threads
        router_running = False
        router_threads = []
        router_pause_event.clear()
        start_btn_router.config(state=NORMAL)
        stop_btn_router.config(state=DISABLED)
        pause_btn_router.config(state=DISABLED, text="⏸ პაუზა")
        totals = {
            "total": router_stats.get("total", 0), "processed": router_stats.get("done", 0),
            "matched": router_stats.get("matched", 0), "review": router_stats.get("review", 0),
            "duplicates": router_stats.get("duplicates", 0), "unmatched": router_stats.get("unmatched", 0),
            "errors": router_stats.get("errors", 0), "resumed": router_stats.get("resumed", 0),
        }
        router_state_data["stats"] = totals
        router_state_data["stats_per_slot"] = {str(k): v for k, v in router_stats.get("per_slot", {}).items()}
        router_state_data["last_scan_status"] = "cancelled" if cancelled else "completed"
        router_state_data["last_scan_finished_at"] = now_iso()
        if router_state_path is not None:
            persist_json_state(router_state_path, router_state_data, force=True)
        if isinstance(router_resume_tracker, ExactResumeTracker):
            router_resume_tracker.close()
        if (not cancelled and int(router_stats.get("total", 0)) > 0
                and int(router_stats.get("done", 0)) >= int(router_stats.get("total", 0))):
            clear_completed_json_state(router_state_path)
        scan_id = str(router_state_data.get("active_scan_id", ""))
        db_finish_scan(scan_id, "cancelled" if cancelled else "completed", totals)
        list_path = export_checked_nonmatches(Path(router_state_data.get("source_folder", ".")), "router")
        append_log(f"Router უდამთხვევო ფოტოების სია განახლდა: {list_path}", "success")
        update_summary_box()
        elapsed, _, _ = get_router_time_stats()
        per_slot_lines = "\n".join(
            f"ბლოკი #{idx}: {cnt}" for idx, cnt in sorted(router_stats["per_slot"].items())
        ) or "ბლოკებში არაფერი მოხვდა"
        progress_label_router.config(
            text=f"{'გაჩერდა' if cancelled else 'დასრულდა'}: "
                 f"{router_stats['done']}/{router_stats['total']} | {format_router_duration(elapsed)}"
        )
        if not cancelled and int(router_stats.get("done", 0)) >= int(router_stats.get("total", 0)):
            notify_scan_completed(int(router_stats.get("done", 0)), int(router_stats.get("total", 0)))
        if router_close_pending:
            _destroy_router_window()
            if globals().get("_APP_EXIT_AFTER_ROUTER", False):
                globals()["_APP_EXIT_AFTER_ROUTER"] = False
                root.after(0, on_app_close)
            return
        messagebox.showinfo(
            "გაჩერებულია" if cancelled else "დასრულდა",
            f"{per_slot_lines}\n\nReview: {router_stats['review']}\n"
            f"დუბლიკატი: {router_stats['duplicates']}\n"
            f"ადგილზე დარჩა: {router_stats['unmatched']}\n"
            f"შეცდომა: {router_stats['errors']}\n"
            f"დრო: {format_router_duration(elapsed)}"
        )
        root.after(200, ensure_live_watch_started)

    # ---- start_router ----
    def start_router() -> None:
        nonlocal router_running, router_threads, router_state_path, router_state_data, router_duplicate_index, router_resume_tracker
        if router_running:
            messagebox.showwarning("მიმდინარეობს", "გადანაწილება უკვე გაშვებულია")
            return
        source_value = source_var.get().strip()
        if not source_value:
            messagebox.showerror("შეცდომა", "აირჩიე საერთო source folder")
            return
        source_dir = Path(source_value).resolve()
        if not source_dir.exists():
            messagebox.showerror("შეცდომა", "source folder ვერ მოიძებნა")
            return
        people_count = _safe_int(people_count_var.get(), 4, 1, 20)
        people_count_var.set(str(people_count))
        active_slots = [slot for slot in slot_states if slot.get("active")][:people_count]
        for slot in active_slots:
            refs = slot.get("ref_paths") or ([slot.get("ref_path")] if slot.get("ref_path") else [])
            if not refs:
                messagebox.showerror("შეცდომა", f"ბლოკი #{slot['index']} — reference ფოტოები არ არის")
                return
            if not slot.get("out_folder"):
                messagebox.showerror("შეცდომა", f"ბლოკი #{slot['index']} — შედეგის საქაღალდე არ არის")
                return
        error = validate_router_paths(active_slots, source_dir)
        if error:
            messagebox.showerror("შეცდომა", error)
            return
        stop_live_watch(wait=True)
        ensure_face_engine(model_profile_var.get())
        try:
            for slot in active_slots:
                refs = slot.get("ref_paths") or [slot["ref_path"]]
                embeddings: List[np.ndarray] = []
                skipped: List[str] = []
                for reference in refs:
                    try:
                        quality, _details = check_face_quality(reference)
                    except Exception:
                        quality = 0
                    if quality < config.threshold_min:
                        skipped.append(f"{Path(reference).name} (ხარისხი {quality}%)")
                        continue
                    try:
                        embeddings.append(get_emb(reference))
                    except Exception:
                        skipped.append(f"{Path(reference).name} (სახე ვერ მოიძებნა)")
                        continue
                if not embeddings:
                    raise ValueError(
                        f"ბლოკი #{slot['index']} — არცერთი გამოსადეგი reference ფოტო ვერ მოიძებნა "
                        f"({len(refs)} ფოტოდან ვერცერთმა ვერ გაიარა შემოწმება). "
                        f"დაამატე უფრო ნათელი, წინა მხრიდან გადაღებული ფოტო."
                    )
                if skipped:
                    _append_rlog(
                        f"ბლოკი #{slot['index']} ({slot.get('display_name') or slot['index']}) — "
                        f"გამოტოვებულია {len(skipped)}/{len(refs)} ფოტო: " + "; ".join(skipped),
                        "warning",
                    )
                slot["ref_embs"] = embeddings
                slot["ref_matrix"], slot["centroid"] = _identity_profile(embeddings)
                slot["ref_emb"] = slot["centroid"]
                slot["display_name"] = slot.get("display_name") or f"ბლოკი #{slot['index']}"
        except Exception as exc:
            messagebox.showerror("Reference შეცდომა", str(exc))
            root.after(150, ensure_live_watch_started)
            return

        threshold_value = router_slider.get() / 100.0
        config.duplicate_phash_distance = _safe_int(
            duplicate_distance_var.get(), config.duplicate_phash_distance, 0, 16
        )
        config.ambiguity_margin = _safe_float(
            ambiguity_margin_var.get(), config.ambiguity_margin, 0.0, 0.20
        )
        duplicate_distance_var.set(str(config.duplicate_phash_distance))
        ambiguity_margin_var.set(str(config.ambiguity_margin))
        requested_workers = _safe_int(worker_count_var.get(), 4, 1, 20)
        worker_count_var.set(str(requested_workers))
        worker_count = _effective_worker_count(requested_workers, performance_profile_var.get())

        router_state_path, router_state_data = prepare_router_scan_state(
            source_dir, active_slots, threshold_value, int(people_count_var.get()), worker_count
        )
        scan_id = f"router-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        router_state_data["active_scan_id"] = scan_id
        router_state_data.pop("dry_run", None)
        router_state_data["run_mode"] = "real"
        router_state_data["duplicate_mode"] = duplicate_mode_var.get()
        router_state_data["matching_signature"] = _matching_signature(
            threshold_value, router_signature_value=str(router_state_data.get("signature", ""))
        )
        router_state_data["identity_signature"] = _identity_signature(
            router_signature_value=str(router_state_data.get("signature", ""))
        )
        router_state_data["recognition_signature"] = _recognition_signature(
            threshold_value, router_signature_value=str(router_state_data.get("signature", ""))
        )
        persist_json_state(router_state_path, router_state_data, force=True)
        db_start_scan(
            scan_id, "router", str(source_dir), [slot["out_folder"] for slot in active_slots],
            [{"slot": slot["index"], "refs": slot.get("ref_paths", [])} for slot in active_slots],
            {
                "threshold": threshold_value, "workers": worker_count,
                "model_signature": _current_model_signature(),
                "run_mode": "real",
                "matching_signature": router_state_data["matching_signature"],
                "recognition_signature": router_state_data["recognition_signature"],
                "router_signature": router_state_data.get("signature", ""),
                "ambiguity_margin": config.ambiguity_margin,
                "algorithm": MATCHING_ALGORITHM_VERSION,
            },
        )
        with state.db_lock:
            for slot in active_slots:
                scan_cur.execute(
                    "INSERT INTO identities(scan_id,identity_index,display_name,refs_json,created_at) VALUES (?,?,?,?,?)",
                    (
                        scan_id, int(slot["index"]), str(slot.get("display_name") or f"ბლოკი #{slot['index']}"),
                        json.dumps(slot.get("ref_paths", []), ensure_ascii=False), now_iso(),
                    ),
                )
            _db_commit_locked(force=True)

        all_files = iter_image_files(source_dir)
        references = {
            str(Path(reference).resolve())
            for slot in active_slots
            for reference in (slot.get("ref_paths") or [slot.get("ref_path")]) if reference
        }
        discovered_candidates = [path for path in all_files if str(path.resolve()) not in references]
        expected_mode = "real"
        matching_signature = str(router_state_data.get("matching_signature", ""))
        identity_signature = str(router_state_data.get("identity_signature", ""))
        if router_state_path is None:
            raise RuntimeError("Router resume state path ვერ მომზადდა")
        if isinstance(router_resume_tracker, ExactResumeTracker):
            router_resume_tracker.close()
        router_resume_tracker = ExactResumeTracker(
            router_state_path, source_dir, "router", matching_signature, expected_mode,
            discovered_candidates, legacy_entries=_router_profile_entries(router_state_data),
            identity_signature=identity_signature,
        )
        candidates = router_resume_tracker.ordered_current_paths()

        _seed_checked_nonmatches_from_router(
            router_state_data, source_dir, str(router_state_data.get("recognition_signature", ""))
        )
        router_checked_index = CheckedNonmatchIndex(
            source_dir, "router", str(router_state_data.get("recognition_signature", ""))
        )
        interrupted = count_router_in_progress_entries(router_state_data)
        files_to_process: List[Path] = []
        resumed_fingerprints: List[Dict[str, Any]] = []
        resumed = router_resume_tracker.missing_completed_count()
        router_new_count = 0
        router_changed_count = 0
        router_reprocess_count = 0
        for path in candidates:
            rel = path.relative_to(source_dir)
            entry = router_state_data.get("files", {}).get(rel.as_posix())
            journal_event = router_resume_tracker.current_event(rel)
            if router_resume_tracker.event_is_current(rel, path):
                resumed += 1
                if isinstance(journal_event, dict) and isinstance(journal_event.get("fingerprint"), dict):
                    resumed_fingerprints.append(dict(journal_event["fingerprint"]))
            elif _router_entry_is_current(router_state_data, rel, path, identity_signature, expected_mode):
                resumed += 1
                if isinstance(entry, dict):
                    router_resume_tracker.seed_entry(rel, entry, moved=False)
                    if isinstance(entry.get("fingerprint"), dict):
                        resumed_fingerprints.append(entry["fingerprint"])
            else:
                known_nonmatch, skip_reason, ledger_entry = router_checked_index.lookup(path, rel)
                if known_nonmatch:
                    resumed += 1
                    if isinstance(ledger_entry, dict):
                        resumed_fingerprints.append(dict(ledger_entry))
                        router_resume_tracker.seed_entry(
                            rel,
                            {
                                "status": "unmatched", "fingerprint": dict(ledger_entry),
                                "matching_signature": matching_signature, "run_mode": expected_mode,
                            },
                            moved=False,
                        )
                    continue
                files_to_process.append(path)
                pending_kind = _classify_pending_photo(entry, path)
                if pending_kind == "new":
                    router_new_count += 1
                elif pending_kind == "changed":
                    router_changed_count += 1
                else:
                    router_reprocess_count += 1

        append_log(
            _photo_change_log_message(
                "Router ფოლდერის შემოწმება",
                router_new_count,
                router_changed_count,
                router_reprocess_count,
                resumed,
            ),
            "success" if router_new_count > 0 else "info",
        )
        append_log(
            f"Router სწრაფი გამოტოვება | metadata: {router_checked_index.stat_skips} | "
            f"hash: {router_checked_index.hash_skips} | სახელით შედარება: გამორთულია",
            "info",
        )
        snapshot = router_resume_tracker.progress_snapshot()
        router_total = int(snapshot["total"])
        resumed = int(snapshot["completed"])
        first_pending_index = router_resume_tracker.first_pending_index()
        restored_percent = (resumed / max(1, router_total)) * 100.0
        router_state_data["resume_checkpoint"] = {
            "version": RESUME_JOURNAL_VERSION,
            "total": router_total,
            "completed": resumed,
            "remaining": int(snapshot["remaining"]),
            "source_remaining": int(snapshot["source_remaining"]),
            "moved_count": int(snapshot["moved_out"]),
            "checked_but_not_moved": int(snapshot["completed_in_source"]),
            "progress_percent": round(restored_percent, 6),
            "next_index": min(router_total, first_pending_index) + (1 if first_pending_index < router_total else 0),
            "last_saved_at": now_iso(),
            "manifest_file": router_resume_tracker.manifest_path.name,
            "journal_file": router_resume_tracker.journal_path.name,
        }
        persist_json_state(router_state_path, router_state_data, force=True)
        append_log(
            f"Router ზუსტი გაგრძელება | {resumed}/{router_total} ({restored_percent:.1f}%) | "
            f"უკვე გადატანილი: {snapshot['moved_out']} | Source-ში დარჩენილი: {snapshot['source_remaining']} | "
            f"დასამუშავებელი დარჩა: {snapshot['remaining']} | შემდეგი პოზიცია: {min(router_total, first_pending_index) + (1 if first_pending_index < router_total else 0)}",
            "success" if resumed else "info",
        )
        if router_total == 0:
            db_finish_scan(scan_id, "empty", {"total": 0})
            export_checked_nonmatches(source_dir, "router")
            router_resume_tracker.close()
            messagebox.showinfo("ინფორმაცია", "source folder-ში ფოტოები ვერ მოიძებნა")
            return
        if not files_to_process:
            db_finish_scan(scan_id, "already_completed", {"total": router_total, "resumed": resumed})
            list_path = export_checked_nonmatches(source_dir, "router")
            append_log(f"Router უდამთხვევო ფოტოების სია: {list_path}", "success")
            progress_router["maximum"] = max(1, router_total)
            progress_router["value"] = resumed
            router_resume_tracker.close()
            messagebox.showinfo("ინფორმაცია", f"ყველა უცვლელი ფოტო უკვე დამუშავებულია: {resumed}")
            return

        router_stop_event.clear()
        router_pause_event.clear()
        router_duplicate_index = DuplicateIndex()
        for resumed_fingerprint in resumed_fingerprints:
            router_duplicate_index.seed(resumed_fingerprint)
        while True:
            try:
                router_queue.get_nowait()
                router_queue.task_done()
            except queue.Empty:
                break
        router_stats.update({
            "matched": 0, "unmatched": 0, "review": 0, "duplicates": 0, "errors": 0,
            "done": resumed, "total": router_total, "resumed": resumed,
            "per_slot": {slot["index"]: 0 for slot in active_slots}, "start_time": time.time(),
            "unmatched_dir": "", "state_file": router_state_path.name if router_state_path else "",
        })
        progress_router["maximum"] = max(1, router_total)
        progress_router["value"] = resumed
        update_summary_box()
        progress_label_router.config(
            text=f"იწყება... სულ {router_total} | ახალი {router_new_count} | "
                 f"შეცვლილი {router_changed_count} | ხელახლა {router_reprocess_count} | "
                 f"გამოტოვებული {resumed} | შეწყვეტილი {interrupted} | worker {worker_count}"
        )
        for path in files_to_process:
            router_queue.put(path)
        router_running = True
        start_btn_router.config(state=DISABLED)
        stop_btn_router.config(state=NORMAL)
        pause_btn_router.config(state=NORMAL, text="⏸ პაუზა")
        threads: List[threading.Thread] = []
        for _ in range(worker_count):
            thread = threading.Thread(
                target=router_worker, args=(active_slots, None, threshold_value, source_dir), daemon=True
            )
            thread.start()
            threads.append(thread)
        router_threads = threads

        def wait_router() -> None:
            router_queue.join()
            for _ in threads:
                router_queue.put(None)
            for thread in threads:
                thread.join()
            post_ui(lambda: finish_router(router_stop_event.is_set()))

        threading.Thread(target=wait_router, daemon=True).start()


    # ---- stop_router ----
    def stop_router() -> None:
        if not router_running:
            return
        router_pause_event.clear()
        router_stop_event.set()
        stop_btn_router.config(state=DISABLED)
        pause_btn_router.config(state=DISABLED)
        progress_label_router.config(text="გაჩერება მოთხოვნილია...")
        removed = 0
        while True:
            try:
                item = router_queue.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                removed += 1
            router_queue.task_done()
        if router_state_path is not None and router_state_data:
            try:
                persist_json_state(router_state_path, router_state_data, force=True)
            except Exception as exc:
                logger.warning(f"Router state save: {exc}")
        progress_label_router.config(text=f"გაჩერება... რიგიდან წაიშალა: {removed}")

    def toggle_router_pause() -> None:
        if not router_running:
            return
        if router_pause_event.is_set():
            router_pause_event.clear()
            pause_btn_router.config(text="⏸ პაუზა")
            _append_rlog("გადანაწილება გაგრძელდა", "success")
        else:
            router_pause_event.set()
            pause_btn_router.config(text="▶ გაგრძელება")
            _append_rlog("გადანაწილება დაპაუზებულია", "warning")

    start_btn_router.config(command=start_router)
    stop_btn_router.config(command=stop_router)
    pause_btn_router.config(command=toggle_router_pause)

    # A router history entry selected from the main history window is restored
    # only after all router widgets and callbacks exist.
    def _restore_into_open_router(data: Dict[str, Any], auto_start: bool = False) -> bool:
        if router_running:
            messagebox.showwarning(
                "ისტორია", "მიმდინარე გადანაწილების დასრულებამდე ისტორიის პარამეტრები ვერ შეიცვლება.",
                parent=router_window,
            )
            return False
        restored = _restore_router_history_payload(
            data, router_slider, people_count_var, worker_count_var,
            src_lbl, source_var, slot_states, router_window,
        )
        if restored:
            router_window.deiconify()
            router_window.lift()
            if auto_start:
                router_window.after_idle(start_router)
        return bool(restored)

    pending_router_restore = globals().pop("_PENDING_ROUTER_HISTORY_RESTORE", None)
    if isinstance(pending_router_restore, dict):
        pending_data = pending_router_restore.get("data")
        pending_auto_start = bool(pending_router_restore.get("auto_start", False))

        def _apply_pending_router_restore() -> None:
            if isinstance(pending_data, dict):
                _restore_into_open_router(pending_data, pending_auto_start)

        router_window.after(120, _apply_pending_router_restore)

    def _destroy_router_window() -> None:
        if _ACTIVE_ROUTER_CONTROLLER.get("window") is router_window:
            _ACTIVE_ROUTER_CONTROLLER.clear()
        try:
            logger.removeHandler(_rlog_hdl)
        except Exception:
            pass
        try:
            slots_canvas.unbind("<MouseWheel>")
        except Exception:
            pass
        try:
            if router_window.winfo_exists():
                router_window.destroy()
        except Exception:
            pass

    def on_router_close() -> None:
        nonlocal router_close_pending
        if router_running:
            if not router_stop_event.is_set():
                if not messagebox.askyesno("გასვლა", "გადანაწილება მიმდინარეობს. უსაფრთხოდ გავაჩერო?"):
                    return
                stop_router()
            router_close_pending = True
            router_window.withdraw()
            return
        _destroy_router_window()

    _ACTIVE_ROUTER_CONTROLLER.update({
        "window": router_window,
        "is_running": lambda: bool(router_running),
        "stop": stop_router,
        "close": on_router_close,
        "destroy": _destroy_router_window,
        "restore": _restore_into_open_router,
    })
    router_window.protocol("WM_DELETE_WINDOW", on_router_close)

def _restore_router_history_payload(
    data: Dict[str, Any],
    router_slider,
    people_count_var,
    worker_count_var,
    src_lbl,
    source_var,
    slot_states: List[Dict[str, Any]],
    parent_window,
) -> bool:
    """Restore and validate a router history entry into the current router UI."""
    if not isinstance(data, dict):
        messagebox.showerror("ისტორია", "ისტორიის ჩანაწერი დაზიანებულია.", parent=parent_window)
        return False

    params = data.get("params", {}) if isinstance(data.get("params"), dict) else {}
    source_value = str(data.get("source_folder") or params.get("source_folder") or "").strip()
    if not source_value:
        messagebox.showerror("Source", "ისტორიაში Source საქაღალდე არ არის შენახული.", parent=parent_window)
        return False
    source_path = Path(source_value).expanduser()
    if not source_path.is_dir():
        messagebox.showerror(
            "Source ვერ მოიძებნა",
            f"Source საქაღალდე აღარ არსებობს:\n{source_path}",
            parent=parent_window,
        )
        return False

    slots_data = params.get("slots") if isinstance(params.get("slots"), list) else data.get("slots", [])
    if not isinstance(slots_data, list) or not slots_data:
        messagebox.showerror("ბლოკები", "ისტორიაში ბლოკების მონაცემები არ არის შენახული.", parent=parent_window)
        return False

    try:
        slot_count = int(params.get("slot_count", data.get("slot_count", len(slots_data))))
    except (TypeError, ValueError):
        slot_count = len(slots_data)
    slot_count = max(1, min(len(slot_states), slot_count, 20))

    try:
        threshold = float(params.get("threshold", data.get("threshold", config.threshold_default / 100)))
    except (TypeError, ValueError):
        threshold = config.threshold_default / 100
    threshold_percent = int(round(threshold * 100)) if threshold <= 1.0 else int(round(threshold))
    threshold_percent = max(config.threshold_min, min(config.threshold_max, threshold_percent))

    try:
        worker_count = int(params.get("worker_count", data.get("last_worker_count", config.worker_count)))
    except (TypeError, ValueError):
        worker_count = config.worker_count
    worker_count = max(config.worker_min, min(config.worker_max, worker_count))

    # Clear old card values first so stale references cannot survive a restore.
    for slot in slot_states:
        slot["ref_path"] = ""
        slot["ref_paths"] = []
        slot["out_folder"] = ""
        slot["ref_emb"] = None
        slot["ref_embs"] = []
        slot["ref_matrix"] = None
        slot["centroid"] = None
        slot["ref_label"].config(text="არ არის არჩეული")
        slot["out_label"].config(text="არ არის არჩეული")

    missing_refs: List[str] = []
    output_errors: List[str] = []
    restored_slots = 0
    for slot_data in slots_data:
        if not isinstance(slot_data, dict):
            continue
        try:
            slot_index = int(slot_data.get("index", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not (0 <= slot_index < len(slot_states)):
            continue

        raw_refs = slot_data.get("ref_paths")
        if not isinstance(raw_refs, list):
            one_ref = slot_data.get("ref_path")
            raw_refs = [one_ref] if one_ref else []
        valid_refs: List[str] = []
        for ref_value in raw_refs:
            if not ref_value:
                continue
            ref_path = Path(str(ref_value)).expanduser()
            if ref_path.is_file():
                valid_refs.append(str(ref_path.resolve()))
            else:
                missing_refs.append(str(ref_path))

        out_value = str(slot_data.get("out_folder") or "").strip()
        out_path: Optional[Path] = None
        if out_value:
            try:
                out_path = Path(out_value).expanduser().resolve()
                out_path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                output_errors.append(f"ბლოკი #{slot_index + 1}: {out_value} — {exc}")
                out_path = None

        slot = slot_states[slot_index]
        if valid_refs:
            slot["ref_paths"] = valid_refs
            slot["ref_path"] = valid_refs[0]
            names = ", ".join(Path(ref).name for ref in valid_refs[:3])
            if len(valid_refs) > 3:
                names += f" ... (+{len(valid_refs)-3})"
            slot["ref_label"].config(text=f"{len(valid_refs)} ფოტო: {names}")
        if out_path is not None:
            slot["out_folder"] = str(out_path)
            slot["out_label"].config(text=str(out_path))
        if valid_refs and out_path is not None:
            restored_slots += 1

    if restored_slots == 0:
        details = []
        if missing_refs:
            details.append("დაკარგული reference:\n" + "\n".join(missing_refs[:6]))
        if output_errors:
            details.append("Output შეცდომა:\n" + "\n".join(output_errors[:4]))
        messagebox.showerror(
            "ისტორიის აღდგენა ვერ მოხერხდა",
            "არცერთი სრულად გამართული ბლოკი ვერ აღდგა.\n\n" + "\n\n".join(details),
            parent=parent_window,
        )
        return False

    source_var.set(str(source_path.resolve()))
    src_lbl.config(text=f"Source: {source_path.resolve()}")
    router_slider.set(threshold_percent)
    people_count_var.set(str(slot_count))
    worker_count_var.set(str(worker_count))
    try:
        parent_window.update_idletasks()
    except Exception:
        pass

    warnings: List[str] = []
    if missing_refs:
        warnings.append(f"დაკარგული reference: {len(missing_refs)}")
    if output_errors:
        warnings.append(f"Output შეცდომა: {len(output_errors)}")
    if warnings:
        messagebox.showwarning(
            "ნაწილობრივ აღდგა",
            f"აღდგა {restored_slots} გამართული ბლოკი.\n" + "\n".join(warnings),
            parent=parent_window,
        )
    return True



def _list_history_state_files(directory: Path) -> List[Path]:
    """Return only primary history JSON files, excluding resume manifests."""
    result: List[Path] = []
    try:
        candidates = directory.glob("*.json")
    except OSError:
        return result
    for path in candidates:
        name = path.name.lower()
        if name.endswith(".resume_manifest.json") or name.endswith(".broken.json"):
            continue
        try:
            if path.is_file():
                result.append(path)
        except OSError:
            continue
    def _mtime(item: Path) -> float:
        try:
            return item.stat().st_mtime
        except OSError:
            return 0.0
    return sorted(result, key=_mtime, reverse=True)


def _history_companion_files(state_path: Path) -> List[Path]:
    """Files that belong to one history/Resume record."""
    state_path = Path(state_path)
    return [
        state_path,
        state_path.with_name(state_path.stem + ".resume_manifest.json"),
        state_path.with_name(state_path.stem + ".resume_journal.jsonl"),
        state_path.with_suffix(state_path.suffix + ".broken"),
    ]


def _delete_history_record(state_path: Path) -> Tuple[int, List[str]]:
    """Delete one history record and its Resume companions, never user photos."""
    path = Path(state_path)
    allowed_dirs = {SCAN_STATE_DIR.resolve(), ROUTER_STATE_DIR.resolve()}
    try:
        parent = path.resolve().parent
    except OSError:
        parent = path.absolute().parent
    if parent not in allowed_dirs:
        return 0, [f"დაუშვებელი ისტორიის მისამართი: {path}"]

    deleted = 0
    errors: List[str] = []
    for candidate in _history_companion_files(path):
        try:
            resolved_key = str(candidate.resolve())
        except OSError:
            resolved_key = str(candidate.absolute())
        JSON_SAVE_TRACKERS.pop(resolved_key, None)
        try:
            if candidate.exists():
                candidate.unlink()
                deleted += 1
        except OSError as exc:
            errors.append(f"{candidate.name}: {exc}")

    # Prevent the deleted main-scan state from being recreated on application exit.
    try:
        active_path = state.scan_state_path.resolve() if state.scan_state_path else None
        deleted_path = path.resolve()
    except OSError:
        active_path = state.scan_state_path.absolute() if state.scan_state_path else None
        deleted_path = path.absolute()
    if active_path == deleted_path and not state.scan_running:
        with state.state_lock:
            state.scan_state_path = None
            state.scan_state = {}
            state.current_profile = None

    return deleted, errors


def _history_delete_blocked(mode: str) -> bool:
    """Avoid deleting a state file while the corresponding scanner writes to it."""
    if mode == "scan":
        return bool(state.scan_running)
    controller = globals().get("_ACTIVE_ROUTER_CONTROLLER", {})
    is_running = controller.get("is_running") if isinstance(controller, dict) else None
    try:
        return bool(callable(is_running) and is_running())
    except Exception:
        return False

def _open_history_window_router(
    router_window, router_slider, people_count_var,
    worker_count_var, src_lbl, source_var, slot_states,
    start_callback: Optional[Callable[[], None]] = None,
) -> None:
    """Open router history window and restore parameters on selection."""
    state_files = _list_history_state_files(ROUTER_STATE_DIR)

    hw = Toplevel(router_window)
    hw.title("გადანაწილების ისტორია")
    hw.configure(bg="#0B1120")
    register_window_theme(hw, "router_history")
    ww, wh = configure_responsive_geometry(
        hw, 980, 700, minimum_width=620, minimum_height=460,
        width_ratio=0.92, height_ratio=0.88,
    )
    history_page, _history_canvas = create_scrollable_page(hw, minimum_content_width=600, horizontal=True)

    Label(history_page, text="გადანაწილების ისტორია", font=("Segoe UI", 14, "bold"),
          fg="#E8EDF5", bg="#0B1120").pack(pady=(14, 2))
    Label(history_page, text="ჩასქროლე • ერთჯერ = დეტალები • ორჯერ ან ✅ = პარამეტრების გამოყენება",
          font=("Segoe UI", 9), fg="#7B93B8", bg="#0B1120").pack(pady=(0, 6))
    Frame(history_page, height=1, bg="#1F2D4A").pack(fill=X, padx=16, pady=(0, 6))

    list_frame = Frame(history_page, bg="#0B1120")
    list_frame.pack(fill=BOTH, expand=True, padx=16)

    list_sb = Scrollbar(list_frame)
    list_sb.pack(side=RIGHT, fill=Y)
    from tkinter import Listbox
    lb = Listbox(list_frame, bg="#131C2E", fg="#7B93B8",
                 font=("Courier", 9), selectbackground="#4D7CFF",
                 selectforeground="#0B1120", relief=FLAT, bd=0,
                 yscrollcommand=list_sb.set, activestyle="none")
    lb.pack(side=LEFT, fill=BOTH, expand=True)
    list_sb.config(command=lb.yview)

    detail_lbl = Label(history_page, text="", font=("Segoe UI", 9), fg="#F5A623",
                       bg="#0B1120", wraplength=ww-40, justify=LEFT, anchor="w")
    detail_lbl.pack(fill=X, padx=16, pady=(6, 4))

    loaded: List[Tuple[Path, Dict[str, Any]]] = []

    for p in state_files:
        try:
            data = load_json(p)
            if not isinstance(data, dict):
                continue
            loaded.append((p, data))
            src = data.get("source_folder", "?")
            updated = data.get("updated_at", "?")
            thr = data.get("threshold", "?")
            slot_count = data.get("slot_count", "?")
            files_done = sum(1 for m in data.get("files", {}).values()
                             if isinstance(m, dict) and m.get("status") == "matched")
            files_total = len(data.get("files", {}))
            lb.insert(END,
                f"{p.name}  |  source: {Path(src).name if src != '?' else '?'}  |"
                f"  ბლოკი: {slot_count}  |  ზღვარი: {thr}  |"
                f"  matched: {files_done}/{files_total}  |  {updated}")
        except Exception as e:
            logger.warning(f"History router load {p}: {e}")

    if not loaded:
        lb.insert(END, "ისტორია ვერ მოიძებნა")

    def on_select(event=None) -> None:
        sel = lb.curselection()
        if not sel or sel[0] >= len(loaded):
            return
        _, data = loaded[sel[0]]
        parts = [f"source: {data.get('source_folder','?')}",
                 f"ზღვარი: {data.get('threshold','?')}",
                 f"ბლოკი: {data.get('slot_count','?')}"]
        for slot_data in data.get("slots", []):
            refs = slot_data.get("ref_paths") or ([slot_data.get("ref_path")] if slot_data.get("ref_path") else [])
            ref_preview = ", ".join(Path(ref).name for ref in refs[:2])
            if len(refs) > 2:
                ref_preview += f" +{len(refs)-2}"
            parts.append(f"#{slot_data.get('index','?')}: refs={ref_preview or '?'}  →  {slot_data.get('out_folder','?')}")
        files = data.get("files", {})
        matched = sum(1 for m in files.values() if isinstance(m, dict) and m.get("status") == "matched")
        parts.append(f"სულ: {len(files)}  matched: {matched}")
        detail_lbl.config(text="  |  ".join(parts[:6]))

    def _selected_router_data() -> Optional[Dict[str, Any]]:
        sel = lb.curselection()
        if not sel or sel[0] >= len(loaded):
            messagebox.showwarning("მონიშვნა საჭიროა", "ჯერ ისტორიიდან მონიშნე ჩანაწერი.", parent=hw)
            return None
        return loaded[sel[0]][1]

    def on_use(event=None, auto_start: bool = False) -> None:
        data = _selected_router_data()
        if data is None:
            return
        if not _restore_router_history_payload(
            data, router_slider, people_count_var, worker_count_var,
            src_lbl, source_var, slot_states, router_window,
        ):
            return
        hw.destroy()
        if auto_start:
            if start_callback is None:
                messagebox.showwarning(
                    "გაშვება",
                    "პარამეტრები აღდგა, მაგრამ გაშვების callback ხელმისაწვდომი არ არის.",
                    parent=router_window,
                )
                return
            router_window.after_idle(start_callback)
        else:
            messagebox.showinfo(
                "აღდგენილია",
                "პარამეტრები ჩატვირთულია — მზადაა გასაშვებად.",
                parent=router_window,
            )

    def on_use_and_start(event=None) -> None:
        on_use(event, auto_start=True)

    def on_view_json(event=None) -> None:
        sel = lb.curselection()
        if not sel or sel[0] >= len(loaded):
            return
        _view_json_file(loaded[sel[0]][0])

    def _refresh_router_history_list(select_index: Optional[int] = None) -> None:
        lb.delete(0, END)
        detail_lbl.config(text="")
        if not loaded:
            lb.insert(END, "ისტორია ვერ მოიძებნა")
            return
        for path, data in loaded:
            src = data.get("source_folder", "?")
            updated = data.get("updated_at", "?")
            thr = data.get("threshold", "?")
            slot_count = data.get("slot_count", "?")
            files_done = sum(
                1 for meta in data.get("files", {}).values()
                if isinstance(meta, dict) and meta.get("status") == "matched"
            )
            files_total = len(data.get("files", {}))
            lb.insert(
                END,
                f"{path.name}  |  source: {Path(src).name if src != '?' else '?'}  |"
                f"  ბლოკი: {slot_count}  |  ზღვარი: {thr}  |"
                f"  matched: {files_done}/{files_total}  |  {updated}",
            )
        if select_index is not None and loaded:
            idx = max(0, min(int(select_index), len(loaded) - 1))
            lb.selection_set(idx)
            lb.activate(idx)
            lb.see(idx)
            on_select()

    def on_delete_selected() -> None:
        if _history_delete_blocked("router"):
            messagebox.showwarning(
                "გადანაწილება მიმდინარეობს",
                "მიმდინარე გადანაწილების დასრულებამდე ისტორიის წაშლა შეუძლებელია.",
                parent=hw,
            )
            return
        sel = lb.curselection()
        if not sel or sel[0] >= len(loaded):
            messagebox.showwarning("მონიშვნა საჭიროა", "ჯერ მონიშნე წასაშლელი ჩანაწერი.", parent=hw)
            return
        idx = int(sel[0])
        path, data = loaded[idx]
        source_name = Path(str(data.get("source_folder") or path.stem)).name
        if not messagebox.askyesno(
            "მონიშნული ისტორიის წაშლა",
            f"წაიშალოს მონიშნული ისტორია?\n\n{source_name}\n{path.name}\n\n"
            "წაიშლება ამ ჩანაწერის Resume პროგრესიც. ფოტოები და შედეგების საქაღალდეები არ წაიშლება.",
            parent=hw,
        ):
            return
        _, errors = _delete_history_record(path)
        if errors:
            messagebox.showerror("წაშლა ვერ დასრულდა", "\n".join(errors[:8]), parent=hw)
            return
        loaded.pop(idx)
        _refresh_router_history_list(idx)
        append_log(f"Router ისტორია წაიშალა: {path.name}", "warning")

    def on_delete_all() -> None:
        if _history_delete_blocked("router"):
            messagebox.showwarning(
                "გადანაწილება მიმდინარეობს",
                "მიმდინარე გადანაწილების დასრულებამდე ისტორიის წაშლა შეუძლებელია.",
                parent=hw,
            )
            return
        all_paths = _list_history_state_files(ROUTER_STATE_DIR)
        if not all_paths:
            messagebox.showinfo("ისტორია", "წასაშლელი ისტორია არ არის.", parent=hw)
            return
        if not messagebox.askyesno(
            "ყველა ისტორიის წაშლა",
            f"წაიშალოს გადანაწილების ყველა ისტორია ({len(all_paths)} ჩანაწერი)?\n\n"
            "წაიშლება Resume პროგრესიც. ფოტოები და შედეგების საქაღალდეები არ წაიშლება.",
            parent=hw,
        ):
            return
        all_errors: List[str] = []
        for path in all_paths:
            _, errors = _delete_history_record(path)
            all_errors.extend(errors)
        loaded.clear()
        _refresh_router_history_list()
        append_log("Router-ის ყველა ისტორია წაიშალა", "warning")
        if all_errors:
            messagebox.showwarning("ნაწილობრივ წაიშალა", "\n".join(all_errors[:10]), parent=hw)

    lb.bind("<<ListboxSelect>>", on_select)
    lb.bind("<Double-Button-1>", on_use_and_start)

    btn_row = Frame(history_page, bg="#0B1120")
    btn_row.pack(pady=(4, 12))
    Button(btn_row, text="✅ მხოლოდ დაყენება", command=on_use,
           font=("Segoe UI", 10, "bold"), bg="#4D7CFF", fg="white",
           activebackground="#3A68E8", activeforeground="white",
           relief=FLAT, bd=0, padx=18, pady=10, cursor="hand2").pack(side=LEFT, padx=6)
    Button(btn_row, text="▶ დაყენება და გაშვება", command=on_use_and_start,
           font=("Segoe UI", 10, "bold"), bg="#2ECC7A", fg="#0B1120",
           activebackground="#28B86D", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=18, pady=10, cursor="hand2").pack(side=LEFT, padx=6)
    Button(btn_row, text="📄 JSON-ის გახსნა", command=on_view_json,
           font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#F5A623",
           activebackground="#F5A623", activeforeground="#0B1120",
           relief=FLAT, bd=0, padx=18, pady=10, cursor="hand2").pack(side=LEFT, padx=6)

    delete_row = Frame(history_page, bg="#0B1120")
    delete_row.pack(pady=(0, 14))
    Button(delete_row, text="🗑 მონიშნულის წაშლა", command=on_delete_selected,
           font=("Segoe UI", 10, "bold"), bg="#C0392B", fg="white",
           activebackground="#A93226", activeforeground="white",
           relief=FLAT, bd=0, padx=18, pady=10, cursor="hand2").pack(side=LEFT, padx=6)
    Button(delete_row, text="⚠ ყველა ისტორიის წაშლა", command=on_delete_all,
           font=("Segoe UI", 10, "bold"), bg="#5B2333", fg="white",
           activebackground="#7A2E43", activeforeground="white",
           relief=FLAT, bd=0, padx=18, pady=10, cursor="hand2").pack(side=LEFT, padx=6)


def _open_history_window(parent_window, mode: str = "scan") -> None:
    """Unified tabbed history window: Scan tab and Router tab."""
    from tkinter import Listbox

    hw = Toplevel(parent_window)
    hw.title("ისტორია")
    hw.configure(bg="#0B1120")
    register_window_theme(hw, "history")
    ww, wh = configure_responsive_geometry(
        hw, 1060, 760, minimum_width=640, minimum_height=480,
        width_ratio=0.94, height_ratio=0.90,
    )
    history_page, _history_canvas = create_scrollable_page(hw, minimum_content_width=620, horizontal=True)

    # ---- Header ----
    Label(history_page, text="🕓  ისტორია", font=("Segoe UI", 15, "bold"),
          fg="#E8EDF5", bg="#0B1120").pack(pady=(14, 2))
    Label(history_page, text="ერთჯერ = დეტალები  •  მონიშნე ჩანაწერი და დააჭირე დაყენებას ან გაშვებას",
          font=("Segoe UI", 9), fg="#7B93B8", bg="#0B1120").pack(pady=(0, 4))
    Frame(history_page, height=1, bg="#1F2D4A").pack(fill=X, padx=16, pady=(0, 4))

    # ---- Tab bar ----
    TAB_SCAN = "scan"
    TAB_ROUTER = "router"
    _active_tab = [TAB_SCAN if mode != "router" else TAB_ROUTER]

    tab_bar = Frame(history_page, bg="#0B1120")
    tab_bar.pack(fill=X, padx=16, pady=(0, 4))

    btn_scan = Button(tab_bar, text="  🔍  სკანირება  ",
                      font=("Segoe UI", 10, "bold"), relief=FLAT, bd=0,
                      padx=18, pady=8, cursor="hand2")
    btn_scan.pack(side=LEFT, padx=(0, 4))

    btn_router = Button(tab_bar, text="  👥  გადანაწილება  ",
                        font=("Segoe UI", 10, "bold"), relief=FLAT, bd=0,
                        padx=18, pady=8, cursor="hand2")
    btn_router.pack(side=LEFT)

    # ---- Content area (shared) ----
    content_area = Frame(history_page, bg="#0B1120")
    content_area.pack(fill=BOTH, expand=True, padx=16)

    list_frame = Frame(content_area, bg="#0B1120")
    list_frame.pack(fill=BOTH, expand=True)

    list_sb = Scrollbar(list_frame)
    list_sb.pack(side=RIGHT, fill=Y)

    lb = Listbox(list_frame, bg="#131C2E", fg="#E8EDF5",
                 font=("Courier", 9), selectbackground="#4D7CFF",
                 selectforeground="#131C2E", relief=FLAT, bd=0,
                 yscrollcommand=list_sb.set, activestyle="none")
    lb.pack(side=LEFT, fill=BOTH, expand=True)
    list_sb.config(command=lb.yview)

    detail_lbl = Label(history_page, text="", font=("Segoe UI", 9), fg="#F5A623",
                       bg="#0B1120", wraplength=ww - 40, justify=LEFT, anchor="w")
    detail_lbl.pack(fill=X, padx=16, pady=(4, 2))

    btn_row = Frame(history_page, bg="#0B1120")
    btn_row.pack(pady=(4, 12))
    use_btn = Button(btn_row, text="✅  მხოლოდ პარამეტრების დაყენება",
                     font=("Segoe UI", 10, "bold"), bg="#4D7CFF", fg="#E8EDF5",
                     activebackground="#3A68E8", activeforeground="#E8EDF5",
                     relief=FLAT, bd=0, padx=16, pady=10, cursor="hand2")
    use_btn.pack(side=LEFT, padx=5)
    run_btn = Button(btn_row, text="▶  დაყენება და გაშვება",
                     font=("Segoe UI", 10, "bold"), bg="#2ECC7A", fg="#0B1120",
                     activebackground="#28B86D", activeforeground="#0B1120",
                     relief=FLAT, bd=0, padx=16, pady=10, cursor="hand2")
    run_btn.pack(side=LEFT, padx=5)
    json_btn = Button(btn_row, text="📄 JSON-ის გახსნა",
                      font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#F5A623",
                      activebackground="#F5A623", activeforeground="#131C2E",
                      relief=FLAT, bd=0, padx=16, pady=10, cursor="hand2")
    json_btn.pack(side=LEFT, padx=5)

    delete_row = Frame(history_page, bg="#0B1120")
    delete_row.pack(pady=(0, 12))
    delete_selected_btn = Button(
        delete_row, text="🗑  მონიშნულის წაშლა",
        font=("Segoe UI", 10, "bold"), bg="#C0392B", fg="white",
        activebackground="#A93226", activeforeground="white",
        relief=FLAT, bd=0, padx=16, pady=10, cursor="hand2",
    )
    delete_selected_btn.pack(side=LEFT, padx=5)
    delete_all_btn = Button(
        delete_row, text="⚠  ყველა ისტორიის წაშლა",
        font=("Segoe UI", 10, "bold"), bg="#5B2333", fg="white",
        activebackground="#7A2E43", activeforeground="white",
        relief=FLAT, bd=0, padx=16, pady=10, cursor="hand2",
    )
    delete_all_btn.pack(side=LEFT, padx=5)

    # ---- Loaded data per tab ----
    scan_loaded: List[Tuple[Path, Dict[str, Any]]] = []
    router_loaded: List[Tuple[Path, Dict[str, Any]]] = []

    def _load_scan_entries():
        scan_loaded.clear()
        files = _list_history_state_files(SCAN_STATE_DIR)
        for p in files:
            try:
                data = load_json(p)
                if not isinstance(data, dict):
                    continue
                scan_loaded.append((p, data))
            except Exception as e:
                logger.warning(f"History scan load {p}: {e}")

    def _load_router_entries():
        router_loaded.clear()
        files = _list_history_state_files(ROUTER_STATE_DIR)
        for p in files:
            try:
                data = load_json(p)
                if not isinstance(data, dict):
                    continue
                router_loaded.append((p, data))
            except Exception as e:
                logger.warning(f"History router load {p}: {e}")

    def _render_scan_list():
        lb.delete(0, END)
        detail_lbl.config(text="")
        if not scan_loaded:
            lb.insert(END, "  სკანირების ისტორია ვერ მოიძებნა")
            return
        for p, data in scan_loaded:
            src    = data.get("source_folder", "?")
            updated = data.get("last_scan_finished_at") or data.get("updated_at", "?")
            thr    = data.get("last_threshold", "?")
            wc     = data.get("last_worker_count", "?")
            status = data.get("last_scan_status", "")
            st_icon = "✅" if status == "completed" else ("⏸" if status == "cancelled" else "•")
            stats  = data.get("stats", {})
            total_f   = stats.get("total") or sum(len(v) for v in data.get("folders", {}).values())
            matched_f = stats.get("matched") or sum(
                1 for v in data.get("folders", {}).values()
                for m in v.values() if isinstance(m, dict) and m.get("status") == "matched")
            refs = data.get("last_reference_files") or []
            ref_names = ", ".join(Path(r).name for r in refs[:2])
            if len(refs) > 2:
                ref_names += f" +{len(refs)-2}"
            lb.insert(END,
                f"  {st_icon} {Path(src).name if src not in ('?','') else p.stem}"
                f"  |  matched: {matched_f}/{total_f}"
                f"  |  ზღვარი: {thr}  |  workers: {wc}"
                f"  |  refs: {ref_names or '?'}"
                f"  |  {updated}")

    def _render_router_list():
        lb.delete(0, END)
        detail_lbl.config(text="")
        if not router_loaded:
            lb.insert(END, "  გადანაწილების ისტორია ვერ მოიძებნა")
            return
        for p, data in router_loaded:
            src        = data.get("source_folder", "?")
            updated    = data.get("last_scan_finished_at") or data.get("updated_at", "?")
            thr        = data.get("threshold", "?")
            wc         = data.get("last_worker_count", "?")
            slot_count = data.get("slot_count", "?")
            status     = data.get("last_scan_status", "")
            st_icon    = "✅" if status == "completed" else ("⏸" if status == "cancelled" else "•")
            stats      = data.get("stats", {})
            matched    = stats.get("matched", 0)
            total      = stats.get("total", len(data.get("files", {})))
            # ref names from slots
            ref_names  = ", ".join(
                s.get("ref_name") or Path(s.get("ref_path","?")).name
                for s in data.get("slots", [])[:3])
            if len(data.get("slots", [])) > 3:
                ref_names += f" +{len(data['slots'])-3}"
            lb.insert(END,
                f"  {st_icon} {Path(src).name if src not in ('?','') else p.stem}"
                f"  |  ბლოკები: {slot_count}  |  ზღვარი: {thr}  |  workers: {wc}"
                f"  |  matched: {matched}/{total}"
                f"  |  refs: {ref_names or '?'}"
                f"  |  {updated}")

    def _set_tab(tab: str):
        _active_tab[0] = tab
        colors = get_window_design_colors("history")

        def set_button_state(button: Any, bg_role: str, fg_role: str) -> None:
            roles = getattr(button, "_gui_theme_roles", {})
            if not isinstance(roles, dict):
                roles = {}
            roles["background"] = bg_role
            roles["foreground"] = fg_role
            button._gui_theme_roles = roles  # type: ignore[attr-defined]
            button.config(bg=colors[bg_role], fg=colors[fg_role])

        if tab == TAB_SCAN:
            set_button_state(btn_scan, "accent", "on_accent")
            set_button_state(btn_router, "card", "text")
            _render_scan_list()
        else:
            set_button_state(btn_router, "accent", "on_accent")
            set_button_state(btn_scan, "card", "text")
            _render_router_list()

    def on_select(event=None):
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        loaded = scan_loaded if _active_tab[0] == TAB_SCAN else router_loaded
        if idx >= len(loaded):
            return
        _, data = loaded[idx]
        if _active_tab[0] == TAB_SCAN:
            refs = data.get("last_reference_files") or []
            stats = data.get("stats", {})
            tf = stats.get("total") or sum(len(v) for v in data.get("folders", {}).values())
            mf = stats.get("matched") or sum(
                1 for v in data.get("folders", {}).values()
                for m in v.values() if isinstance(m, dict) and m.get("status") == "matched")
            dupes = stats.get("duplicates", "?")
            errs  = stats.get("errors", "?")
            wc    = data.get("last_worker_count", "?")
            parts = [
                f"📁 source: {data.get('source_folder', '?')}",
                f"📤 output: {data.get('last_output_folder', '?')}",
                f"🎯 ზღვარი: {data.get('last_threshold', '?')}",
                f"⚙️ workers: {wc}",
                f"👤 refs ({len(refs)}): {', '.join(Path(r).name for r in refs[:4])}"
                + (f" +{len(refs) - 4}" if len(refs) > 4 else ""),
                f"📊 სულ: {tf}  ✅ matched: {mf}  🔁 dupes: {dupes}  ❌ errors: {errs}",
                f"🕐 დასრულდა: {data.get('last_scan_finished_at', data.get('updated_at', '?'))}",
            ]
        else:
            stats = data.get("stats", {})
            matched  = stats.get("matched", 0)
            total    = stats.get("total", len(data.get("files", {})))
            unmatched= stats.get("unmatched", 0)
            errs     = stats.get("errors", 0)
            wc       = data.get("last_worker_count", "?")
            parts = [
                f"📁 source: {data.get('source_folder', '?')}",
                f"🎯 ზღვარი: {data.get('threshold', '?')}",
                f"🔢 ბლოკები: {data.get('slot_count', '?')}",
                f"⚙️ workers: {wc}",
            ]
            for s in data.get("slots", []):
                rname = s.get("ref_name") or Path(s.get("ref_path","?")).name
                parts.append(f"  #{s.get('index','?')}: {rname} → {s.get('out_folder','?')}")
            per_slot = data.get("stats_per_slot", {})
            if per_slot:
                parts.append("📊 per-slot: " + "  ".join(f"#{k}:{v}" for k, v in sorted(per_slot.items())))
            parts.append(f"✅ matched: {matched}/{total}  🚫 unmatched: {unmatched}  ❌ errors: {errs}")
            parts.append(f"🕐 დასრულდა: {data.get('last_scan_finished_at', data.get('updated_at','?'))}")
        detail_lbl.config(text="   ".join(parts[:8]))

    def _selected_history_entry() -> Optional[Tuple[Path, Dict[str, Any]]]:
        """Return the currently selected history entry."""
        sel = lb.curselection()
        if not sel:
            messagebox.showwarning(
                "მონიშვნა საჭიროა",
                "ჯერ ისტორიიდან მონიშნე ჩანაწერი.",
                parent=hw,
            )
            return None

        idx = sel[0]
        loaded = scan_loaded if _active_tab[0] == TAB_SCAN else router_loaded
        if idx >= len(loaded):
            messagebox.showerror("შეცდომა", "მონიშნული ჩანაწერი ვერ მოიძებნა.", parent=hw)
            return None
        return loaded[idx]

    def _scan_history_param(data: Dict[str, Any], param_name: str,
                            legacy_name: str, default: Any = None) -> Any:
        """Read both the new params block and the legacy last_* fields."""
        params = data.get("params", {})
        if isinstance(params, dict):
            value = params.get(param_name)
            if value not in (None, "", []):
                return value
        value = data.get(legacy_name, default)
        return default if value in (None, "") else value

    def _restore_scan_history(data: Dict[str, Any]) -> bool:
        """Restore a selected scan completely and validate it before use."""
        if state.scan_running:
            messagebox.showwarning(
                "სკანირება მიმდინარეობს",
                "ისტორიის დაყენებამდე მიმდინარე სკანირება გააჩერე.",
                parent=hw,
            )
            return False

        src_value = data.get("source_folder", "")
        out_value = _scan_history_param(data, "output_folder", "last_output_folder", "")
        threshold_value = _scan_history_param(
            data, "threshold", "last_threshold", config.threshold_default / 100
        )
        worker_value = _scan_history_param(
            data, "worker_count", "last_worker_count", config.worker_count
        )
        refs_value = _scan_history_param(
            data, "reference_files", "last_reference_files", []
        )

        if not src_value:
            messagebox.showerror("შეცდომა", "ისტორიაში წყაროს საქაღალდე არ არის შენახული.", parent=hw)
            return False

        src_path = Path(str(src_value)).expanduser()
        if not src_path.is_dir():
            messagebox.showerror(
                "წყარო ვერ მოიძებნა",
                f"წყაროს საქაღალდე აღარ არსებობს:\n{src_path}",
                parent=hw,
            )
            return False

        if not out_value:
            messagebox.showerror("შეცდომა", "ისტორიაში შედეგის საქაღალდე არ არის შენახული.", parent=hw)
            return False

        out_path = Path(str(out_value)).expanduser()
        try:
            out_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "შედეგის საქაღალდე",
                f"შედეგის საქაღალდე ვერ შეიქმნა:\n{out_path}\n\n{exc}",
                parent=hw,
            )
            return False

        src_resolved = src_path.resolve()
        out_resolved = out_path.resolve()
        if src_resolved == out_resolved:
            messagebox.showerror(
                "შეცდომა",
                "წყაროს და შედეგის საქაღალდეები ერთნაირი ვერ იქნება.",
                parent=hw,
            )
            return False
        try:
            out_resolved.relative_to(src_resolved)
            messagebox.showerror(
                "შეცდომა",
                "შედეგის საქაღალდე წყაროს საქაღალდეში არ უნდა იყოს.",
                parent=hw,
            )
            return False
        except ValueError:
            pass

        if isinstance(refs_value, (str, Path)):
            refs = [str(refs_value)]
        elif isinstance(refs_value, (list, tuple)):
            refs = [str(p) for p in refs_value if p]
        else:
            refs = []

        if not refs:
            messagebox.showerror(
                "საცნობარო ფოტოები",
                "ამ ისტორიის ჩანაწერში საცნობარო ფოტოები არ არის შენახული.",
                parent=hw,
            )
            return False

        detail_lbl.config(text="⏳ მონიშნული ისტორიის საცნობარო ფოტოები იტვირთება...")
        hw.update_idletasks()

        ok_embs: List[np.ndarray] = []
        ok_files: List[str] = []
        failed_refs: List[str] = []
        for ref_path_value in refs:
            ref_path = Path(ref_path_value).expanduser()
            if not ref_path.is_file():
                failed_refs.append(f"{ref_path.name}: ფაილი აღარ არსებობს")
                continue
            try:
                ok_embs.append(get_emb(ref_path))
                ok_files.append(str(ref_path.resolve()))
            except Exception as exc:
                logger.warning(f"History ref load fail: {ref_path}: {exc}")
                failed_refs.append(f"{ref_path.name}: {exc}")

        if not ok_embs:
            preview = "\n".join(failed_refs[:8]) or "არცერთი ფოტო ვერ ჩაიტვირთა"
            messagebox.showerror(
                "საცნობარო ფოტოები ვერ ჩაიტვირთა",
                f"სკანირების გაშვება შეუძლებელია.\n\n{preview}",
                parent=hw,
            )
            return False

        # Rebuild the exact identity grouping saved by v4+, rather than treating
        # every reference photo as a different person.
        emb_by_path = {str(Path(path).resolve()): emb for path, emb in zip(ok_files, ok_embs)}
        identity_defs = data.get("identities", [])
        restored_identities: List[Dict[str, Any]] = []
        if isinstance(identity_defs, list):
            for fallback_index, identity in enumerate(identity_defs, start=1):
                if not isinstance(identity, dict):
                    continue
                identity_files = identity.get("files", [])
                if isinstance(identity_files, (str, Path)):
                    identity_files = [identity_files]
                valid_identity_files: List[str] = []
                identity_embeddings: List[np.ndarray] = []
                for identity_file in identity_files if isinstance(identity_files, (list, tuple)) else []:
                    try:
                        resolved = str(Path(str(identity_file)).expanduser().resolve())
                    except Exception:
                        continue
                    embedding = emb_by_path.get(resolved)
                    if embedding is not None:
                        valid_identity_files.append(resolved)
                        identity_embeddings.append(embedding)
                if not identity_embeddings:
                    continue
                matrix, centroid = _identity_profile(identity_embeddings)
                restored_identities.append({
                    "index": int(identity.get("index", fallback_index)),
                    "name": str(identity.get("name") or f"ადამიანი #{fallback_index}"),
                    "files": valid_identity_files,
                    "embeddings": identity_embeddings,
                    "matrix": matrix,
                    "centroid": centroid,
                })

        if not restored_identities:
            for index, (path, embedding) in enumerate(zip(ok_files, ok_embs), start=1):
                matrix, centroid = _identity_profile([embedding])
                restored_identities.append({
                    "index": index,
                    "name": Path(path).stem,
                    "files": [path],
                    "embeddings": [embedding],
                    "matrix": matrix,
                    "centroid": centroid,
                })

        try:
            threshold_percent = int(round(float(threshold_value) * 100))
        except (TypeError, ValueError):
            threshold_percent = config.threshold_default
        threshold_percent = max(config.threshold_min, min(config.threshold_max, threshold_percent))

        try:
            restored_workers = int(worker_value)
        except (TypeError, ValueError):
            restored_workers = config.worker_count
        restored_workers = max(config.worker_min, min(config.worker_max, restored_workers))

        state.src_folder = str(src_resolved)
        state.out_folder = str(out_resolved)
        state.ref_embs = ok_embs
        state.ref_embs_matrix = np.vstack(ok_embs).astype(np.float32)
        state.ref_files = ok_files
        state.ref_db_value = build_reference_db_value(ok_files)
        state.main_identities = restored_identities
        state.current_ref_signature = ref_signature(ok_files)
        state.reference_model_signature = _current_model_signature()
        state.reference_content_signature = ref_signature(state.ref_files)

        slider.set(threshold_percent)
        worker_var.set(str(restored_workers))
        config.worker_count = restored_workers
        lbl_src.config(text=f"წყარო: {state.src_folder}")
        lbl_out.config(text=f"შედეგი: {state.out_folder}")
        lbl_ref.config(
            text=f"ჩატვირთულია {len(ok_embs)} საცნობარო ფოტო (ისტორიიდან)"
            + (f" • გამოტოვებულია {len(failed_refs)}" if failed_refs else "")
        )
        progress_label.config(text="ისტორიის პარამეტრები დაყენებულია — მზადაა გასაშვებად.")
        save_app_settings()
        return True

    def on_use(event=None):
        selected = _selected_history_entry()
        if selected is None:
            return

        _, data = selected
        if _active_tab[0] != TAB_SCAN:
            controller = globals().get("_ACTIVE_ROUTER_CONTROLLER", {})
            hw.destroy()
            if controller and callable(controller.get("restore")):
                root.after_idle(lambda d=data: controller["restore"](d, False))
            else:
                globals()["_PENDING_ROUTER_HISTORY_RESTORE"] = {"data": data, "auto_start": False}
                root.after_idle(open_multi_person_router)
            return

        if not _restore_scan_history(data):
            return

        messagebox.showinfo(
            "დაყენებულია",
            "მონიშნული ისტორიის ყველა პარამეტრი დაყენებულია.\n"
            "ახლა შეგიძლია დააჭირო „სკანირების დაწყებას“.",
            parent=hw,
        )
        hw.destroy()

    def on_use_and_start(event=None):
        selected = _selected_history_entry()
        if selected is None:
            return

        _, data = selected
        if _active_tab[0] != TAB_SCAN:
            controller = globals().get("_ACTIVE_ROUTER_CONTROLLER", {})
            hw.destroy()
            if controller and callable(controller.get("restore")):
                root.after_idle(lambda d=data: controller["restore"](d, True))
            else:
                globals()["_PENDING_ROUTER_HISTORY_RESTORE"] = {"data": data, "auto_start": True}
                root.after_idle(open_multi_person_router)
            return

        if not _restore_scan_history(data):
            return

        hw.destroy()
        root.after(150, start_scan)

    def on_view_json(event=None):
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        loaded = scan_loaded if _active_tab[0] == TAB_SCAN else router_loaded
        if idx >= len(loaded):
            return
        _view_json_file(loaded[idx][0])

    def _render_active_history(select_index: Optional[int] = None) -> None:
        if _active_tab[0] == TAB_SCAN:
            _render_scan_list()
            loaded = scan_loaded
        else:
            _render_router_list()
            loaded = router_loaded
        if select_index is not None and loaded:
            idx = max(0, min(int(select_index), len(loaded) - 1))
            lb.selection_set(idx)
            lb.activate(idx)
            lb.see(idx)
            on_select()

    def on_delete_selected() -> None:
        selected = _selected_history_entry()
        if selected is None:
            return
        mode_name = TAB_SCAN if _active_tab[0] == TAB_SCAN else TAB_ROUTER
        if _history_delete_blocked(mode_name):
            messagebox.showwarning(
                "პროცესი მიმდინარეობს",
                "მიმდინარე პროცესის დასრულებამდე შესაბამისი ისტორიის წაშლა შეუძლებელია.",
                parent=hw,
            )
            return
        sel = lb.curselection()
        if not sel:
            return
        idx = int(sel[0])
        path, data = selected
        source_name = Path(str(data.get("source_folder") or path.stem)).name
        history_type = "სკანირების" if mode_name == TAB_SCAN else "გადანაწილების"
        if not messagebox.askyesno(
            "მონიშნული ისტორიის წაშლა",
            f"წაიშალოს მონიშნული {history_type} ისტორია?\n\n{source_name}\n{path.name}\n\n"
            "წაიშლება ამ ჩანაწერის Resume პროგრესიც. ფოტოები, Output საქაღალდეები და face cache არ წაიშლება.",
            parent=hw,
        ):
            return
        _, errors = _delete_history_record(path)
        if errors:
            messagebox.showerror("წაშლა ვერ დასრულდა", "\n".join(errors[:8]), parent=hw)
            return
        loaded = scan_loaded if mode_name == TAB_SCAN else router_loaded
        if idx < len(loaded):
            loaded.pop(idx)
        _render_active_history(idx)
        append_log(f"ისტორიის ჩანაწერი წაიშალა: {path.name}", "warning")

    def on_delete_all() -> None:
        if state.scan_running or _history_delete_blocked("router"):
            messagebox.showwarning(
                "პროცესი მიმდინარეობს",
                "ყველა ისტორიის წაშლამდე მიმდინარე სკანირება ან გადანაწილება გააჩერე.",
                parent=hw,
            )
            return
        all_paths = _list_history_state_files(SCAN_STATE_DIR) + _list_history_state_files(ROUTER_STATE_DIR)
        if not all_paths:
            messagebox.showinfo("ისტორია", "წასაშლელი ისტორია არ არის.", parent=hw)
            return
        if not messagebox.askyesno(
            "ყველა ისტორიის წაშლა",
            f"წაიშალოს სკანირებისა და გადანაწილების ყველა ისტორია ({len(all_paths)} ჩანაწერი)?\n\n"
            "წაიშლება მათი Resume პროგრესიც. ფოტოები, Output საქაღალდეები და face cache არ წაიშლება.",
            parent=hw,
        ):
            return
        all_errors: List[str] = []
        for path in all_paths:
            _, errors = _delete_history_record(path)
            all_errors.extend(errors)
        scan_loaded.clear()
        router_loaded.clear()
        _render_active_history()
        append_log("სკანირებისა და Router-ის ყველა ისტორია წაიშალა", "warning")
        if all_errors:
            messagebox.showwarning("ნაწილობრივ წაიშალა", "\n".join(all_errors[:10]), parent=hw)

    lb.bind("<<ListboxSelect>>", on_select)
    lb.bind("<Double-Button-1>", on_use_and_start)
    use_btn.config(command=on_use)
    run_btn.config(command=on_use_and_start)
    json_btn.config(command=on_view_json)
    delete_selected_btn.config(command=on_delete_selected)
    delete_all_btn.config(command=on_delete_all)
    btn_scan.config(command=lambda: _set_tab(TAB_SCAN))
    btn_router.config(command=lambda: _set_tab(TAB_ROUTER))

    _load_scan_entries()
    _load_router_entries()
    _set_tab(_active_tab[0])


def _view_json_file(path: Path) -> None:
    jw = Toplevel()
    jw.title(f"JSON: {path.name}")
    jw.configure(bg="#0B1120")
    register_window_theme(jw, "json_viewer")
    configure_responsive_geometry(
        jw, 1020, 760, minimum_width=560, minimum_height=420,
        width_ratio=0.94, height_ratio=0.90,
    )
    try:
        content = path.read_text(encoding="utf-8")
    except Exception as e:
        messagebox.showerror("შეცდომა", str(e))
        jw.destroy()
        return
    viewer_shell = Frame(jw, bg="#0B1120")
    viewer_shell.pack(fill=BOTH, expand=True)
    viewer_shell.grid_rowconfigure(0, weight=1)
    viewer_shell.grid_columnconfigure(0, weight=1)
    jsb = Scrollbar(viewer_shell, orient=VERTICAL)
    jxsb = Scrollbar(viewer_shell, orient=HORIZONTAL)
    jt = Text(viewer_shell, bg="#0D1526", fg="#7B93B8", font=("Courier", 9),
              relief=FLAT, bd=0, wrap=NONE, yscrollcommand=jsb.set, xscrollcommand=jxsb.set)
    jt.grid(row=0, column=0, sticky="nsew")
    jsb.grid(row=0, column=1, sticky="ns")
    jxsb.grid(row=1, column=0, sticky="ew")
    jsb.config(command=jt.yview)
    jxsb.config(command=jt.xview)
    jt.insert(END, content)
    jt.config(state=DISABLED)

# ============================================================================
# PRO ACCURACY / SAFETY / AUTOMATION ENHANCEMENTS (v3)
# ============================================================================

APP_VERSION = "4.8"
FACE_ENGINE_CACHE_VERSION = "4.6"
APP_SETTINGS_PATH = BASE_DIR / "face_scanner_settings.json"

MODEL_PROFILES: Dict[str, Dict[str, Any]] = {
    "მაქსიმალური სიზუსტე": {"model": "buffalo_l", "det_size": (768, 768)},
    "დაბალანსებული": {"model": "buffalo_l", "det_size": (640, 640)},
    "სწრაფი": {"model": "buffalo_l", "det_size": (320, 320)},
    "მსუბუქი მოდელი": {"model": "buffalo_s", "det_size": (320, 320)},
}

FACE_ENGINE_LOCK = threading.RLock()
FACE_ENGINE_MODEL = "buffalo_l"
FACE_ENGINE_DET_SIZE: Tuple[int, int] = (int(config.det_size[0]), int(config.det_size[1]))
# Each worker thread owns its own FaceAnalysis instance (own ONNX Runtime
# sessions), so inference runs truly in parallel instead of being serialized
# behind a single shared lock. FACE_ENGINE_LOCK now only protects the small
# "what model/det_size is currently selected" bookkeeping below, never the
# actual app.get() inference call.
FACE_ENGINE_TLS = threading.local()
OPERATION_CONTEXT = threading.local()
LIVE_WATCH_LOCK = threading.Lock()
LIVE_WATCH_ALWAYS_ENABLED = True

try:
    from PIL import ImageOps, ImageTk
except Exception:
    ImageOps = None  # type: ignore
    ImageTk = None  # type: ignore

try:
    import pillow_heif  # type: ignore
    pillow_heif.register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False

try:
    import pillow_avif as _pillow_avif  # type: ignore  # registers AVIF support with Pillow
    AVIF_AVAILABLE = True
except Exception:
    AVIF_AVAILABLE = False

try:
    import rawpy  # type: ignore
    RAW_AVAILABLE = True
except Exception:
    RAW_AVAILABLE = False


def _load_settings_file() -> Dict[str, Any]:
    try:
        if not APP_SETTINGS_PATH.exists():
            return {}
        data = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning(f"Settings load failed: {exc}")
        return {}


def _safe_int(value: Any, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = int(default)
    if minimum is not None:
        result = max(int(minimum), result)
    if maximum is not None:
        result = min(int(maximum), result)
    return result


def _safe_float(value: Any, default: float, minimum: Optional[float] = None, maximum: Optional[float] = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = float(default)
    if minimum is not None:
        result = max(float(minimum), result)
    if maximum is not None:
        result = min(float(maximum), result)
    return result


def _global_tk_value(name: str, default: Any = None) -> Any:
    """Read a late-created Tk variable without unsafe optional member access."""
    variable = globals().get(name)
    getter = getattr(variable, "get", None)
    if not callable(getter):
        return default
    try:
        return getter()
    except Exception:
        return default


def _safe_det_size(value: Any, default: Tuple[int, int]) -> Tuple[int, int]:
    try:
        if isinstance(value, (list, tuple)) and len(value) == 2:
            width = _safe_int(value[0], default[0], 160, 2048)
            height = _safe_int(value[1], default[1], 160, 2048)
            return width, height
    except Exception:
        pass
    return int(default[0]), int(default[1])


def configure_responsive_geometry(
    window: Any,
    preferred_width: int,
    preferred_height: int,
    minimum_width: int = 520,
    minimum_height: int = 420,
    width_ratio: float = 0.96,
    height_ratio: float = 0.92,
) -> Tuple[int, int]:
    """Size and centre a Tk window without letting it exceed the current display."""
    window.update_idletasks()
    screen_width = max(640, int(window.winfo_screenwidth()))
    screen_height = max(480, int(window.winfo_screenheight()))
    usable_width = max(420, int(screen_width * max(0.55, min(0.99, width_ratio))))
    usable_height = max(340, int(screen_height * max(0.55, min(0.98, height_ratio))))
    width = max(minimum_width, min(int(preferred_width), usable_width))
    height = max(minimum_height, min(int(preferred_height), usable_height))
    width = min(width, usable_width)
    height = min(height, usable_height)
    x = max(0, (screen_width - width) // 2)
    y = max(0, (screen_height - height) // 2)
    window.geometry(f"{width}x{height}+{x}+{y}")
    window.minsize(min(width, minimum_width), min(height, minimum_height))
    return width, height


def _widget_can_scroll(widget: Any, horizontal: bool = False) -> bool:
    try:
        if not isinstance(widget, (Text, Listbox, Canvas)):
            return False
        fractions = widget.xview() if horizontal else widget.yview()
        return len(fractions) == 2 and (float(fractions[0]) > 0.0 or float(fractions[1]) < 1.0)
    except Exception:
        return False


def create_scrollable_page(
    window: Any,
    background: str = "#0B1120",
    minimum_content_width: int = 0,
    horizontal: bool = True,
) -> Tuple[Frame, Canvas]:
    """Create a responsive page with vertical and optional horizontal scrolling.

    Mouse-wheel events are routed to a nested Text/Listbox/Canvas first, so an
    inner log/list continues to scroll normally instead of fighting the page.
    """
    shell = Frame(window, bg=background)
    shell.pack(fill=BOTH, expand=True)
    shell.grid_rowconfigure(0, weight=1)
    shell.grid_columnconfigure(0, weight=1)

    canvas = Canvas(shell, bg=background, highlightthickness=0, bd=0)
    vertical_bar = Scrollbar(shell, orient=VERTICAL, command=canvas.yview)
    horizontal_bar = Scrollbar(shell, orient=HORIZONTAL, command=canvas.xview) if horizontal else None
    canvas.configure(yscrollcommand=vertical_bar.set)
    if horizontal_bar is not None:
        canvas.configure(xscrollcommand=horizontal_bar.set)
    canvas.grid(row=0, column=0, sticky="nsew")
    vertical_bar.grid(row=0, column=1, sticky="ns")
    if horizontal_bar is not None:
        horizontal_bar.grid(row=1, column=0, sticky="ew")

    page = Frame(canvas, bg=background)
    page_window = canvas.create_window((0, 0), window=page, anchor="nw")

    def refresh_region(_event: Any = None) -> None:
        try:
            canvas.configure(scrollregion=canvas.bbox("all"))
        except Exception:
            pass

    def fit_width(_event: Any = None) -> None:
        try:
            canvas_width = max(1, int(canvas.winfo_width()))
            requested = max(int(minimum_content_width), int(page.winfo_reqwidth()))
            target_width = max(canvas_width, requested) if horizontal else canvas_width
            canvas.itemconfigure(page_window, width=target_width)
            refresh_region()
        except Exception:
            pass

    def wheel(event: Any) -> str:
        horizontal_scroll = bool(getattr(event, "state", 0) & 0x0001)
        direction = 0
        if getattr(event, "num", None) == 4:
            direction = -1
        elif getattr(event, "num", None) == 5:
            direction = 1
        else:
            delta = int(getattr(event, "delta", 0) or 0)
            direction = -1 if delta > 0 else 1 if delta < 0 else 0
        if direction == 0:
            return "break"
        try:
            target = window.winfo_containing(event.x_root, event.y_root)
        except Exception:
            target = None
        current = target
        while current is not None and current is not window:
            if current is not canvas and _widget_can_scroll(current, horizontal_scroll):
                try:
                    if horizontal_scroll:
                        current.xview_scroll(direction * 3, "units")
                    else:
                        current.yview_scroll(direction * 3, "units")
                    return "break"
                except Exception:
                    break
            try:
                current = current.master
            except Exception:
                break
        try:
            if horizontal_scroll and horizontal_bar is not None:
                canvas.xview_scroll(direction * 3, "units")
            else:
                canvas.yview_scroll(direction * 3, "units")
        except Exception:
            pass
        return "break"

    page.bind("<Configure>", refresh_region, add="+")
    canvas.bind("<Configure>", fit_width, add="+")
    window.bind("<MouseWheel>", wheel, add="+")
    window.bind("<Button-4>", wheel, add="+")
    window.bind("<Button-5>", wheel, add="+")
    window.after_idle(fit_width)
    return page, canvas


APP_SETTINGS: Dict[str, Any] = _load_settings_file()
config.cpu_threshold = _safe_int(APP_SETTINGS.get("cpu_threshold"), config.cpu_threshold, 20, 100)
config.memory_threshold = _safe_int(APP_SETTINGS.get("memory_threshold"), config.memory_threshold, 20, 100)
config.worker_count = _safe_int(APP_SETTINGS.get("worker_count"), config.worker_count, config.worker_min, config.worker_max)
config.threshold_default = _safe_int(
    APP_SETTINGS.get("threshold"), config.threshold_default, config.threshold_min, config.threshold_max
)
_configured_det_size: Tuple[int, int] = (int(config.det_size[0]), int(config.det_size[1]))
config.det_size = _safe_det_size(  # pyright: ignore[reportGeneralTypeIssues]
    APP_SETTINGS.get("det_size"), _configured_det_size
)


# ----------- GUI DESIGN SYSTEM ------------
GUI_DESIGNS_PATH = BASE_DIR / "gui_designs.json"
GUI_DESIGN_VERSION = 2
DEFAULT_WINDOW_DESIGNS: Dict[str, str] = {
    "main": "midnight_aurora",
    "quality_checker": "midnight_aurora",
    "router": "midnight_aurora",
    "router_history": "midnight_aurora",
    "history": "midnight_aurora",
    "json_viewer": "midnight_aurora",
    "review_queue": "midnight_aurora",
}
GUI_DESIGNS: Dict[str, Dict[str, Any]] = {
    "midnight_aurora": {
        "name": "Midnight Aurora",
        "colors": {"bg": "#0B1120", "surface": "#131C2E", "card": "#1A2540", "editor": "#0D1526",
                   "border": "#1F2D4A", "muted_deep": "#3E5070", "text": "#E8EDF5", "muted": "#7B93B8",
                   "muted_light": "#9FB4D8", "accent": "#4D7CFF", "accent_hover": "#3A68E8", "accent_soft": "#7EA2FF",
                   "warning": "#F5A623", "danger": "#E05555", "danger_hover": "#C94848", "danger_dark": "#8A3B46",
                   "success": "#2ECC7A", "success_hover": "#28B86D", "success_dark": "#2E7D5B", "on_accent": "#FFFFFF"},
    },
    "royal_amethyst": {
        "name": "Royal Amethyst",
        "colors": {"bg": "#100B1E", "surface": "#1A1230", "card": "#261A43", "editor": "#140E27",
                   "border": "#3B2863", "muted_deep": "#665080", "text": "#F4EEFF", "muted": "#B2A3CC",
                   "muted_light": "#D2C4EA", "accent": "#9B6DFF", "accent_hover": "#8254E8", "accent_soft": "#C09CFF",
                   "warning": "#FFBD59", "danger": "#FF627D", "danger_hover": "#E84A68", "danger_dark": "#933B55",
                   "success": "#43D99B", "success_hover": "#31C087", "success_dark": "#28775C", "on_accent": "#FFFFFF"},
    },
    "emerald_noir": {
        "name": "Emerald Noir",
        "colors": {"bg": "#071713", "surface": "#0D241D", "card": "#123429", "editor": "#091E18",
                   "border": "#1D4A3B", "muted_deep": "#426B5E", "text": "#E9FFF6", "muted": "#83B5A3",
                   "muted_light": "#ADD5C7", "accent": "#27D89B", "accent_hover": "#1DB782", "accent_soft": "#70EDC1",
                   "warning": "#F7C85B", "danger": "#F05D6F", "danger_hover": "#D94A5D", "danger_dark": "#843944",
                   "success": "#4BE39D", "success_hover": "#30CA83", "success_dark": "#267557", "on_accent": "#061A14"},
    },
    "graphite_gold": {
        "name": "Graphite Gold",
        "colors": {"bg": "#111214", "surface": "#1A1C20", "card": "#25282D", "editor": "#15171A",
                   "border": "#3B3F46", "muted_deep": "#626872", "text": "#F5F2E9", "muted": "#AAA79F",
                   "muted_light": "#D4D0C5", "accent": "#D8AC4A", "accent_hover": "#BC9135", "accent_soft": "#F2CF78",
                   "warning": "#F0B84F", "danger": "#E35D62", "danger_hover": "#C9474D", "danger_dark": "#81383B",
                   "success": "#65C98B", "success_hover": "#4DB276", "success_dark": "#376E50", "on_accent": "#17130B"},
    },
    "arctic_glass": {
        "name": "Arctic Glass",
        "colors": {"bg": "#EDF4FA", "surface": "#FFFFFF", "card": "#DDEAF4", "editor": "#F7FBFE",
                   "border": "#B7CEDF", "muted_deep": "#6E8799", "text": "#172B3A", "muted": "#526E82",
                   "muted_light": "#36576F", "accent": "#1677C8", "accent_hover": "#0E63AB", "accent_soft": "#5AA7E1",
                   "warning": "#A96900", "danger": "#C83E50", "danger_hover": "#A92D3E", "danger_dark": "#7B3240",
                   "success": "#16845B", "success_hover": "#0D6D49", "success_dark": "#245C49", "on_accent": "#FFFFFF"},
    },
    "ocean_neon": {
        "name": "Ocean Neon",
        "colors": {"bg": "#051622", "surface": "#082434", "card": "#0C3245", "editor": "#071D2A",
                   "border": "#14506A", "muted_deep": "#39768C", "text": "#EAFBFF", "muted": "#82B6C6",
                   "muted_light": "#B0D7E1", "accent": "#00BFEA", "accent_hover": "#009BC4", "accent_soft": "#55DCF5",
                   "warning": "#FFB64D", "danger": "#FF5B72", "danger_hover": "#E6425C", "danger_dark": "#8C3445",
                   "success": "#2DDBA4", "success_hover": "#1FB98A", "success_dark": "#27745D", "on_accent": "#03151E"},
    },
    "crimson_velvet": {
        "name": "Crimson Velvet",
        "colors": {"bg": "#190A0E", "surface": "#291017", "card": "#3B1721", "editor": "#200C12",
                   "border": "#5A2431", "muted_deep": "#7D4B57", "text": "#FFF0F3", "muted": "#CAA0AA",
                   "muted_light": "#E4C3CB", "accent": "#F04F6D", "accent_hover": "#D63857", "accent_soft": "#FF8EA2",
                   "warning": "#F4B95C", "danger": "#FF4B55", "danger_hover": "#DC343F", "danger_dark": "#8D3038",
                   "success": "#4ED392", "success_hover": "#36B779", "success_dark": "#2D7354", "on_accent": "#FFFFFF"},
    },
    "sakura_night": {
        "name": "Sakura Night",
        "colors": {"bg": "#160F1A", "surface": "#241828", "card": "#342239", "editor": "#1C1320",
                   "border": "#503457", "muted_deep": "#765D7B", "text": "#FFF2FA", "muted": "#C3A6BD",
                   "muted_light": "#E1C9DC", "accent": "#F071B5", "accent_hover": "#D7559A", "accent_soft": "#F5A4D0",
                   "warning": "#F4BD64", "danger": "#EE627A", "danger_hover": "#D74A65", "danger_dark": "#8A3E54",
                   "success": "#55D3A1", "success_hover": "#3CB987", "success_dark": "#2E7159", "on_accent": "#FFFFFF"},
    },
    "cyber_cyan": {
        "name": "Cyber Cyan",
        "colors": {"bg": "#020C13", "surface": "#071923", "card": "#0C2935", "editor": "#04131C",
                   "border": "#0E5366", "muted_deep": "#2D6F7D", "text": "#E8FEFF", "muted": "#78B9C2",
                   "muted_light": "#A9E2E7", "accent": "#00E5FF", "accent_hover": "#00BDD4", "accent_soft": "#6DF3FF",
                   "warning": "#FFD166", "danger": "#FF5C77", "danger_hover": "#E53E5E", "danger_dark": "#8A3245",
                   "success": "#29E6A7", "success_hover": "#19C98D", "success_dark": "#1F7257", "on_accent": "#001217"},
    },
    "sunset_coral": {
        "name": "Sunset Coral",
        "colors": {"bg": "#1B1012", "surface": "#2A181B", "card": "#3A2226", "editor": "#211316",
                   "border": "#624047", "muted_deep": "#815F66", "text": "#FFF4EF", "muted": "#D0A8A0",
                   "muted_light": "#E8C8C0", "accent": "#FF7A59", "accent_hover": "#E85D3D", "accent_soft": "#FFAD91",
                   "warning": "#FFC857", "danger": "#F24E65", "danger_hover": "#D83A51", "danger_dark": "#843541",
                   "success": "#57D39B", "success_hover": "#3DBA82", "success_dark": "#2D7156", "on_accent": "#FFFFFF"},
    },
    "royal_sapphire": {
        "name": "Royal Sapphire",
        "colors": {"bg": "#071025", "surface": "#0C1B38", "card": "#142A50", "editor": "#09162E",
                   "border": "#244A82", "muted_deep": "#4D6F9E", "text": "#EEF5FF", "muted": "#91AED4",
                   "muted_light": "#BDD1EC", "accent": "#3E8BFF", "accent_hover": "#226ED8", "accent_soft": "#7CB3FF",
                   "warning": "#F7C55E", "danger": "#EF6077", "danger_hover": "#D74660", "danger_dark": "#823A4B",
                   "success": "#42D6A0", "success_hover": "#2EB987", "success_dark": "#28725A", "on_accent": "#FFFFFF"},
    },
    "forest_lime": {
        "name": "Forest Lime",
        "colors": {"bg": "#0B140B", "surface": "#142014", "card": "#213221", "editor": "#101B10",
                   "border": "#3B5538", "muted_deep": "#62775E", "text": "#F2FFE9", "muted": "#A6C09B",
                   "muted_light": "#CDE2C5", "accent": "#8EDB55", "accent_hover": "#70BC3B", "accent_soft": "#B7ED8D",
                   "warning": "#F3C85B", "danger": "#EB6170", "danger_hover": "#D44959", "danger_dark": "#843A44",
                   "success": "#55D98D", "success_hover": "#3FBE75", "success_dark": "#337052", "on_accent": "#102008"},
    },
}

_LEGACY_COLOR_ROLES: Dict[str, str] = {
    "#0B1120": "bg", "#131C2E": "surface", "#1A2540": "card", "#0D1526": "editor",
    "#1F2D4A": "border", "#263451": "border", "#3E5070": "muted_deep",
    "#E8EDF5": "text", "#7B93B8": "muted", "#9FB4D8": "muted_light",
    "#4D7CFF": "accent", "#3A68E8": "accent_hover", "#7EA2FF": "accent_soft",
    "#F5A623": "warning", "#E05555": "danger", "#C94848": "danger_hover", "#8A3B46": "danger_dark",
    "#2ECC7A": "success", "#28B86D": "success_hover", "#2E7D5B": "success_dark",
    "WHITE": "on_accent", "#FFFFFF": "on_accent",
}


def _valid_hex_color(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value.strip()) is not None


def _load_gui_design_state() -> Dict[str, Any]:
    default = {
        "version": GUI_DESIGN_VERSION,
        "available_designs": [],
        "window_designs": dict(DEFAULT_WINDOW_DESIGNS),
    }
    try:
        if GUI_DESIGNS_PATH.exists():
            loaded = json.loads(GUI_DESIGNS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                default.update(loaded)
    except Exception as exc:
        logger.warning(f"GUI designs load failed: {exc}")
    selections = default.get("window_designs")
    merged_selections = dict(DEFAULT_WINDOW_DESIGNS)
    if isinstance(selections, dict):
        merged_selections.update({str(key): str(value) for key, value in selections.items()})
    default["window_designs"] = merged_selections
    return default


GUI_DESIGN_STATE: Dict[str, Any] = _load_gui_design_state()  # pyright: ignore[reportGeneralTypeIssues]
_REGISTERED_THEME_WINDOWS: Dict[str, Any] = {}


def _save_gui_design_state() -> None:
    payload = {
        "version": GUI_DESIGN_VERSION,
        "available_designs": [
            {"id": theme_id, "name": theme["name"], "colors": dict(theme["colors"])}
            for theme_id, theme in GUI_DESIGNS.items()
        ],
        "window_designs": dict(GUI_DESIGN_STATE.get("window_designs", {})),
        "updated_at": now_iso(),
    }
    save_json_atomic(GUI_DESIGNS_PATH, payload)
    GUI_DESIGN_STATE.clear()
    GUI_DESIGN_STATE.update(payload)


def _theme_color_role(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    # Roles are captured from the original built-in palette once per widget.
    # This avoids ambiguous colors such as #FFFFFF being a surface in one
    # design and button text in another design.
    return _LEGACY_COLOR_ROLES.get(value.strip().upper())




def get_window_design_colors(window_key: str) -> Dict[str, str]:
    theme_id = str(GUI_DESIGN_STATE.get("window_designs", {}).get(window_key, "midnight_aurora"))
    if theme_id not in GUI_DESIGNS:
        theme_id = "midnight_aurora"
    return GUI_DESIGNS[theme_id]["colors"]


def _bind_theme_button_hover(widget: Any, colors: Dict[str, str], role_map: Dict[str, str]) -> None:
    base_bg = str(role_map.get("background", "card"))
    base_fg = str(role_map.get("foreground", "text"))

    def hover_roles() -> Tuple[str, str]:
        if base_bg in {"accent", "accent_hover", "accent_soft"}:
            return "accent_hover", "on_accent"
        if base_bg in {"danger", "danger_hover", "danger_dark"}:
            return "danger_hover", "on_accent"
        if base_bg in {"success", "success_hover", "success_dark"}:
            return "success_hover", "on_accent"
        if base_bg == "warning":
            return "warning", "bg"
        if base_fg in {"warning"}:
            return "warning", "bg"
        if base_fg in {"danger", "danger_dark"}:
            return "danger", "on_accent"
        if base_fg in {"success", "success_dark"}:
            return "success", "on_accent"
        return "accent", "on_accent"

    def on_enter(_event: Any = None) -> None:
        try:
            if str(widget.cget("state")) == str(DISABLED):
                return
        except Exception:
            pass
        bg_role, fg_role = hover_roles()
        try:
            widget.configure(background=colors[bg_role], foreground=colors[fg_role])
        except Exception:
            pass

    def on_leave(_event: Any = None) -> None:
        try:
            widget.configure(  # pyright: ignore[reportCallIssue]
                background=colors.get(base_bg, colors["card"]),
                foreground=colors.get(base_fg, colors["text"]),
            )
        except Exception:
            pass

    try:
        widget.bind("<Enter>", on_enter)
        widget.bind("<Leave>", on_leave)
    except Exception:
        pass

def _configure_widget_theme(widget: Any, colors: Dict[str, str], window_key: str) -> None:
    option_names = (
        "background", "foreground", "activebackground", "activeforeground", "selectcolor",
        "troughcolor", "highlightbackground", "highlightcolor", "insertbackground", "disabledforeground",
        "selectbackground", "selectforeground",
    )
    role_map = getattr(widget, "_gui_theme_roles", None)
    if not isinstance(role_map, dict):
        role_map = {}
    for option in option_names:
        role = role_map.get(option)
        if not role:
            try:
                current = widget.cget(option)
            except Exception:
                continue
            role = _theme_color_role(current)
            if role:
                role_map[option] = role
        if role and role in colors:
            try:
                widget.configure(**{option: colors[role]})
            except Exception:
                pass
    try:
        widget._gui_theme_roles = role_map  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        if isinstance(widget, Button):
            _bind_theme_button_hover(widget, colors, role_map)
    except Exception:
        pass
    try:
        if isinstance(widget, (Text, Listbox, Entry)):
            widget.configure(  # pyright: ignore[reportCallIssue]
                selectbackground=colors["accent"], selectforeground=colors["on_accent"],
                insertbackground=colors["text"],
            )
    except Exception:
        pass
    try:
        if isinstance(widget, Scrollbar):
            widget.configure(
                background=colors["card"], activebackground=colors["accent"],
                troughcolor=colors["surface"], highlightbackground=colors["border"], bd=0,
            )
    except Exception:
        pass
    try:
        if isinstance(widget, Menu):
            widget.configure(
                background=colors["card"], foreground=colors["text"],
                activebackground=colors["accent"], activeforeground=colors["on_accent"],
                selectcolor=colors["accent"], bd=0,
            )
    except Exception:
        pass
    try:
        if isinstance(widget, Text):
            for tag, role in (
                ("info", "accent"), ("recommendation", "accent"),
                ("error", "danger"), ("bad", "danger"),
                ("warning", "warning"), ("warn", "warning"),
                ("success", "success"), ("good", "success"),
                ("normal", "text"), ("section", "text"),
            ):
                widget.tag_config(tag, foreground=colors[role])
    except Exception:
        pass
    try:
        if isinstance(widget, Progressbar):
            style_name = f"Theme.{sanitize_filename(window_key)}.Horizontal.TProgressbar"
            ttk_style = Style(widget)
            ttk_style.theme_use("clam")
            ttk_style.configure(
                style_name, troughcolor=colors["card"], bordercolor=colors["border"],
                background=colors["accent"], lightcolor=colors["accent"], darkcolor=colors["accent"],
            )
            widget.configure(style=style_name)
    except Exception:
        pass
    try:
        menu_name = widget.cget("menu")
        if menu_name:
            _configure_widget_theme(widget.nametowidget(menu_name), colors, window_key)
    except Exception:
        pass
    try:
        children = widget.winfo_children()
    except Exception:
        children = []
    for child in children:
        _configure_widget_theme(child, colors, window_key)


def apply_window_design(window: Any, window_key: str, theme_id: str, persist: bool = True) -> None:
    if theme_id not in GUI_DESIGNS:
        theme_id = "midnight_aurora"
    colors = GUI_DESIGNS[theme_id]["colors"]
    try:
        window.configure(bg=colors["bg"])
        window.option_add("*Menu.background", colors["card"])
        window.option_add("*Menu.foreground", colors["text"])
        window.option_add("*Menu.activeBackground", colors["accent"])
        window.option_add("*Menu.activeForeground", colors["on_accent"])
        _configure_widget_theme(window, colors, window_key)
        window.update_idletasks()
    except Exception as exc:
        logger.warning(f"Theme apply failed ({window_key}/{theme_id}): {exc}")
    if persist:
        GUI_DESIGN_STATE.setdefault("window_designs", {})[window_key] = theme_id
        try:
            _save_gui_design_state()
        except Exception as exc:
            logger.warning(f"GUI design save failed: {exc}")


def register_window_theme(window: Any, window_key: str) -> None:
    """Attach an independent theme selector to one Tk/Toplevel window."""
    try:
        if not window.winfo_exists():
            return
        existing = _REGISTERED_THEME_WINDOWS.get(window_key)
        if existing is not None and existing is not window:
            try:
                if existing.winfo_exists():
                    # Multiple viewers may share the same saved design, but each gets its own menu.
                    pass
            except Exception:
                pass
        _REGISTERED_THEME_WINDOWS[window_key] = window
        selected = str(GUI_DESIGN_STATE.get("window_designs", {}).get(window_key, "midnight_aurora"))
        if selected not in GUI_DESIGNS:
            selected = "midnight_aurora"
        theme_var = StringVar(window, value=selected)
        menu = Menu(window, tearoff=0)
        design_menu = Menu(menu, tearoff=0)
        for theme_id, theme in GUI_DESIGNS.items():
            design_menu.add_radiobutton(
                label=str(theme["name"]), value=theme_id, variable=theme_var,
                command=lambda tid=theme_id: apply_window_design(window, window_key, tid, persist=True),
            )
        menu.add_cascade(label="🎨 დიზაინი", menu=design_menu)
        window.configure(menu=menu)
        window._gui_theme_var = theme_var  # type: ignore[attr-defined]
        window._gui_theme_key = window_key  # type: ignore[attr-defined]
        apply_window_design(window, window_key, selected, persist=False)
        # Most Toplevel functions create their child widgets after registration.
        # Reapply once the current callback returns so every newly-created widget
        # receives the selected per-window design.
        window.after_idle(lambda: apply_window_design(window, window_key, theme_var.get(), persist=False))
    except Exception as exc:
        logger.warning(f"Theme registration failed ({window_key}): {exc}")


try:
    _save_gui_design_state()
except Exception as exc:
    logger.warning(f"Initial GUI design save failed: {exc}")


def _initialize_pro_schema() -> None:
    schema = [
        """CREATE TABLE IF NOT EXISTS scans(
            id TEXT PRIMARY KEY,
            mode TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT,
            source_folder TEXT,
            output_json TEXT,
            refs_json TEXT,
            params_json TEXT,
            totals_json TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS file_results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT,
            mode TEXT,
            relative_path TEXT,
            source_path TEXT,
            destination_json TEXT,
            status TEXT,
            best_similarity REAL,
            second_similarity REAL,
            top_matches_json TEXT,
            fingerprint_json TEXT,
            face_count INTEGER,
            error TEXT,
            created_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS operations(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT,
            mode TEXT,
            kind TEXT,
            source_path TEXT,
            destination_path TEXT,
            file_size INTEGER,
            file_mtime_ns INTEGER,
            file_sha256 TEXT,
            undone INTEGER DEFAULT 0,
            created_at TEXT,
            undone_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS embedding_cache(
            cache_key TEXT PRIMARY KEY,
            original_path TEXT,
            file_size INTEGER,
            file_mtime_ns INTEGER,
            file_sha256 TEXT,
            model_signature TEXT,
            face_count INTEGER,
            embedding_dim INTEGER,
            embeddings BLOB,
            updated_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS review_queue(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT,
            mode TEXT,
            source_path TEXT,
            relative_path TEXT,
            score REAL,
            second_score REAL,
            top_matches_json TEXT,
            recommended_json TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            decided_at TEXT
        )""",
        """CREATE TABLE IF NOT EXISTS identities(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id TEXT,
            identity_index INTEGER,
            display_name TEXT,
            refs_json TEXT,
            created_at TEXT
        )""",
        "CREATE INDEX IF NOT EXISTS idx_file_results_scan ON file_results(scan_id, status)",
        "CREATE INDEX IF NOT EXISTS idx_operations_scan ON operations(scan_id, undone)",
        "CREATE INDEX IF NOT EXISTS idx_review_status ON review_queue(status, mode)",
        "CREATE INDEX IF NOT EXISTS idx_review_lookup ON review_queue(mode, source_path, relative_path, status)",
        "CREATE INDEX IF NOT EXISTS idx_cache_sha_model ON embedding_cache(file_sha256, model_signature)",
    ]
    with state.db_lock:
        for statement in schema:
            scan_cur.execute(statement)
        scan_con.commit()


_initialize_pro_schema()












def _current_model_signature() -> str:
    return f"{FACE_ENGINE_MODEL}:{FACE_ENGINE_DET_SIZE[0]}x{FACE_ENGINE_DET_SIZE[1]}:{FACE_ENGINE_CACHE_VERSION}"


def ensure_face_engine(profile_name: Optional[str] = None) -> str:
    """Apply the selected accuracy profile with a safe fallback.

    This only updates the shared "which model/det_size is active" bookkeeping.
    It does NOT touch any thread's live FaceAnalysis instance while it may be
    mid-inference: each worker thread lazily rebuilds its own instance (see
    _get_thread_face_app) the next time it calls detect_faces() and notices
    the selected model/det_size changed.
    """
    global app, FACE_ENGINE_MODEL, FACE_ENGINE_DET_SIZE
    selected = profile_name or APP_SETTINGS.get("model_profile", "მაქსიმალური სიზუსტე")
    profile = MODEL_PROFILES.get(selected, MODEL_PROFILES["მაქსიმალური სიზუსტე"])
    wanted_model = str(profile["model"])
    wanted_det = tuple(profile["det_size"])
    with FACE_ENGINE_LOCK:
        if FACE_ENGINE_MODEL == wanted_model and FACE_ENGINE_DET_SIZE == wanted_det:
            return _current_model_signature()
        try:
            # Validate the profile loads before publishing it, so a bad
            # profile can never leave workers without a usable engine.
            probe = insightface.app.FaceAnalysis(name=wanted_model, providers=providers)
            probe.prepare(ctx_id=-1, det_size=wanted_det)
            app = probe  # keeps a ready instance around for the main thread
            FACE_ENGINE_MODEL = wanted_model
            FACE_ENGINE_DET_SIZE = wanted_det
            logger.info(f"Face model profile enabled: {selected} -> {wanted_model} {wanted_det}")
        except Exception as exc:
            logger.warning(f"Model profile failed ({selected}); keeping current model: {exc}")
        return _current_model_signature()


def _get_thread_face_app() -> Any:
    """Return this thread's own FaceAnalysis instance, building/rebuilding
    it only when the selected model/det_size differ from what this thread
    currently has loaded. No lock is held across inference, so worker
    threads run detection/embedding truly in parallel."""
    with FACE_ENGINE_LOCK:
        wanted_model, wanted_det = FACE_ENGINE_MODEL, FACE_ENGINE_DET_SIZE
    cached = getattr(FACE_ENGINE_TLS, "app", None)
    cached_key = getattr(FACE_ENGINE_TLS, "key", None)
    if cached is not None and cached_key == (wanted_model, wanted_det):
        return cached
    thread_app = insightface.app.FaceAnalysis(name=wanted_model, providers=providers)
    thread_app.prepare(ctx_id=-1, det_size=wanted_det)
    FACE_ENGINE_TLS.app = thread_app
    FACE_ENGINE_TLS.key = (wanted_model, wanted_det)
    return thread_app


def detect_faces(img: np.ndarray) -> List[Any]:
    """Run detection+embedding on this thread's own engine instance.

    No global lock around inference: ONNX Runtime sessions are safe to run
    concurrently, and giving each thread its own FaceAnalysis instance
    avoids any shared mutable state, so multiple workers now genuinely
    overlap on the CPU/DirectML provider instead of queueing behind one
    another. This is the single biggest throughput fix in this pass.
    """
    thread_app = _get_thread_face_app()
    return list(thread_app.get(img))


def load_image_bgr(path: Union[str, Path]) -> Optional[np.ndarray]:
    """Load common, EXIF-rotated, HEIF/AVIF and optional RAW images safely."""
    p = Path(path)
    try:
        if p.suffix.lower() in {".dng", ".cr2", ".nef", ".arw", ".rw2"}:
            if not RAW_AVAILABLE:
                raise ValueError("RAW მხარდაჭერისთვის დააყენე rawpy")
            with rawpy.imread(str(p)) as raw:  # type: ignore[name-defined]
                rgb = raw.postprocess(use_camera_wb=True, half_size=False, no_auto_bright=False)
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

        with Image.open(p) as pil_img:
            if ImageOps is not None:
                pil_img = ImageOps.exif_transpose(pil_img)
            if getattr(pil_img, "n_frames", 1) > 1:
                pil_img.seek(0)
            rgb = np.asarray(pil_img.convert("RGB"))
            if rgb.size == 0:
                return None
            return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception as pil_exc:
        try:
            raw = np.fromfile(str(p), dtype=np.uint8)
            if raw.size == 0:
                return None
            image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
            if image is not None:
                return image
        except Exception:
            pass
        logger.warning(f"Image load failed for {p}: {pil_exc}")
        return None


def sha256_file(path: Union[str, Path], chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()








def get_emb(path: Union[str, Path]) -> np.ndarray:
    matrix = get_cached_face_embeddings(path)
    if matrix.shape[0] == 0:
        raise ValueError(f"სახე ვერ მოიძებნა: {path}")
    return matrix[0].astype(np.float32)


def _identity_profile(embeddings: List[np.ndarray]) -> Tuple[np.ndarray, np.ndarray]:
    matrix = np.vstack(embeddings).astype(np.float32)
    centroid = np.mean(matrix, axis=0)
    norm = float(np.linalg.norm(centroid))
    if norm > 0:
        centroid = centroid / norm
    return matrix, centroid.astype(np.float32)


def score_embedding_to_identity(embedding: np.ndarray, matrix: np.ndarray, centroid: np.ndarray) -> float:
    if matrix.size == 0:
        return 0.0
    sims = np.sort(matrix @ embedding)[::-1]
    if sims.size == 1:
        return float(sims[0])
    robust = 0.72 * float(sims[0]) + 0.28 * float(np.mean(sims[: min(3, sims.size)]))
    centroid_score = float(np.dot(centroid, embedding)) if centroid.size else robust
    return max(robust, 0.85 * robust + 0.15 * centroid_score)


def _perceptual_hash_and_geometry(img: np.ndarray) -> Tuple[str, str, int, int, float]:
    h, w = img.shape[:2]
    pil_img = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB)) if img.ndim == 3 else Image.fromarray(img)
    p_hash = str(imagehash.phash(pil_img))
    # pHash mostly describes structure and may treat different solid colors as equal.
    # Add mean+standard-deviation RGB signature to prevent those false duplicates.
    rgb_small = np.asarray(pil_img.resize((24, 24)).convert("RGB"), dtype=np.float32)
    means = np.mean(rgb_small, axis=(0, 1))
    stds = np.std(rgb_small, axis=(0, 1))
    color_values = [*means.tolist(), *stds.tolist()]
    color_hash = "-".join(str(int(round(v))) for v in color_values)
    return p_hash, color_hash, w, h, (w / max(1.0, float(h)))


def _check_duplicate(fingerprint: Dict[str, Any], img: np.ndarray) -> Tuple[bool, str, str]:
    sha = str(fingerprint.get("sha256", ""))
    p_hash, color_hash, width, height, aspect = _perceptual_hash_and_geometry(img)
    with state.set_lock:
        exact_hashes = getattr(state, "exact_hashes", set())
        phash_items = getattr(state, "phash_items", [])
        if sha and sha in exact_hashes:
            return True, "duplicate_exact", "SHA-256: ზუსტად იგივე ფაილი"
        for old_hash, old_color_hash, old_aspect, old_w, old_h in phash_items:
            try:
                distance = imagehash.hex_to_hash(p_hash) - imagehash.hex_to_hash(old_hash)
                current_rgb = [int(v) for v in color_hash.split("-")]
                previous_rgb = [int(v) for v in old_color_hash.split("-")]
                color_distance = float(sum(abs(a-b) for a, b in zip(current_rgb, previous_rgb)) / max(1, len(current_rgb)))
            except Exception:
                continue
            aspect_diff = abs(aspect - old_aspect) / max(0.0001, old_aspect)
            if (distance <= config.duplicate_phash_distance
                    and color_distance <= 12.0
                    and aspect_diff <= config.duplicate_aspect_tolerance):
                return True, "duplicate_near", f"pHash distance={distance}, color distance={color_distance}"
        exact_hashes.add(sha)
        phash_items.append((p_hash, color_hash, aspect, width, height))
        state.exact_hashes = exact_hashes
        state.phash_items = phash_items
    return False, "", ""












def _profile_entry(profile: Dict[str, Any], rel: Path) -> Optional[Dict[str, Any]]:
    folder = folder_key_from_rel(rel)
    files = profile.get("folders", {}).get(folder, {})
    value = files.get(rel.name)
    return value if isinstance(value, dict) else None




def router_signature(active_slots: List[Dict[str, Any]], threshold: float, people_count: int) -> str:
    parts = [f"people={people_count}", f"threshold={round(float(threshold), 4)}", _current_model_signature()]
    for slot in sorted(active_slots, key=lambda x: x["index"]):
        refs = [ref for ref in (slot.get("ref_paths") or [slot.get("ref_path")])
                if isinstance(ref, (str, Path))]
        ref_parts: List[str] = []
        for ref in refs:
            p = Path(ref).resolve()
            try:
                st = p.stat()
                ref_parts.append(f"{p}|{st.st_size}|{st.st_mtime_ns}")
            except OSError:
                ref_parts.append(str(p))
        parts.append(f"{slot['index']}|{'||'.join(ref_parts)}|{Path(slot['out_folder']).resolve()}")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def prepare_router_scan_state(
    src_path: Path,
    active_slots: List[Dict[str, Any]],
    threshold: float,
    people_count: int,
    worker_count: int = 4,
) -> Tuple[Path, Dict[str, Any]]:
    state_path = get_router_state_path(src_path)
    loaded = load_json(state_path)
    source = str(src_path.resolve())
    signature = router_signature(active_slots, threshold, people_count)
    if not isinstance(loaded, dict) or loaded.get("mode") != "multi_router" or loaded.get("source_folder") != source:
        loaded = {"version": 3, "mode": "multi_router", "source_folder": source,
                  "created_at": now_iso(), "files": {}, "stats": {}}
    slots_payload = []
    for slot in sorted(active_slots, key=lambda item: item["index"]):
        refs = [ref for ref in (slot.get("ref_paths") or [slot.get("ref_path")])
                if isinstance(ref, (str, Path))]
        slots_payload.append({
            "index": int(slot["index"]),
            "ref_paths": [str(Path(p).resolve()) for p in refs],
            "ref_names": [Path(p).name for p in refs],
            "ref_path": str(Path(refs[0]).resolve()) if refs else "",
            "ref_name": Path(refs[0]).name if refs else "",
            "out_folder": str(Path(slot["out_folder"]).resolve()),
        })
    loaded.update({
        "version": 3,
        "mode": "multi_router",
        "resume_policy": "ordered_queue_counts_no_filename_matching",
        "source_folder": source,
        "signature": signature,
        "updated_at": now_iso(),
        "slot_count": int(people_count),
        "threshold": round(float(threshold), 4),
        "last_worker_count": int(worker_count),
        "model_signature": _current_model_signature(),
        "slots": slots_payload,
        "params": {
            "slot_count": int(people_count), "threshold": round(float(threshold), 4),
            "worker_count": int(worker_count), "model_signature": _current_model_signature(),
            "slots": slots_payload,
        },
    })
    loaded.setdefault("files", {})
    loaded.setdefault("stats", {})
    persist_json_state(state_path, loaded, force=True)
    return state_path, loaded








def _review_margin() -> float:
    return max(0.01, min(0.12, float(getattr(config, "review_margin", 0.04))))




def _wait_while_paused() -> bool:
    while getattr(state, "pause_requested", threading.Event()).is_set():
        if state.stop_requested.is_set():
            return False
        time.sleep(0.12)
    return not state.stop_requested.is_set()












def _scan_totals(total_files: int, done_count: int) -> Dict[str, Any]:
    return {
        "total": total_files, "processed": done_count,
        "matched": len(state.matched), "nonmatched": len(state.nonmatched),
        "duplicates": state.stats.duplicates, "errors": state.stats.errors,
        "resumed": state.stats.resumed, "review": int(getattr(state, "review_count", 0)),
    }




def toggle_pause_scan() -> None:
    if not state.scan_running:
        return
    if state.pause_requested.is_set():
        state.pause_requested.clear()
        pause_btn.config(text="⏸  პაუზა")
        append_log("სკანირება გაგრძელდა", "success")
    else:
        state.pause_requested.set()
        pause_btn.config(text="▶  გაგრძელება")
        append_log("სკანირება დაპაუზებულია; მიმდინარე ფაილი უსაფრთხოდ დასრულდება", "warning")


def stop_scan(close_after: bool = False) -> None:
    if close_after:
        state.close_after_stop = True
    if not state.scan_running:
        if close_after:
            stop_live_watch()
            flush_state_on_exit()
            scan_db_manager.close_persistent()
            router_db_manager.close_persistent()
            root.destroy()
        else:
            messagebox.showinfo("ინფორმაცია", "სკანირება უკვე გაჩერებულია")
        return
    if state.stop_requested.is_set():
        return
    state.pause_requested.clear()
    state.stop_requested.set()
    state.stop_removed_pending = clear_pending_queue()
    flush_state_on_exit()
    stop_btn.config(state=DISABLED)
    pause_btn.config(state=DISABLED)
    progress_label.config(text=f"გაჩერება... რიგიდან წაიშალა {state.stop_removed_pending} ფაილი")




def _operation_unchanged(path: Path, expected_sha: str) -> bool:
    try:
        return path.exists() and (not expected_sha or sha256_file(path) == expected_sha)
    except Exception:
        return False




def undo_last_operation() -> None:
    rollback_operations(only_last=True)


def rollback_last_scan() -> None:
    rollback_operations(only_last=False)

















# ============================================================================
# V4 ACCURACY / SPEED / CONSISTENCY LAYER
# ============================================================================

MATCHING_ALGORITHM_VERSION = "identity-v4.0"


def _upgrade_v4_schema() -> None:
    """Add v4 audit columns/indexes without breaking an existing database."""
    with state.db_lock:
        columns = {row[1] for row in scan_cur.execute("PRAGMA table_info(operations)").fetchall()}
        for name, sql_type in (
            ("logical_source_path", "TEXT"),
            ("relative_path", "TEXT"),
        ):
            if name not in columns:
                scan_cur.execute(f"ALTER TABLE operations ADD COLUMN {name} {sql_type}")
        scan_cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_results_scan_rel ON file_results(scan_id,relative_path,id)"
        )
        scan_cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_operations_scan_rel ON operations(scan_id,relative_path,undone)"
        )
        scan_cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cache_path_stat_model ON embedding_cache(original_path,file_size,file_mtime_ns,model_signature)"
        )
        scan_cur.execute(
            """CREATE TABLE IF NOT EXISTS checked_nonmatches(
                source_key TEXT NOT NULL,
                source_root TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_name_folded TEXT NOT NULL,
                mode TEXT NOT NULL,
                recognition_signature TEXT NOT NULL,
                status TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                file_mtime_ns INTEGER NOT NULL,
                file_ctime_ns INTEGER DEFAULT 0,
                quick_hash TEXT,
                file_sha256 TEXT,
                model_signature TEXT,
                reference_signature TEXT,
                threshold REAL,
                checked_at TEXT,
                last_seen_at TEXT,
                PRIMARY KEY(source_key, relative_path, mode, recognition_signature)
            )"""
        )
        scan_cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_checked_nonmatches_exact ON checked_nonmatches(source_key,mode,recognition_signature,relative_path)"
        )
        # No filename-based resume index: same names never imply the same checked photo.
        scan_cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_checked_nonmatches_content ON checked_nonmatches(source_key,mode,recognition_signature,file_size,quick_hash)"
        )
        scan_con.commit()


_upgrade_v4_schema()


def _db_commit_locked(force: bool = False) -> None:
    """Batch frequent SQLite writes; force only at transaction boundaries."""
    global DB_PENDING_WRITES, DB_LAST_COMMIT_AT
    now = time.monotonic()
    DB_PENDING_WRITES += 1
    if DB_LAST_COMMIT_AT <= 0:
        DB_LAST_COMMIT_AT = now
    if (
        force
        or DB_PENDING_WRITES >= config.db_commit_every
        or (now - DB_LAST_COMMIT_AT) >= config.db_commit_interval
    ):
        scan_con.commit()
        DB_PENDING_WRITES = 0
        DB_LAST_COMMIT_AT = now


def db_start_scan(scan_id: str, mode: str, source: str, outputs: Any, refs: Any, params: Dict[str, Any]) -> None:
    with state.db_lock:
        scan_cur.execute(
            """INSERT INTO scans
               (id,mode,started_at,status,source_folder,output_json,refs_json,params_json,totals_json)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   mode=excluded.mode,status='running',source_folder=excluded.source_folder,
                   output_json=excluded.output_json,refs_json=excluded.refs_json,
                   params_json=excluded.params_json""",
            (
                scan_id, mode, now_iso(), "running", source,
                json.dumps(outputs, ensure_ascii=False),
                json.dumps(refs, ensure_ascii=False),
                json.dumps(params, ensure_ascii=False), "{}",
            ),
        )
        _db_commit_locked(force=True)


def db_finish_scan(scan_id: str, status: str, totals: Dict[str, Any]) -> None:
    if not scan_id:
        return
    with state.db_lock:
        scan_cur.execute(
            "UPDATE scans SET finished_at=?,status=?,totals_json=? WHERE id=?",
            (now_iso(), status, json.dumps(totals, ensure_ascii=False), scan_id),
        )
        _db_commit_locked(force=True)


def db_record_file_result(
    scan_id: str,
    mode: str,
    relative_path: str,
    source_path: str,
    status: str,
    destination_paths: Optional[List[str]] = None,
    best_similarity: float = 0.0,
    second_similarity: float = 0.0,
    top_matches: Optional[List[Dict[str, Any]]] = None,
    fingerprint: Optional[Dict[str, Any]] = None,
    face_count: int = 0,
    error: str = "",
) -> None:
    with state.db_lock:
        scan_cur.execute(
            """INSERT INTO file_results
               (scan_id,mode,relative_path,source_path,destination_json,status,
                best_similarity,second_similarity,top_matches_json,fingerprint_json,
                face_count,error,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id, mode, relative_path, source_path,
                json.dumps(destination_paths or [], ensure_ascii=False), status,
                float(best_similarity), float(second_similarity),
                json.dumps(top_matches or [], ensure_ascii=False),
                json.dumps(fingerprint or {}, ensure_ascii=False), int(face_count),
                error, now_iso(),
            ),
        )
        _db_commit_locked()


def db_add_review(
    scan_id: str,
    mode: str,
    source_path: str,
    relative_path: str,
    score: float,
    second_score: float,
    top_matches: List[Dict[str, Any]],
    recommended: Dict[str, Any],
) -> int:
    with state.db_lock:
        # Keep only one current pending decision for the same physical file.
        # A changed file or a restarted scan refreshes the existing row instead
        # of leaving stale duplicate Review entries behind.
        existing = scan_cur.execute(
            """SELECT id FROM review_queue
               WHERE mode=? AND source_path=? AND relative_path=? AND status='pending'
               ORDER BY id DESC LIMIT 1""",
            (mode, source_path, relative_path),
        ).fetchone()
        if existing:
            review_id = int(existing[0])
            scan_cur.execute(
                """UPDATE review_queue SET scan_id=?,score=?,second_score=?,top_matches_json=?,
                          recommended_json=?,created_at=?,decided_at=NULL
                   WHERE id=?""",
                (
                    scan_id, float(score), float(second_score),
                    json.dumps(top_matches, ensure_ascii=False),
                    json.dumps(recommended, ensure_ascii=False), now_iso(), review_id,
                ),
            )
            _db_commit_locked(force=True)
            return review_id
        scan_cur.execute(
            """INSERT INTO review_queue
               (scan_id,mode,source_path,relative_path,score,second_score,
                top_matches_json,recommended_json,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id, mode, source_path, relative_path, float(score), float(second_score),
                json.dumps(top_matches, ensure_ascii=False),
                json.dumps(recommended, ensure_ascii=False), "pending", now_iso(),
            ),
        )
        if scan_cur.lastrowid is None:
            raise RuntimeError("Review ჩანაწერის ID ვერ შეიქმნა")
        review_id = int(scan_cur.lastrowid)
        _db_commit_locked(force=True)
        return review_id


def db_cancel_pending_reviews(mode: str, source_path: str, relative_path: str) -> None:
    if not source_path or not relative_path:
        return
    with state.db_lock:
        scan_cur.execute(
            """UPDATE review_queue SET status='superseded',decided_at=?
               WHERE mode=? AND source_path=? AND relative_path=? AND status='pending'""",
            (now_iso(), mode, source_path, relative_path),
        )
        if scan_cur.rowcount:
            _db_commit_locked(force=True)


def quick_hash_file(path: Union[str, Path], sample_size: int = 64 * 1024) -> str:
    """Fast content signature using beginning/middle/end samples."""
    p = Path(path)
    size = p.stat().st_size
    digest = hashlib.blake2b(digest_size=16)
    digest.update(str(size).encode("ascii"))
    with open(p, "rb") as fh:
        offsets = [0]
        if size > sample_size * 2:
            offsets.append(max(0, size // 2 - sample_size // 2))
        if size > sample_size:
            offsets.append(max(0, size - sample_size))
        for offset in dict.fromkeys(offsets):
            fh.seek(offset)
            digest.update(fh.read(sample_size))
    return digest.hexdigest()


def file_fingerprint(path: Union[str, Path], include_sha: bool = False) -> Dict[str, Any]:
    p = Path(path)
    st = p.stat()
    cache_key = str(p.resolve())
    cached_quick = None
    with state.set_lock:
        cached = state.precomputed_quick_hashes.pop(cache_key, None)
    if cached and int(cached[0]) == int(st.st_size) and int(cached[1]) == int(st.st_mtime_ns):
        cached_quick = str(cached[2])
    result = {
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "ctime_ns": int(getattr(st, "st_ctime_ns", 0)),
        "quick_hash": cached_quick or quick_hash_file(p),
        "sha256": "",
        "suffix": p.suffix.lower(),
    }
    if include_sha:
        result["sha256"] = sha256_file(p)
    return result


def ensure_fingerprint_sha(fingerprint: Dict[str, Any], path: Union[str, Path]) -> str:
    sha = str(fingerprint.get("sha256", ""))
    if not sha:
        sha = sha256_file(path)
        fingerprint["sha256"] = sha
    return sha


def _embedding_cache_key(fingerprint: Dict[str, Any], model_signature: str) -> str:
    basis = fingerprint.get("sha256") or (
        f"{fingerprint.get('size')}|{fingerprint.get('mtime_ns')}|{fingerprint.get('quick_hash')}"
    )
    return hashlib.sha256(f"{basis}|{model_signature}".encode("utf-8")).hexdigest()


def get_cached_face_embeddings(
    path: Union[str, Path],
    img: Optional[np.ndarray] = None,
    fingerprint: Optional[Dict[str, Any]] = None,
) -> np.ndarray:
    p = Path(path)
    fp = fingerprint or file_fingerprint(p, include_sha=False)
    model_sig = _current_model_signature()

    # Fast path: unchanged path/stat cache lookup avoids reading the whole file for SHA-256.
    with state.db_lock:
        row = scan_cur.execute(
            """SELECT face_count,embedding_dim,embeddings,file_sha256
               FROM embedding_cache
               WHERE original_path=? AND file_size=? AND file_mtime_ns=? AND model_signature=?
               ORDER BY updated_at DESC LIMIT 1""",
            (str(p), int(fp["size"]), int(fp["mtime_ns"]), model_sig),
        ).fetchone()
    if row:
        count, dim, blob, cached_sha = int(row[0]), int(row[1]), row[2], str(row[3] or "")
        if cached_sha:
            fp["sha256"] = cached_sha
        if count <= 0:
            return np.empty((0, 512), dtype=np.float32)
        return np.frombuffer(blob, dtype=np.float32).copy().reshape(count, dim)

    ensure_fingerprint_sha(fp, p)
    key = _embedding_cache_key(fp, model_sig)
    with state.db_lock:
        row = scan_cur.execute(
            "SELECT face_count,embedding_dim,embeddings FROM embedding_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
    if row:
        count, dim, blob = int(row[0]), int(row[1]), row[2]
        if count <= 0:
            return np.empty((0, 512), dtype=np.float32)
        return np.frombuffer(blob, dtype=np.float32).copy().reshape(count, dim)

    loaded = img if img is not None else load_image_bgr(p)
    if loaded is None:
        raise ValueError(f"სურათი ვერ ჩაიტვირთა: {p}")
    faces = detect_faces(loaded)
    image_h, image_w = loaded.shape[:2]
    min_face_side = max(28.0, min(image_h, image_w) * 0.02)
    valid_faces: List[Any] = []
    for face in faces:
        try:
            det_score = float(getattr(face, "det_score", 1.0))
            bbox = np.asarray(face.bbox, dtype=float).flatten()
            face_w = float(bbox[2] - bbox[0]) if bbox.size >= 4 else min_face_side
            face_h = float(bbox[3] - bbox[1]) if bbox.size >= 4 else min_face_side
            if det_score >= 0.67 and min(face_w, face_h) >= min_face_side:
                valid_faces.append(face)
        except Exception:
            continue
    embeddings = [np.asarray(face.normed_embedding, dtype=np.float32) for face in valid_faces]
    matrix = np.vstack(embeddings).astype(np.float32) if embeddings else np.empty((0, 512), dtype=np.float32)
    dim = int(matrix.shape[1]) if matrix.size else 512
    with state.db_lock:
        scan_cur.execute(
            """INSERT OR REPLACE INTO embedding_cache
               (cache_key,original_path,file_size,file_mtime_ns,file_sha256,model_signature,
                face_count,embedding_dim,embeddings,updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                key, str(p), int(fp["size"]), int(fp["mtime_ns"]), str(fp["sha256"]),
                model_sig, int(matrix.shape[0]), dim, matrix.tobytes(), now_iso(),
            ),
        )
        _db_commit_locked()
    return matrix


class DuplicateIndex:
    """Near-O(1) exact lookup and bucketed pHash candidate lookup."""

    def __init__(self) -> None:
        self.exact_hashes: Set[str] = set()
        self.records: List[Tuple[str, str, float, int, int]] = []
        self.buckets: Dict[Tuple[int, int], Set[int]] = {}
        self.lock = threading.Lock()

    @staticmethod
    def _segments(hash_text: str) -> List[int]:
        # Eight 8-bit chunks guarantee that a 64-bit pHash with <=4 changed bits
        # still shares several exact buckets. Four 16-bit chunks could miss a
        # valid near-duplicate when one bit changed in every chunk.
        value = int(hash_text, 16)
        return [(value >> (8 * index)) & 0xFF for index in range(8)]

    def seed(self, fingerprint: Dict[str, Any]) -> None:
        sha = str(fingerprint.get("sha256", ""))
        p_hash = str(fingerprint.get("phash", ""))
        color_hash = str(fingerprint.get("color_hash", ""))
        aspect = float(fingerprint.get("aspect", 0.0) or 0.0)
        width = int(fingerprint.get("width", 0) or 0)
        height = int(fingerprint.get("height", 0) or 0)
        with self.lock:
            if sha:
                self.exact_hashes.add(sha)
            if p_hash and color_hash and aspect > 0:
                self._add_visual_locked(p_hash, color_hash, aspect, width, height)

    def _add_visual_locked(self, p_hash: str, color_hash: str, aspect: float, width: int, height: int) -> None:
        record_id = len(self.records)
        self.records.append((p_hash, color_hash, aspect, width, height))
        for index, segment in enumerate(self._segments(p_hash)):
            self.buckets.setdefault((index, segment), set()).add(record_id)

    def check_and_add(self, fingerprint: Dict[str, Any], img: np.ndarray) -> Tuple[bool, str, str]:
        sha = str(fingerprint.get("sha256", ""))
        p_hash, color_hash, width, height, aspect = _perceptual_hash_and_geometry(img)
        fingerprint.update({
            "phash": p_hash, "color_hash": color_hash, "width": width,
            "height": height, "aspect": round(float(aspect), 8),
        })
        with self.lock:
            if sha and sha in self.exact_hashes:
                return True, "duplicate_exact", "SHA-256: ზუსტად იგივე ფაილი"
            candidate_ids: Set[int] = set()
            for index, segment in enumerate(self._segments(p_hash)):
                candidate_ids.update(self.buckets.get((index, segment), set()))
            current_rgb = [int(value) for value in color_hash.split("-")]
            for record_id in candidate_ids:
                old_hash, old_color_hash, old_aspect, _old_w, _old_h = self.records[record_id]
                try:
                    distance = imagehash.hex_to_hash(p_hash) - imagehash.hex_to_hash(old_hash)
                    previous_rgb = [int(value) for value in old_color_hash.split("-")]
                    color_distance = sum(abs(a - b) for a, b in zip(current_rgb, previous_rgb)) / max(1, len(current_rgb))
                    aspect_diff = abs(aspect - old_aspect) / max(0.0001, old_aspect)
                except Exception:
                    continue
                if (
                    distance <= config.duplicate_phash_distance
                    and color_distance <= 12.0
                    and aspect_diff <= config.duplicate_aspect_tolerance
                ):
                    return True, "duplicate_near", f"pHash={distance}, color={color_distance:.1f}"
            if sha:
                self.exact_hashes.add(sha)
            self._add_visual_locked(p_hash, color_hash, aspect, width, height)
        return False, "", ""


def _seed_duplicate_index(index: DuplicateIndex, data: Dict[str, Any], router: bool = False) -> None:
    if router:
        entries = data.get("files", {}).values()
    else:
        entries = (
            meta
            for folder in data.get("folders", {}).values()
            for meta in folder.values()
        )
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("fingerprint"), dict):
            index.seed(entry["fingerprint"])


def _set_operation_context(
    scan_id: str,
    mode: str,
    logical_source_path: Union[str, Path, None] = None,
    relative_path: Union[str, Path, None] = None,
) -> None:
    OPERATION_CONTEXT.scan_id = scan_id
    OPERATION_CONTEXT.mode = mode
    OPERATION_CONTEXT.logical_source_path = str(logical_source_path or "")
    OPERATION_CONTEXT.relative_path = Path(relative_path).as_posix() if relative_path else ""


def _clear_operation_context() -> None:
    OPERATION_CONTEXT.scan_id = ""
    OPERATION_CONTEXT.mode = ""
    OPERATION_CONTEXT.logical_source_path = ""
    OPERATION_CONTEXT.relative_path = ""


def _log_operation(kind: str, source: Path, destination: Path, fingerprint: Dict[str, Any]) -> None:
    scan_id = str(getattr(OPERATION_CONTEXT, "scan_id", "") or getattr(state, "active_scan_id", ""))
    mode = str(getattr(OPERATION_CONTEXT, "mode", "") or "scan")
    logical_source = str(getattr(OPERATION_CONTEXT, "logical_source_path", "") or source)
    relative_path = str(getattr(OPERATION_CONTEXT, "relative_path", ""))
    with state.db_lock:
        scan_cur.execute(
            """INSERT INTO operations
               (scan_id,mode,kind,source_path,destination_path,file_size,file_mtime_ns,
                file_sha256,created_at,logical_source_path,relative_path)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                scan_id, mode, kind, str(source), str(destination), int(fingerprint.get("size", 0)),
                int(fingerprint.get("mtime_ns", 0)), str(fingerprint.get("sha256", "")), now_iso(),
                logical_source, relative_path,
            ),
        )
        _db_commit_locked(force=True)


def move_file_safely(
    source: Union[str, Path], destination: Union[str, Path], fingerprint: Optional[Dict[str, Any]] = None
) -> Path:
    src = Path(source)
    fp = fingerprint if fingerprint is not None else file_fingerprint(src, include_sha=False)
    ensure_fingerprint_sha(fp, src)
    dst = unique_destination_path(Path(destination))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    _log_operation("move", src, dst, fp)
    return dst


def copy_file_safely(
    source: Union[str, Path], destination: Union[str, Path], fingerprint: Optional[Dict[str, Any]] = None
) -> Path:
    src = Path(source)
    fp = fingerprint if fingerprint is not None else file_fingerprint(src, include_sha=False)
    ensure_fingerprint_sha(fp, src)
    dst = unique_destination_path(Path(destination))
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(src), str(dst))
    _log_operation("copy", src, dst, fp)
    return dst




def _identity_signature(
    reference_signature: str = "",
    router_signature_value: str = "",
) -> str:
    """Signature of what makes previously-computed results reusable.

    Deliberately includes ONLY things that change what a file's face
    identity/embeddings mean: the matching algorithm version, the active
    model, and which reference (or router) identities were selected.

    It deliberately EXCLUDES runtime tuning knobs - threshold, ambiguity
    margin, review margin, duplicate mode/distance, worker count, GUI
    settings - so that changing those never invalidates resume/history.
    This is the signature used to decide "is this file's prior result still
    usable" for resume purposes. See _matching_signature for the fuller,
    threshold-aware signature still used for match/review bookkeeping.
    """
    payload = {
        "algorithm": MATCHING_ALGORITHM_VERSION,
        "model": _current_model_signature(),
        "reference_signature": reference_signature,
        "router_signature": router_signature_value,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _matching_signature(
    threshold: float,
    reference_signature: str = "",
    router_signature_value: str = "",
) -> str:
    payload = {
        "algorithm": MATCHING_ALGORITHM_VERSION,
        "model": _current_model_signature(),
        "threshold": round(float(threshold), 6),
        "ambiguity": round(float(config.ambiguity_margin), 6),
        "review_margin": round(float(_review_margin()), 6),
        "duplicate_distance": int(config.duplicate_phash_distance),
        "duplicate_mode": str(getattr(state, "duplicate_mode", "მხოლოდ ანგარიშში")),
        "run_mode": "real",
        "reference_signature": reference_signature,
        "router_signature": router_signature_value,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _recognition_signature(
    threshold: float,
    reference_signature: str = "",
    router_signature_value: str = "",
) -> str:
    """Signature of recognition decisions used by resume and checked-nonmatch caches."""
    payload = {
        "algorithm": MATCHING_ALGORITHM_VERSION,
        "model": _current_model_signature(),
        "threshold": round(float(threshold), 6),
        "ambiguity": round(float(config.ambiguity_margin), 6),
        "review_margin": round(float(_review_margin()), 6),
        "reference_signature": reference_signature,
        "router_signature": router_signature_value,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _source_manifest_key(source_root: Union[str, Path]) -> str:
    resolved = str(Path(source_root).resolve()).lower()
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()


def _stat_fingerprint(path: Path) -> Dict[str, int]:
    st = path.stat()
    return {
        "size": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns),
        "ctime_ns": int(getattr(st, "st_ctime_ns", 0)),
    }


def _checked_mode(mode: str) -> str:
    return "router" if str(mode) == "router" else "scan"


def db_upsert_checked_nonmatch(
    source_root: Union[str, Path],
    relative_path: Union[str, Path],
    mode: str,
    recognition_signature: str,
    fingerprint: Dict[str, Any],
    status: str,
    reference_signature: str = "",
    threshold: float = 0.0,
) -> None:
    """Persist an unchanged nonmatch so later scans can skip AI/image decoding."""
    if not source_root or not recognition_signature or not fingerprint:
        return
    rel = Path(relative_path).as_posix()
    root = str(Path(source_root).resolve())
    with state.db_lock:
        scan_cur.execute(
            """INSERT INTO checked_nonmatches(
                   source_key,source_root,relative_path,file_name,file_name_folded,mode,
                   recognition_signature,status,file_size,file_mtime_ns,file_ctime_ns,
                   quick_hash,file_sha256,model_signature,reference_signature,threshold,
                   checked_at,last_seen_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(source_key,relative_path,mode,recognition_signature) DO UPDATE SET
                   file_name=excluded.file_name,
                   file_name_folded=excluded.file_name_folded,
                   status=excluded.status,
                   file_size=excluded.file_size,
                   file_mtime_ns=excluded.file_mtime_ns,
                   file_ctime_ns=excluded.file_ctime_ns,
                   quick_hash=excluded.quick_hash,
                   file_sha256=excluded.file_sha256,
                   model_signature=excluded.model_signature,
                   reference_signature=excluded.reference_signature,
                   threshold=excluded.threshold,
                   checked_at=excluded.checked_at,
                   last_seen_at=excluded.last_seen_at""",
            (
                _source_manifest_key(root), root, rel, Path(rel).name, Path(rel).name.casefold(),
                _checked_mode(mode), recognition_signature, status,
                int(fingerprint.get("size", 0)), int(fingerprint.get("mtime_ns", 0)),
                int(fingerprint.get("ctime_ns", 0)), str(fingerprint.get("quick_hash", "")),
                str(fingerprint.get("sha256", "")), _current_model_signature(),
                reference_signature, float(threshold), now_iso(), now_iso(),
            ),
        )
        _db_commit_locked()


def _seed_checked_nonmatches_from_main(profile: Dict[str, Any], source_root: Path, recognition_signature: str) -> None:
    """Import only missing legacy/current JSON nonmatches into the indexed ledger."""
    with state.db_lock:
        existing_paths = {
            str(row[0]) for row in scan_cur.execute(
                "SELECT relative_path FROM checked_nonmatches WHERE source_key=? AND mode='scan' AND recognition_signature=?",
                (_source_manifest_key(source_root), recognition_signature),
            ).fetchall()
        }
    for folder_name, files in profile.get("folders", {}).items():
        if not isinstance(files, dict):
            continue
        prefix = "" if folder_name == "__root__" else f"{folder_name}/"
        for file_name, entry in files.items():
            if not isinstance(entry, dict) or str(entry.get("status")) not in {"nonmatched", "review_rejected"}:
                continue
            fp = entry.get("fingerprint")
            if not isinstance(fp, dict) or not fp:
                continue
            rel = str(entry.get("relative_path") or (prefix + file_name))
            if rel in existing_paths:
                continue
            stored_sig = str(entry.get("recognition_signature", ""))
            if stored_sig:
                if stored_sig != recognition_signature:
                    continue
            else:
                if str(entry.get("model_signature", "")) not in {"", _current_model_signature()}:
                    continue
                if str(entry.get("reference_signature", "")) != str(getattr(state, "current_ref_signature", "")):
                    continue
                if abs(float(entry.get("threshold", -1.0)) - float(getattr(state, "current_threshold", 0.0))) > 1e-9:
                    continue
            db_upsert_checked_nonmatch(
                source_root, rel, "scan", recognition_signature, fp, "nonmatched",
                str(entry.get("reference_signature", getattr(state, "current_ref_signature", ""))),
                float(entry.get("threshold", getattr(state, "current_threshold", 0.0)) or 0.0),
            )
            existing_paths.add(rel)


def _seed_checked_nonmatches_from_router(router_state: Dict[str, Any], source_root: Path, recognition_signature: str) -> None:
    with state.db_lock:
        existing_paths = {
            str(row[0]) for row in scan_cur.execute(
                "SELECT relative_path FROM checked_nonmatches WHERE source_key=? AND mode='router' AND recognition_signature=?",
                (_source_manifest_key(source_root), recognition_signature),
            ).fetchall()
        }
    for rel, entry in router_state.get("files", {}).items():
        if rel in existing_paths:
            continue
        if not isinstance(entry, dict) or str(entry.get("status")) not in {"unmatched", "review_rejected"}:
            continue
        fp = entry.get("fingerprint")
        if not isinstance(fp, dict) or not fp:
            continue
        stored_sig = str(entry.get("recognition_signature", ""))
        if stored_sig:
            if stored_sig != recognition_signature:
                continue
        else:
            if str(entry.get("model_signature", "")) not in {"", _current_model_signature()}:
                continue
            if str(entry.get("router_signature", "")) != str(router_state.get("signature", "")):
                continue
            if abs(float(entry.get("threshold", -1.0)) - float(router_state.get("threshold", 0.0))) > 1e-9:
                continue
        db_upsert_checked_nonmatch(
            source_root, rel, "router", recognition_signature, fp, "unmatched",
            str(entry.get("router_signature", router_state.get("signature", ""))),
            float(entry.get("threshold", router_state.get("threshold", 0.0)) or 0.0),
        )
        existing_paths.add(rel)


class CheckedNonmatchIndex:
    """Persistent exact-path skip index.

    It never treats a same-named, renamed, or moved file as already checked.
    Only the exact stored relative path may be skipped, and only when its
    metadata or verified quick content hash proves that the file is unchanged.
    """

    def __init__(self, source_root: Path, mode: str, recognition_signature: str):
        self.source_root = source_root.resolve()
        self.mode = _checked_mode(mode)
        self.recognition_signature = recognition_signature
        self.by_rel: Dict[str, Dict[str, Any]] = {}
        self.stat_skips = 0
        self.hash_skips = 0
        self.moved_skips = 0  # kept for backward-compatible log access; always zero
        key = _source_manifest_key(self.source_root)
        with state.db_lock:
            rows = scan_cur.execute(
                """SELECT relative_path,file_name,file_size,file_mtime_ns,file_ctime_ns,
                          quick_hash,file_sha256,status,checked_at
                   FROM checked_nonmatches
                   WHERE source_key=? AND mode=? AND recognition_signature=?""",
                (key, self.mode, self.recognition_signature),
            ).fetchall()
        for row in rows:
            item = {
                "relative_path": str(row[0]), "file_name": str(row[1]),
                "size": int(row[2]), "mtime_ns": int(row[3]), "ctime_ns": int(row[4] or 0),
                "quick_hash": str(row[5] or ""), "sha256": str(row[6] or ""),
                "status": str(row[7]), "checked_at": str(row[8] or ""),
            }
            self.by_rel[str(item["relative_path"])] = item

    def remember(self, rel: Path, fingerprint: Dict[str, Any], status: str = "nonmatched") -> None:
        self.by_rel[rel.as_posix()] = {
            "relative_path": rel.as_posix(), "file_name": rel.name,
            "size": int(fingerprint.get("size", 0)),
            "mtime_ns": int(fingerprint.get("mtime_ns", 0)),
            "ctime_ns": int(fingerprint.get("ctime_ns", 0)),
            "quick_hash": str(fingerprint.get("quick_hash", "")),
            "sha256": str(fingerprint.get("sha256", "")),
            "status": status, "checked_at": now_iso(),
        }

    def lookup(self, path: Path, rel: Path) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """Skip only the exact unchanged path; never compare names across files."""
        try:
            stat_fp = _stat_fingerprint(path)
        except OSError:
            return False, "missing", None
        exact = self.by_rel.get(rel.as_posix())
        if not exact or int(exact.get("size", -1)) != int(stat_fp["size"]):
            return False, "new-or-changed", None
        same_mtime = int(exact.get("mtime_ns", 0)) == int(stat_fp["mtime_ns"])
        old_ctime = int(exact.get("ctime_ns", 0) or 0)
        same_ctime = old_ctime == 0 or old_ctime == int(stat_fp["ctime_ns"])
        if same_mtime and same_ctime:
            self.stat_skips += 1
            return True, "exact-path+size+time", exact
        old_quick = str(exact.get("quick_hash", ""))
        if old_quick:
            computed_quick = quick_hash_file(path)
            if computed_quick == old_quick:
                self.hash_skips += 1
                return True, "exact-path+content", exact
            try:
                with state.set_lock:
                    state.precomputed_quick_hashes[str(path.resolve())] = (
                        int(stat_fp["size"]), int(stat_fp["mtime_ns"]), computed_quick,
                    )
            except Exception:
                pass
        return False, "new-or-changed", None


def export_checked_nonmatches(source_root: Union[str, Path], mode: str) -> Path:
    """Create a human-readable list while SQLite remains the fast source of truth."""
    root = Path(source_root).resolve()
    target_dir = (ROUTER_OUT_DIR if _checked_mode(mode) == "router" else SCAN_OUT_DIR) / "checked_lists"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{sanitize_filename(root.name)}_{_source_manifest_key(root)[:12]}_checked_nonmatches.csv"
    with state.db_lock:
        rows = scan_cur.execute(
            """SELECT relative_path,file_name,status,file_size,file_mtime_ns,quick_hash,
                      model_signature,reference_signature,threshold,recognition_signature,checked_at,last_seen_at
               FROM checked_nonmatches WHERE source_key=? AND mode=?
               ORDER BY relative_path COLLATE NOCASE""",
            (_source_manifest_key(root), _checked_mode(mode)),
        ).fetchall()
    tmp = target.with_suffix(target.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow([
            "relative_path", "file_name", "status", "size", "mtime_ns", "quick_hash",
            "model", "reference_signature", "threshold", "recognition_signature", "checked_at", "last_seen_at",
        ])
        writer.writerows(rows)
    os.replace(tmp, target)
    return target


def iter_image_files(root: Path) -> List[Path]:
    """Faster recursive discovery than repeated Path.rglob metadata calls."""
    results: List[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False) and Path(entry.name).suffix.lower() in config.extensions:
                            results.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
    return results


def _fingerprint_matches(entry_fp: Dict[str, Any], path: Path) -> bool:
    try:
        current = _stat_fingerprint(path)
    except OSError:
        return False
    if int(current.get("size", -1)) != int(entry_fp.get("size", -2)):
        return False
    old_mtime = int(entry_fp.get("mtime_ns", -2))
    old_ctime = int(entry_fp.get("ctime_ns", 0) or 0)
    if int(current.get("mtime_ns", -1)) == old_mtime and (
        old_ctime == 0 or int(current.get("ctime_ns", -1)) == old_ctime
    ):
        return True
    old_quick = str(entry_fp.get("quick_hash", ""))
    if old_quick:
        return old_quick == quick_hash_file(path)
    old_sha = str(entry_fp.get("sha256", ""))
    return bool(old_sha) and sha256_file(path) == old_sha


def _entry_identity_compatible(entry: Dict[str, Any], identity_signature: str) -> bool:
    """True if a previously-recorded entry is still identity-compatible.

    Entries written by this version carry an explicit identity_signature.
    Entries written by older versions of the app only have the old
    threshold-including matching_signature and no identity_signature at
    all - for those we don't want to force a full rescan just because the
    user upgraded, so we treat "no identity_signature recorded" as
    compatible and rely on the fingerprint check (same file, unchanged)
    that every caller already performs alongside this one.
    """
    stored = entry.get("identity_signature")
    if not stored:
        return True
    return str(stored) == identity_signature


def _entry_is_current(
    entry: Optional[Dict[str, Any]],
    path: Path,
    identity_signature: str,
    expected_run_mode: str,
) -> bool:
    if not entry:
        return False
    status = str(entry.get("status", ""))
    allowed = {
        "real": {"matched", "nonmatched", "duplicate", "duplicate_exact", "duplicate_near", "review"},
    }
    if status not in allowed.get(expected_run_mode, set()):
        return False
    if str(entry.get("run_mode", "")) != expected_run_mode:
        return False
    if not _entry_identity_compatible(entry, identity_signature):
        return False
    fp = entry.get("fingerprint")
    return isinstance(fp, dict) and _fingerprint_matches(fp, path)


def _router_entry_is_current(
    router_state: Dict[str, Any],
    rel: Path,
    path: Path,
    identity_signature: str,
    expected_run_mode: str,
) -> bool:
    entry = router_state.get("files", {}).get(rel.as_posix())
    if not isinstance(entry, dict):
        return False
    status = str(entry.get("status", ""))
    allowed = {
        "real": {"matched", "unmatched", "duplicate_exact", "duplicate_near", "review"},
    }
    if status not in allowed.get(expected_run_mode, set()):
        return False
    if str(entry.get("run_mode", "")) != expected_run_mode:
        return False
    if not _entry_identity_compatible(entry, identity_signature):
        return False
    fp = entry.get("fingerprint")
    return isinstance(fp, dict) and _fingerprint_matches(fp, path)


def _classify_pending_photo(entry: Optional[Dict[str, Any]], path: Path) -> str:
    """Classify why a photo needs processing for accurate change logs."""
    if not isinstance(entry, dict):
        return "new"

    fingerprint = entry.get("fingerprint")
    if isinstance(fingerprint, dict):
        try:
            if not _fingerprint_matches(fingerprint, path):
                return "changed"
        except Exception:
            return "changed"

    status = str(entry.get("status", ""))
    if status in {"error", "in_progress", "rolled_back", "review_rejected", "failed_temporary"}:
        return "retry"
    return "reprocess"


def _photo_change_log_message(
    prefix: str,
    new_count: int,
    changed_count: int,
    reprocess_count: int,
    resumed_count: Optional[int] = None,
) -> str:
    """Build one consistent Georgian log line for folder changes."""
    parts = [
        prefix,
        f"ახალი ფოტო: {max(0, int(new_count))}",
        f"შეცვლილი: {max(0, int(changed_count))}",
        f"ხელახლა დასამუშავებელი: {max(0, int(reprocess_count))}",
    ]
    if resumed_count is not None:
        parts.append(f"უკვე დამუშავებული: {max(0, int(resumed_count))}")
    return " | ".join(parts)


# ----------- EXACT ORDERED RESUME JOURNAL (v4.6) ------------
RESUME_JOURNAL_VERSION = 2
_RESUME_FINAL_STATUSES = {
    "matched", "nonmatched", "unmatched", "review",
    "review_approved", "duplicate", "duplicate_exact", "duplicate_near",
}
_RESUME_MOVED_STATUSES = {"matched", "review_approved", "duplicate", "duplicate_exact", "duplicate_near"}


def _resume_context_key(source_root: Path, mode: str, identity_signature: str, run_mode: str) -> str:
    """Key for the resume manifest/journal. Built from identity_signature
    (source folder + reference identity + model), NOT the full
    threshold-including matching_signature, so changing runtime tuning
    parameters (threshold, worker count, margins, duplicate mode, GUI
    settings) never resets/discards the resume journal."""
    raw = f"{source_root.resolve()}|{mode}|{identity_signature}|{run_mode}|{MATCHING_ALGORITHM_VERSION}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _profile_entries(profile: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for folder_name, files in profile.get("folders", {}).items():
        if not isinstance(files, dict):
            continue
        prefix = "" if folder_name == "__root__" else f"{folder_name}/"
        for file_name, meta in files.items():
            if isinstance(meta, dict):
                rel = str(meta.get("relative_path") or f"{prefix}{file_name}")
                result[rel] = meta
    return result


def _entry_destinations(entry: Dict[str, Any]) -> List[str]:
    values: List[str] = []
    raw_many = entry.get("destination_paths")
    if isinstance(raw_many, list):
        values.extend(str(value) for value in raw_many if value)
    raw_one = entry.get("destination_path")
    if raw_one:
        values.append(str(raw_one))
    return list(dict.fromkeys(values))


def _entry_matches_resume_context(entry: Dict[str, Any], identity_signature: str, run_mode: str) -> bool:
    return (
        str(entry.get("status", "")) in _RESUME_FINAL_STATUSES
        and _entry_identity_compatible(entry, identity_signature)
        and str(entry.get("run_mode", "")) == str(run_mode)
    )


class ExactResumeTracker:
    """Stable manifest + append-only completion journal.

    The manifest keeps the original order. Newly discovered files are appended,
    even when their names sort before older files. The journal records each
    completed index, so multi-threaded out-of-order completion resumes from the
    first truly unfinished item rather than guessing from filenames.
    """

    def __init__(
        self,
        state_path: Path,
        source_root: Path,
        mode: str,
        matching_signature: str,
        run_mode: str,
        current_paths: List[Path],
        legacy_entries: Optional[Dict[str, Dict[str, Any]]] = None,
        identity_signature: Optional[str] = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.source_root = Path(source_root).resolve()
        self.mode = str(mode)
        # matching_signature is still recorded for informational/checkpoint
        # purposes (it shows exactly which threshold/margins produced a
        # result). identity_signature is what actually gates whether a
        # prior result can be reused - it excludes runtime tuning knobs.
        self.matching_signature = str(matching_signature)
        self.identity_signature = str(identity_signature) if identity_signature else str(matching_signature)
        self.run_mode = str(run_mode)
        self.context_key = _resume_context_key(self.source_root, self.mode, self.identity_signature, self.run_mode)
        self.manifest_path = self.state_path.with_name(self.state_path.stem + ".resume_manifest.json")
        self.journal_path = self.state_path.with_name(self.state_path.stem + ".resume_journal.jsonl")
        self.lock = threading.RLock()
        self.events: Dict[str, Dict[str, Any]] = {}
        self._journal_handle: Optional[Any] = None
        self._writes_since_sync = 0
        self._last_sync_at = 0.0

        self._load_journal()
        current_map = {path.relative_to(self.source_root).as_posix(): path for path in current_paths}
        old_order: List[str] = []
        try:
            old_manifest = load_json(self.manifest_path)
            if str(old_manifest.get("context_key", "")) == self.context_key:
                old_order = [str(value) for value in old_manifest.get("order", []) if isinstance(value, str)]
            else:
                self._reset_journal()
        except Exception:
            self._reset_journal()

        legacy_entries = legacy_entries or {}
        legacy_moved: List[str] = []
        for rel, entry in legacy_entries.items():
            if rel in current_map or not _entry_matches_resume_context(entry, self.identity_signature, self.run_mode):
                continue
            if str(entry.get("status", "")) not in _RESUME_MOVED_STATUSES:
                continue
            destinations = _entry_destinations(entry)
            if destinations or str(entry.get("status")) == "matched":
                legacy_moved.append(rel)
                if rel not in self.events:
                    self.events[rel] = self._event_from_entry(rel, entry, moved=True)

        preserved: List[str] = []
        seen: Set[str] = set()
        for rel in old_order:
            event = self.events.get(rel, {})
            if rel in current_map or (event.get("completed") and event.get("moved")) or rel in legacy_moved:
                if rel not in seen:
                    preserved.append(rel)
                    seen.add(rel)
        for rel in sorted(legacy_moved, key=str.casefold):
            if rel not in seen:
                preserved.append(rel)
                seen.add(rel)
        for rel in sorted(current_map, key=str.casefold):
            if rel not in seen:
                preserved.append(rel)
                seen.add(rel)

        self.order = preserved
        self.index_by_rel = {rel: index for index, rel in enumerate(self.order)}
        self.current_map = current_map
        self.completed_indices: Set[int] = set()
        for rel, event in self.events.items():
            index = self.index_by_rel.get(rel)
            if index is not None and self._event_data_is_current(event, self.current_map.get(rel)):
                self.completed_indices.add(int(index))
        self.cursor_index = 0
        while self.cursor_index in self.completed_indices:
            self.cursor_index += 1
        self._save_manifest()
        self._open_journal()

    def _reset_journal(self) -> None:
        self.events.clear()
        try:
            self.journal_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_journal(self) -> None:
        self.events.clear()
        if not self.journal_path.exists():
            return
        try:
            with open(self.journal_path, "r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict) or str(event.get("context_key", "")) != self.context_key:
                        continue
                    rel = str(event.get("relative_path", ""))
                    if rel:
                        self.events[rel] = event
        except OSError as exc:
            logger.warning(f"Resume journal load failed: {exc}")

    def _open_journal(self) -> None:
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)
        self._journal_handle = open(self.journal_path, "a", encoding="utf-8", buffering=1)

    def _save_manifest(self) -> None:
        snapshot = self.progress_snapshot() if hasattr(self, "current_map") else {
            "total": len(getattr(self, "order", [])), "completed": 0, "remaining": len(getattr(self, "order", [])),
            "source_remaining": len(getattr(self, "current_map", {})), "moved_out": 0, "completed_in_source": 0,
        }
        save_json_atomic(self.manifest_path, {
            "version": RESUME_JOURNAL_VERSION,
            "context_key": self.context_key,
            "mode": self.mode,
            "source_folder": str(self.source_root),
            "matching_signature": self.matching_signature,
            "run_mode": self.run_mode,
            "order": self.order,
            "total": int(snapshot["total"]),
            "completed": int(snapshot["completed"]),
            "remaining": int(snapshot["remaining"]),
            "source_remaining": int(snapshot["source_remaining"]),
            "moved_out": int(snapshot["moved_out"]),
            "completed_in_source": int(snapshot["completed_in_source"]),
            "stored_statuses": "journal stores both moved and not-moved completed items; moved=true identifies files removed from Source",
            "updated_at": now_iso(),
        })

    def _event_from_entry(self, rel: str, entry: Dict[str, Any], moved: bool = False) -> Dict[str, Any]:
        status = str(entry.get("status", ""))
        return {
            "version": RESUME_JOURNAL_VERSION,
            "context_key": self.context_key,
            "relative_path": rel,
            "index": int(self.index_by_rel.get(rel, -1)) if hasattr(self, "index_by_rel") else -1,
            "status": status,
            "completed": status in _RESUME_FINAL_STATUSES,
            "moved": bool(moved or (_entry_destinations(entry) and status in _RESUME_MOVED_STATUSES)),
            "fingerprint": dict(entry.get("fingerprint") or {}),
            "destination_paths": _entry_destinations(entry),
            "checked_at": str(entry.get("checked_at") or now_iso()),
        }

    def ordered_current_paths(self) -> List[Path]:
        return [self.current_map[rel] for rel in self.order if rel in self.current_map]

    def _event_data_is_current(self, event: Dict[str, Any], path: Optional[Path]) -> bool:
        if not isinstance(event, dict) or not bool(event.get("completed")):
            return False
        if path is None or not path.exists():
            return bool(event.get("moved"))
        fingerprint = event.get("fingerprint")
        return isinstance(fingerprint, dict) and _fingerprint_matches(fingerprint, path)

    def event_is_current(self, rel: Path, path: Optional[Path]) -> bool:
        event = self.events.get(rel.as_posix())
        return isinstance(event, dict) and self._event_data_is_current(event, path)

    def missing_completed_count(self) -> int:
        return sum(
            1 for rel, event in self.events.items()
            if rel in self.index_by_rel and rel not in self.current_map
            and bool(event.get("completed")) and bool(event.get("moved"))
        )

    def moved_completed_count(self) -> int:
        return sum(
            1 for rel, event in self.events.items()
            if rel in self.index_by_rel and bool(event.get("completed")) and bool(event.get("moved"))
        )

    def progress_snapshot(self) -> Dict[str, int]:
        """Return count-correct progress when moved files disappear from Source.

        The effective total is always: files currently remaining in Source +
        files already moved out by this resume context. A moved photo is thus
        subtracted exactly once from the remaining work, never twice.
        """
        with self.lock:
            moved_out = self.missing_completed_count()
            completed_in_source = 0
            for rel, path in self.current_map.items():
                event = self.events.get(rel)
                if isinstance(event, dict) and self._event_data_is_current(event, path):
                    completed_in_source += 1
            source_remaining = len(self.current_map)
            total = source_remaining + moved_out
            completed = moved_out + completed_in_source
            remaining = max(0, source_remaining - completed_in_source)
            return {
                "total": int(total),
                "completed": int(completed),
                "remaining": int(remaining),
                "source_remaining": int(source_remaining),
                "moved_out": int(moved_out),
                "completed_in_source": int(completed_in_source),
            }

    def current_event(self, rel: Path) -> Optional[Dict[str, Any]]:
        event = self.events.get(rel.as_posix())
        return dict(event) if isinstance(event, dict) else None

    def seed_entry(self, rel: Path, entry: Dict[str, Any], moved: bool = False) -> None:
        if self.event_is_current(rel, self.current_map.get(rel.as_posix())):
            return
        event = self._event_from_entry(rel.as_posix(), entry, moved=moved)
        event["index"] = int(self.index_by_rel.get(rel.as_posix(), -1))
        self._append_event(event, force_sync=False)

    def append_result(self, rel: Path, status: str, entry: Dict[str, Any], done: int, total: int) -> None:
        moved = bool(_entry_destinations(entry) and str(status) in _RESUME_MOVED_STATUSES)
        event = self._event_from_entry(rel.as_posix(), {**entry, "status": status}, moved=moved)
        event.update({
            "index": int(self.index_by_rel.get(rel.as_posix(), -1)),
            "done": int(done),
            "total": int(total),
            "progress_percent": round((int(done) / max(1, int(total))) * 100.0, 6),
            "saved_at": now_iso(),
        })
        self._append_event(event, force_sync=False)

    def _append_event(self, event: Dict[str, Any], force_sync: bool = False) -> None:
        rel = str(event.get("relative_path", ""))
        if not rel:
            return
        with self.lock:
            self.events[rel] = dict(event)
            current_path = self.current_map.get(rel)
            if bool(event.get("completed")) and bool(event.get("moved")):
                if current_path is None or not current_path.exists():
                    self.current_map.pop(rel, None)
            index = self.index_by_rel.get(rel)
            if index is not None:
                if self._event_data_is_current(event, self.current_map.get(rel)):
                    self.completed_indices.add(int(index))
                else:
                    self.completed_indices.discard(int(index))
                    if int(index) < self.cursor_index:
                        self.cursor_index = int(index)
                while self.cursor_index in self.completed_indices:
                    self.cursor_index += 1
            if self._journal_handle is None:
                self._open_journal()
            assert self._journal_handle is not None
            self._journal_handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
            self._journal_handle.flush()
            self._writes_since_sync += 1
            now = time.monotonic()
            if force_sync or self._writes_since_sync >= 8 or (now - self._last_sync_at) >= 1.0:
                try:
                    os.fsync(self._journal_handle.fileno())
                except OSError:
                    pass
                self._writes_since_sync = 0
                self._last_sync_at = now
                try:
                    self._save_manifest()
                except Exception as exc:
                    logger.warning(f"Resume manifest summary save failed: {exc}")

    def first_pending_index(self) -> int:
        return min(len(self.order), int(self.cursor_index))

    def close(self) -> None:
        with self.lock:
            if self._journal_handle is not None:
                try:
                    self._journal_handle.flush()
                    os.fsync(self._journal_handle.fileno())
                except OSError:
                    pass
                try:
                    self._journal_handle.close()
                except OSError:
                    pass
                self._journal_handle = None


def _main_profile_entry(profile: Dict[str, Any], rel: Path) -> Dict[str, Any]:
    return dict(_profile_entry(profile, rel) or {})


def _router_profile_entries(router_state: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {str(key): value for key, value in router_state.get("files", {}).items() if isinstance(value, dict)}


def record_scan_result(rel_path: Path, status: str, **extra: Any) -> None:
    extra = dict(extra)
    mode = str(extra.pop("_mode", "scan"))
    run_mode_value = str(extra.pop("run_mode", "real"))
    matching_sig = str(extra.pop("matching_signature", getattr(state, "current_matching_signature", "")))
    identity_sig = str(extra.pop("identity_signature", getattr(state, "current_identity_signature", "")))
    with state.state_lock:
        profile = ensure_profile()
        folder_name = folder_key_from_rel(rel_path)
        bucket = profile["folders"].setdefault(folder_name, {})
        existing = bucket.get(rel_path.name, {})
        entry = {
            "file_name": rel_path.name,
            "relative_path": rel_path.as_posix(),
            "status": status,
            "checked_at": now_iso(),
            "started_at": existing.get("started_at"),
            "attempts": existing.get("attempts", 1),
            "model_signature": _current_model_signature(),
            "reference_signature": getattr(state, "current_ref_signature", ""),
            "threshold": round(float(getattr(state, "current_threshold", 0.0)), 6),
            "run_mode": run_mode_value,
            "matching_signature": matching_sig,
            "identity_signature": identity_sig,
            "recognition_signature": str(
                extra.get("recognition_signature", getattr(state, "current_recognition_signature", ""))
            ),
            **extra,
        }
        bucket[rel_path.name] = entry
        profile["updated_at"] = now_iso()
        persist_state()
    if status != "review":
        db_cancel_pending_reviews(mode, str(extra.get("source_path", "")), rel_path.as_posix())
    db_record_file_result(
        getattr(state, "active_scan_id", ""), mode, rel_path.as_posix(),
        str(extra.get("source_path", "")), status,
        list(extra.get("destination_paths") or ([extra["destination_path"]] if extra.get("destination_path") else [])),
        float(extra.get("best_similarity", 0.0)), float(extra.get("second_similarity", 0.0)),
        list(extra.get("top_matches") or []), dict(extra.get("fingerprint") or {}),
        int(extra.get("face_count", 0)), str(extra.get("error", "")),
    )
    if status in {"nonmatched", "review_rejected"}:
        fp = dict(extra.get("fingerprint") or {})
        recognition_sig = str(
            extra.get("recognition_signature", getattr(state, "current_recognition_signature", ""))
        )
        db_upsert_checked_nonmatch(
            getattr(state, "src_folder", ""), rel_path, "scan", recognition_sig, fp, "nonmatched",
            getattr(state, "current_ref_signature", ""), getattr(state, "current_threshold", 0.0),
        )
        index = getattr(state, "checked_nonmatch_index", None)
        if isinstance(index, CheckedNonmatchIndex):
            index.remember(rel_path, fp, "nonmatched")


def record_router_scan_result(
    router_state: Dict[str, Any],
    router_state_path: Optional[Path],
    router_state_lock: threading.RLock,
    rel_path: Path,
    status: str,
    **extra: Any,
) -> None:
    if router_state_path is None:
        raise RuntimeError("Router resume state path არ არის მომზადებული")
    extra = dict(extra)
    run_mode_value = str(extra.pop("run_mode", "real"))
    matching_sig = str(extra.pop("matching_signature", router_state.get("matching_signature", "")))
    identity_sig = str(extra.pop("identity_signature", router_state.get("identity_signature", "")))
    with router_state_lock:
        bucket = router_state.setdefault("files", {})
        key = rel_path.as_posix()
        existing = bucket.get(key, {})
        bucket[key] = {
            "relative_path": key,
            "status": status,
            "checked_at": now_iso(),
            "started_at": existing.get("started_at"),
            "attempts": existing.get("attempts", 1),
            "model_signature": _current_model_signature(),
            "router_signature": router_state.get("signature", ""),
            "threshold": round(float(router_state.get("threshold", 0.0)), 6),
            "run_mode": run_mode_value,
            "matching_signature": matching_sig,
            "identity_signature": identity_sig,
            "recognition_signature": str(
                extra.get("recognition_signature", router_state.get("recognition_signature", ""))
            ),
            **extra,
        }
        router_state["updated_at"] = now_iso()
        persist_json_state(router_state_path, router_state)
    if status != "review":
        db_cancel_pending_reviews("router", str(extra.get("source_path", "")), rel_path.as_posix())
    db_record_file_result(
        str(router_state.get("active_scan_id", "")), "router", rel_path.as_posix(),
        str(extra.get("source_path", "")), status,
        list(extra.get("destination_paths") or ([extra["destination_path"]] if extra.get("destination_path") else [])),
        float(extra.get("best_similarity", 0.0)), float(extra.get("second_similarity", 0.0)),
        list(extra.get("top_matches") or []), dict(extra.get("fingerprint") or {}),
        int(extra.get("face_count", 0)), str(extra.get("error", "")),
    )
    if status in {"unmatched", "review_rejected"}:
        fp = dict(extra.get("fingerprint") or {})
        recognition_sig = str(extra.get("recognition_signature", router_state.get("recognition_signature", "")))
        db_upsert_checked_nonmatch(
            router_state.get("source_folder", ""), rel_path, "router", recognition_sig, fp, "unmatched",
            str(router_state.get("signature", "")), float(router_state.get("threshold", 0.0)),
        )


def _ensure_main_identities() -> List[Dict[str, Any]]:
    identities = list(getattr(state, "main_identities", []) or [])
    if identities:
        return identities
    fallback: List[Dict[str, Any]] = []
    for index, (ref_path, embedding) in enumerate(zip(state.ref_files, state.ref_embs), start=1):
        matrix, centroid = _identity_profile([embedding])
        fallback.append({
            "index": index,
            "name": Path(ref_path).stem,
            "files": [str(ref_path)],
            "matrix": matrix,
            "centroid": centroid,
        })
    state.main_identities = fallback
    return fallback


def _top_reference_matches(embeddings: np.ndarray) -> Tuple[float, float, List[Dict[str, Any]]]:
    identities = _ensure_main_identities()
    if embeddings.size == 0 or not identities:
        return 0.0, 0.0, []
    scores: List[float] = []
    for identity in identities:
        score = 0.0
        for embedding in embeddings:
            score = max(score, score_embedding_to_identity(embedding, identity["matrix"], identity["centroid"]))
        scores.append(score)
    order = np.argsort(np.asarray(scores))[::-1][:3]
    top = [
        {
            "index": int(position),
            "identity_index": int(identities[int(position)].get("index", int(position) + 1)),
            "name": str(identities[int(position)].get("name", f"ადამიანი #{int(position)+1}")),
            "reference": str((identities[int(position)].get("files") or [""])[0]),
            "score": round(float(scores[int(position)]), 6),
        }
        for position in order
    ]
    best = float(top[0]["score"]) if top else 0.0
    second = float(top[1]["score"]) if len(top) > 1 else 0.0
    return best, second, top


def _load_image_with_retry(path: Path, attempts: int = 3) -> np.ndarray:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            image = load_image_bgr(path)
            if image is not None:
                return image
            raise ValueError("დაზიანებული, ჩაკეტილი ან მხარდაუჭერელი ფოტო")
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.18 * attempt)
    raise last_error or ValueError("ფოტო ვერ ჩაიტვირთა")


def _get_embeddings_with_retry(path: Path, image: np.ndarray, fingerprint: Dict[str, Any], attempts: int = 2) -> np.ndarray:
    last_error: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return get_cached_face_embeddings(path, img=image, fingerprint=fingerprint)
        except Exception as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(0.20 * attempt)
    raise last_error or RuntimeError("Embedding ვერ გამოითვალა")


def process_main_file(f: Path, threshold: float, live_mode: bool = False) -> Dict[str, Any]:
    if not _wait_while_paused():
        return {"status": "cancelled"}
    source_root = Path(state.src_folder).resolve()
    try:
        rel = f.resolve().relative_to(source_root)
    except Exception as exc:
        logger.error(f"Source-ის გარეთ არსებული ფაილი გამოტოვებულია: {f}: {exc}")
        return {"status": "error", "error": str(exc)}

    mode = "live" if live_mode else "scan"
    best_score = second_score = 0.0
    top_matches: List[Dict[str, Any]] = []
    fingerprint: Dict[str, Any] = {}
    face_count = 0
    mark_scan_in_progress(rel, source_path=str(f))
    try:
        image = _load_image_with_retry(f, attempts=3)
        fingerprint = file_fingerprint(f, include_sha=False)
        ensure_fingerprint_sha(fingerprint, f)

        duplicate_index = getattr(state, "duplicate_index", None)
        if not isinstance(duplicate_index, DuplicateIndex):
            duplicate_index = DuplicateIndex()
            state.duplicate_index = duplicate_index
        duplicate, duplicate_status, duplicate_note = duplicate_index.check_and_add(fingerprint, image)
        if duplicate:
            with state.progress_lock:
                state.stats.duplicates += 1
            destination = ""
            if str(getattr(state, "duplicate_mode", "მხოლოდ ანგარიშში")) == "ცალკე საქაღალდეში":
                _set_operation_context(state.active_scan_id, mode, f, rel)
                try:
                    destination = str(move_file_safely(f, Path(state.out_folder) / "_duplicates" / rel, fingerprint))
                finally:
                    _clear_operation_context()
            record_scan_result(
                rel, duplicate_status, _mode=mode, fingerprint=fingerprint, source_path=str(f),
                destination_path=destination, note=duplicate_note,
            )
            return {"status": duplicate_status, "rel": rel}

        embeddings = _get_embeddings_with_retry(f, image, fingerprint, attempts=2)
        face_count = int(embeddings.shape[0])
        best_score, second_score, top_matches = _top_reference_matches(embeddings)
        identity_count = len(_ensure_main_identities())
        gap = best_score - second_score
        confident = best_score >= threshold and (identity_count <= 1 or gap >= config.ambiguity_margin)
        review_floor = max(0.0, threshold - _review_margin())

        if confident:
            with state.set_lock:
                state.matched.add(str(fingerprint.get("sha256", "")))
            _set_operation_context(state.active_scan_id, mode, f, rel)
            try:
                destination = move_file_safely(f, Path(state.out_folder) / rel, fingerprint)
            finally:
                _clear_operation_context()
            db_insert_match(state.ref_db_value or build_reference_db_value(state.ref_files), str(f))
            record_scan_result(
                rel, "matched", _mode=mode, fingerprint=fingerprint, source_path=str(f),
                destination_path=str(destination), best_similarity=best_score,
                second_similarity=second_score, top_matches=top_matches, face_count=face_count,
            )
            return {"status": "matched", "rel": rel, "score": best_score, "destination": str(destination)}

        should_review = face_count > 0 and (
            best_score >= review_floor or (best_score >= threshold and identity_count > 1 and gap < config.ambiguity_margin)
        )
        if should_review:
            review_id = db_add_review(
                state.active_scan_id, mode, str(f), rel.as_posix(), best_score, second_score,
                top_matches, {"destinations": [str(Path(state.out_folder) / rel)], "action": "move"},
            )
            state.review_count = int(getattr(state, "review_count", 0)) + 1
            record_scan_result(
                rel, "review", _mode=mode, fingerprint=fingerprint, source_path=str(f),
                best_similarity=best_score, second_similarity=second_score,
                top_matches=top_matches, face_count=face_count, review_id=review_id,
                ambiguity_gap=round(gap, 6),
            )
            return {"status": "review", "rel": rel, "score": best_score}

        with state.set_lock:
            state.nonmatched.add(str(fingerprint.get("sha256", "")))
        record_scan_result(
            rel, "nonmatched", _mode=mode, fingerprint=fingerprint, source_path=str(f),
            best_similarity=best_score, second_similarity=second_score,
            top_matches=top_matches, face_count=face_count,
        )
        return {"status": "nonmatched", "rel": rel, "score": best_score}
    except Exception as exc:
        _clear_operation_context()
        with state.progress_lock:
            state.stats.errors += 1
        logger.error(f"Error processing {f}: {exc}")
        record_scan_result(
            rel, "error", _mode=mode, fingerprint=fingerprint, source_path=str(f), error=str(exc),
            best_similarity=best_score, second_similarity=second_score,
            top_matches=top_matches, face_count=face_count, retryable=True,
        )
        return {"status": "error", "rel": rel, "error": str(exc)}


def worker(
    threshold: float,
    pbar: tqdm,
    total_files: int,
    start_time: float,
    progress_bar: Progressbar,
    progress_label: Label,
) -> None:
    while True:
        item = state.file_q.get()
        try:
            if item is None:
                return
            if state.stop_requested.is_set():
                continue
            started = time.time()
            throttle = resource_throttle_delay(
                str(getattr(state, "performance_profile", "ავტომატური"))
            )
            if throttle > 0:
                time.sleep(throttle)
            result = process_main_file(Path(item), threshold)
            elapsed = time.time() - started
            with state.progress_lock:
                state.time_records.append(elapsed)
                if len(state.time_records) > config.time_records_max:
                    state.time_records.pop(0)
                state.progress_completed += 1
                done_now = state.progress_completed
            tracker = getattr(state, "resume_tracker", None)
            if isinstance(tracker, ExactResumeTracker) and isinstance(result, dict):
                result_rel = result.get("rel")
                if isinstance(result_rel, Path):
                    profile = state.current_profile if isinstance(state.current_profile, dict) else {}
                    entry = _main_profile_entry(profile, result_rel)
                    tracker.append_result(
                        result_rel, str(result.get("status", entry.get("status", "error"))),
                        entry, done_now, total_files,
                    )
                    snapshot = tracker.progress_snapshot()
                    with state.progress_lock:
                        state.progress_completed = int(snapshot["completed"])
                        done_now = int(snapshot["completed"])
                    if isinstance(profile, dict):
                        profile["resume_checkpoint"] = {
                            "version": RESUME_JOURNAL_VERSION,
                            "total": int(snapshot["total"]),
                            "completed": int(snapshot["completed"]),
                            "remaining": int(snapshot["remaining"]),
                            "source_remaining": int(snapshot["source_remaining"]),
                            "moved_count": int(snapshot["moved_out"]),
                            "checked_but_not_moved": int(snapshot["completed_in_source"]),
                            "progress_percent": round((int(snapshot["completed"]) / max(1, int(snapshot["total"]))) * 100.0, 6),
                            "last_completed_relative_path": result_rel.as_posix(),
                            "last_completed_status": str(result.get("status", "")),
                            "next_index": min(int(snapshot["total"]), tracker.first_pending_index()) + (1 if tracker.first_pending_index() < int(snapshot["total"]) else 0),
                            "last_saved_at": now_iso(),
                            "manifest_file": tracker.manifest_path.name,
                            "journal_file": tracker.journal_path.name,
                        }
            schedule_scan_progress_update(
                pbar, total_files, start_time, progress_bar, progress_label,
                force=(done_now >= total_files),
            )
        except Exception as exc:
            logger.exception(f"Worker-ის მოულოდნელი შეცდომა: {exc}")
            with state.progress_lock:
                state.stats.errors += 1
                state.progress_completed += 1
        finally:
            state.file_q.task_done()


def _effective_worker_count(requested: int, profile: str) -> int:
    cpu = max(1, psutil.cpu_count(logical=True) or 4)
    memory_gb = max(1.0, psutil.virtual_memory().total / (1024 ** 3))
    provider = str(providers[0]) if providers else "CPUExecutionProvider"
    # Each worker now runs inference on its own FaceAnalysis instance (see
    # _get_thread_face_app), so workers genuinely parallelize CPU-bound
    # detection/embedding work, not just disk decode/hash. DirectML devices
    # still share one physical GPU queue underneath, so we keep a tighter
    # cap there for stability; CPU provider scales with core count.
    provider_cap = 4 if "Dml" in provider else min(8, max(2, cpu // 2))
    memory_cap = max(1, int(memory_gb // 2.2))
    if profile == "ეკონომიური":
        cap = min(2, provider_cap)
    elif profile == "დაბალანსებული":
        cap = min(provider_cap, max(2, cpu // 3), memory_cap)
    elif profile == "მაქსიმალური":
        cap = min(provider_cap, memory_cap)
    else:
        cap = min(provider_cap, max(2, cpu // 3), memory_cap)
    return max(1, min(int(requested), int(cap)))


def get_selected_worker_count() -> int:
    try:
        requested = max(config.worker_min, min(config.worker_max, int(worker_var.get())))
    except Exception:
        requested = config.worker_count
    profile = str(_global_tk_value("performance_profile_var", "ავტომატური"))
    config.worker_count = _effective_worker_count(requested, profile)
    return config.worker_count


def _build_identity(name: str, files: List[str]) -> Dict[str, Any]:
    embeddings = [get_emb(path) for path in files]
    matrix, centroid = _identity_profile(embeddings)
    return {
        "index": 0,
        "name": name,
        "files": files,
        "embeddings": embeddings,
        "matrix": matrix,
        "centroid": centroid,
    }


def pick_refs() -> None:
    """Select references per identity, preventing cross-person ambiguity."""
    try:
        raw = simpledialog.askstring("საცნობარო ფოტოები", "რამდენი ადამიანის სახეს ეძებ?")
        if raw is None:
            return
        people_count = int(raw)
        if people_count <= 0:
            raise ValueError
    except ValueError:
        messagebox.showerror("შეცდომა", "შეიყვანე 0-ზე მეტი მთელი რიცხვი")
        return

    stop_live_watch(wait=True)
    ensure_face_engine(_global_tk_value("model_profile_var"))
    identities: List[Dict[str, Any]] = []
    all_files: List[str] = []
    all_embeddings: List[np.ndarray] = []
    warnings: List[str] = []
    used: Set[str] = set()
    for index in range(1, people_count + 1):
        # სახელის ხელით შეყვანა აღარ მოითხოვება.
        # შიდა ლოგიკისთვის გამოიყენება ავტომატური ტექნიკური სახელი.
        name = f"ადამიანი #{index}"
        chosen = list(dict.fromkeys(filedialog.askopenfilenames(
            title=f"აირჩიე ერთი ან რამდენიმე reference ფოტო — ადამიანი #{index}",
            filetypes=[("სურათები", "*.jpg *.jpeg *.png *.bmp *.webp *.tif *.tiff *.heic *.heif *.avif *.dng *.cr2 *.nef *.arw *.rw2")],
        )))
        if not chosen:
            messagebox.showerror("შეცდომა", f"{name}-ს მინიმუმ ერთი reference ფოტო სჭირდება")
            root.after(100, ensure_live_watch_started)
            return
        valid_files: List[str] = []
        valid_embeddings: List[np.ndarray] = []
        for path in chosen:
            resolved = str(Path(path).resolve())
            if resolved in used:
                warnings.append(f"{Path(path).name}: სხვა ადამიანთან უკვე გამოყენებულია")
                continue
            try:
                quality, _details = check_face_quality(path)
                if quality < 40:
                    warnings.append(f"{Path(path).name}: უარყოფილია ({quality}%)")
                    continue
                if quality < 65:
                    warnings.append(f"{Path(path).name}: დაბალი reference ხარისხი ({quality}%)")
                embedding = get_emb(path)
                valid_files.append(path)
                valid_embeddings.append(embedding)
                used.add(resolved)
            except Exception as exc:
                warnings.append(f"{Path(path).name}: {exc}")
        if not valid_files:
            messagebox.showerror("შეცდომა", f"{name}-ს ვარგისი reference ფოტო არ დარჩა")
            root.after(100, ensure_live_watch_started)
            return
        matrix, centroid = _identity_profile(valid_embeddings)
        identities.append({
            "index": index,
            "name": name.strip() or f"ადამიანი #{index}",
            "files": valid_files,
            "embeddings": valid_embeddings,
            "matrix": matrix,
            "centroid": centroid,
        })
        all_files.extend(valid_files)
        all_embeddings.extend(valid_embeddings)

    state.main_identities = identities
    state.ref_files = all_files
    state.ref_embs = all_embeddings
    state.ref_embs_matrix = np.vstack(all_embeddings).astype(np.float32)
    state.ref_db_value = build_reference_db_value(all_files)
    state.current_ref_signature = ref_signature(all_files)
    state.reference_model_signature = _current_model_signature()
    state.reference_content_signature = ref_signature(state.ref_files)
    lbl_ref.config(text=f"ჩატვირთულია {len(all_files)} reference ფოტო | იდენტობა: {len(identities)}")
    save_app_settings()
    root.after(200, ensure_live_watch_started)
    if warnings:
        messagebox.showwarning("Reference გაფრთხილება", "\n".join(warnings[:15]))


def auto_calibrate_threshold() -> None:
    identities = _ensure_main_identities()
    if not identities:
        messagebox.showerror("შეცდომა", "ჯერ reference ფოტოები აირჩიე")
        return
    positives: List[float] = []
    negatives: List[float] = []
    for identity in identities:
        matrix = np.asarray(identity["matrix"], dtype=np.float32)
        if matrix.shape[0] > 1:
            sims = matrix @ matrix.T
            for row in range(matrix.shape[0]):
                for col in range(row + 1, matrix.shape[0]):
                    positives.append(float(sims[row, col]))
    for first in range(len(identities)):
        for second in range(first + 1, len(identities)):
            negatives.append(float(np.max(identities[first]["matrix"] @ identities[second]["matrix"].T)))

    if positives and negatives:
        positive_low = float(np.percentile(positives, 10))
        negative_high = float(np.percentile(negatives, 95))
        if positive_low > negative_high:
            balanced = float(np.clip((positive_low + negative_high) / 2.0, 0.36, 0.60))
        else:
            balanced = float(np.clip(max(negative_high + 0.045, np.median(positives) - 0.10), 0.42, 0.60))
        separation = positive_low - negative_high
        suggested_gap = float(np.clip(max(0.025, separation / 3.0), 0.025, 0.10))
        reason = f"შიდა P10={positive_low:.3f}, სხვა პირის P95={negative_high:.3f}"
    elif negatives:
        negative_high = float(np.percentile(negatives, 95))
        balanced = float(np.clip(negative_high + 0.07, 0.42, 0.60))
        suggested_gap = 0.035
        reason = f"თითო ადამიანზე ერთი reference; სხვა პირის P95={negative_high:.3f}"
    elif positives:
        positive_low = float(np.percentile(positives, 10))
        balanced = float(np.clip(positive_low - 0.09, 0.38, 0.56))
        suggested_gap = 0.0
        reason = f"ერთი იდენტობა; შიდა P10={positive_low:.3f}"
    else:
        balanced, suggested_gap = 0.47, 0.035
        reason = "მონაცემი მცირეა — გამოყენებულია უსაფრთხო ნაგულისხმევი ზღვარი"
    slider.set(int(round(balanced * 100)))
    if globals().get("ambiguity_margin_var"):
        ambiguity_margin_var.set(f"{suggested_gap:.3f}")
    threshold_hint_label.config(
        text=f"ფართო: {max(0.32, balanced-0.05):.2f} | დაბალანსებული: {balanced:.2f} | "
             f"მკაცრი: {min(0.62, balanced+0.06):.2f} — {reason}"
    )
    save_app_settings()


def save_app_settings() -> None:
    try:
        identities_payload = [
            {"name": str(item.get("name", "")), "files": list(item.get("files", []))}
            for item in (getattr(state, "main_identities", []) or [])
        ]
        worker_widget = globals().get("worker_var")
        threshold_widget = globals().get("slider")
        duplicate_widget = globals().get("duplicate_distance_var")
        ambiguity_widget = globals().get("ambiguity_margin_var")
        review_widget = globals().get("review_margin_var")
        data = dict(APP_SETTINGS)
        data.pop("dry_run", None)
        data.pop("auto_report", None)
        data.update({
            "version": APP_VERSION,
            "source_folder": state.src_folder,
            "output_folder": state.out_folder,
            "reference_files": list(state.ref_files),
            "reference_identities": identities_payload,
            "worker_count": _safe_int(
                worker_widget.get() if worker_widget else config.worker_count,
                _safe_int(data.get("worker_count"), config.worker_count), config.worker_min, config.worker_max,
            ),
            "threshold": _safe_int(
                threshold_widget.get() if threshold_widget else config.threshold_default,
                _safe_int(data.get("threshold"), config.threshold_default), config.threshold_min, config.threshold_max,
            ),
            "performance_profile": _global_tk_value("performance_profile_var", "ავტომატური"),
            "model_profile": _global_tk_value("model_profile_var", "მაქსიმალური სიზუსტე"),
            "live_watch_always_enabled": True,
            "duplicate_mode": _global_tk_value("duplicate_mode_var", "მხოლოდ ანგარიშში"),
            "duplicate_distance": _safe_int(
                duplicate_widget.get() if duplicate_widget else config.duplicate_phash_distance,
                _safe_int(data.get("duplicate_distance"), config.duplicate_phash_distance), 0, 16,
            ),
            "ambiguity_margin": _safe_float(
                ambiguity_widget.get() if ambiguity_widget else config.ambiguity_margin,
                _safe_float(data.get("ambiguity_margin"), config.ambiguity_margin), 0.0, 0.20,
            ),
            "review_margin": _safe_float(
                review_widget.get() if review_widget else config.review_margin,
                _safe_float(data.get("review_margin"), config.review_margin), 0.01, 0.12,
            ),
            "cpu_threshold": _safe_int(config.cpu_threshold, 85, 20, 100),
            "memory_threshold": _safe_int(config.memory_threshold, 85, 20, 100),
            "det_size": list(_safe_det_size(FACE_ENGINE_DET_SIZE, config.det_size)),
        })
        save_json_atomic(APP_SETTINGS_PATH, data)
        APP_SETTINGS.clear()
        APP_SETTINGS.update(data)
    except Exception as exc:
        logger.warning(f"Settings save failed: {exc}")


def _reload_main_identities_for_model() -> None:
    identities = list(getattr(state, "main_identities", []) or [])
    if not identities:
        identities = [{"index": i + 1, "name": Path(path).stem, "files": [path]} for i, path in enumerate(state.ref_files)]
    all_embeddings: List[np.ndarray] = []
    rebuilt: List[Dict[str, Any]] = []
    for index, identity in enumerate(identities, start=1):
        files = [str(path) for path in identity.get("files", []) if Path(path).exists()]
        if not files:
            continue
        embeddings = [get_emb(path) for path in files]
        matrix, centroid = _identity_profile(embeddings)
        rebuilt.append({
            "index": int(identity.get("index", index)), "name": str(identity.get("name", f"ადამიანი #{index}")),
            "files": files, "embeddings": embeddings, "matrix": matrix, "centroid": centroid,
        })
        all_embeddings.extend(embeddings)
    if not rebuilt:
        raise ValueError("ვერცერთი reference იდენტობა ვერ ჩაიტვირთა")
    state.main_identities = rebuilt
    state.ref_files = [path for item in rebuilt for path in item["files"]]
    state.ref_embs = all_embeddings
    state.ref_embs_matrix = np.vstack(all_embeddings).astype(np.float32)
    state.ref_db_value = build_reference_db_value(state.ref_files)
    state.current_ref_signature = ref_signature(state.ref_files)
    state.reference_model_signature = _current_model_signature()
    state.reference_content_signature = ref_signature(state.ref_files)


def start_scan() -> None:
    if state.scan_running:
        messagebox.showwarning("მიმდინარეობს", "სკანირება უკვე გაშვებულია")
        return
    if not state.ref_files or not state.src_folder or not state.out_folder:
        messagebox.showerror("შეცდომა", "აირჩიე reference ფოტოები, source და output საქაღალდეები")
        return
    stop_live_watch(wait=True)
    source_root = Path(state.src_folder).resolve()
    output_root = Path(state.out_folder).resolve()
    if not source_root.is_dir():
        messagebox.showerror("შეცდომა", "Source საქაღალდე აღარ არსებობს ან მიუწვდომელია")
        return
    try:
        output_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        messagebox.showerror("შეცდომა", f"Output საქაღალდე ვერ მომზადდა: {exc}")
        return
    if source_root == output_root:
        messagebox.showerror("შეცდომა", "Source და Output ერთი საქაღალდე ვერ იქნება")
        return
    try:
        output_root.relative_to(source_root)
        messagebox.showerror("შეცდომა", "Output source საქაღალდეში არ უნდა იყოს")
        return
    except ValueError:
        pass

    model_signature = ensure_face_engine(_global_tk_value("model_profile_var"))
    try:
        current_reference_signature = ref_signature(state.ref_files)
        references_changed = getattr(state, "reference_content_signature", "") != current_reference_signature
        if getattr(state, "reference_model_signature", "") != model_signature or references_changed:
            _reload_main_identities_for_model()
        else:
            _ensure_main_identities()
    except Exception as exc:
        messagebox.showerror("Reference შეცდომა", str(exc))
        return

    state.reset()
    state.pause_requested.clear()
    state.review_count = 0
    scan_id = f"scan-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    state.active_scan_id = scan_id
    state.duplicate_mode = str(_global_tk_value("duplicate_mode_var", "მხოლოდ ანგარიშში"))
    config.duplicate_phash_distance = _safe_int(
        duplicate_distance_var.get(), config.duplicate_phash_distance, 0, 16
    )
    config.ambiguity_margin = _safe_float(
        ambiguity_margin_var.get(), config.ambiguity_margin, 0.0, 0.20
    )
    config.review_margin = _safe_float(
        review_margin_var.get(), config.review_margin, 0.01, 0.12
    )
    duplicate_distance_var.set(str(config.duplicate_phash_distance))
    ambiguity_margin_var.set(str(config.ambiguity_margin))
    review_margin_var.set(str(config.review_margin))
    state.performance_profile = str(_global_tk_value("performance_profile_var", "ავტომატური"))
    state.current_threshold = slider.get() / 100.0
    state.current_ref_signature = ref_signature(state.ref_files)
    state.ref_embs_matrix = np.vstack(state.ref_embs).astype(np.float32)
    state.ref_db_value = build_reference_db_value(state.ref_files)
    state.current_matching_signature = _matching_signature(
        state.current_threshold, reference_signature=state.current_ref_signature
    )
    state.current_identity_signature = _identity_signature(
        reference_signature=state.current_ref_signature
    )
    state.current_recognition_signature = _recognition_signature(
        state.current_threshold, reference_signature=state.current_ref_signature
    )
    worker_count = get_selected_worker_count()
    profile = prepare_scan_state(source_root, output_root, state.current_threshold, worker_count)
    profile.pop("dry_run", None)
    profile.update({
        "version": 4,
        "active_scan_id": scan_id,
        "model_signature": model_signature,
        "reference_signature": state.current_ref_signature,
        "run_mode": "real",
        "matching_signature": state.current_matching_signature,
        "identity_signature": state.current_identity_signature,
        "recognition_signature": state.current_recognition_signature,
        "identities": [
            {"index": item["index"], "name": item["name"], "files": item["files"]}
            for item in _ensure_main_identities()
        ],
    })
    persist_state(force=True)

    db_start_scan(
        scan_id, "scan", str(source_root), [str(output_root)],
        [{"name": item["name"], "files": item["files"]} for item in _ensure_main_identities()],
        {
            "threshold": state.current_threshold, "workers": worker_count,
            "model_signature": model_signature,
            "run_mode": "real", "matching_signature": state.current_matching_signature,
            "recognition_signature": state.current_recognition_signature,
            "reference_signature": state.current_ref_signature,
            "ambiguity_margin": config.ambiguity_margin, "review_margin": _review_margin(),
            "algorithm": MATCHING_ALGORITHM_VERSION,
        },
    )

    reference_paths = {str(Path(path).resolve()) for path in state.ref_files}
    discovered_files = [
        path for path in iter_image_files(source_root)
        if str(path.resolve()) not in reference_paths
    ]
    expected_mode = "real"
    if state.scan_state_path is None:
        raise RuntimeError("Resume state path ვერ მომზადდა")
    resume_tracker = ExactResumeTracker(
        state.scan_state_path, source_root, "scan", state.current_matching_signature, expected_mode,
        discovered_files, legacy_entries=_profile_entries(profile),
        identity_signature=state.current_identity_signature,
    )
    state.resume_tracker = resume_tracker
    files = resume_tracker.ordered_current_paths()
    state.stats.resumed = resume_tracker.missing_completed_count()

    _seed_checked_nonmatches_from_main(profile, source_root, state.current_recognition_signature)
    state.checked_nonmatch_index = CheckedNonmatchIndex(
        source_root, "scan", state.current_recognition_signature
    )
    files_to_scan: List[Path] = []
    resumed_fingerprints: List[Dict[str, Any]] = []
    interrupted_count = count_in_progress_entries(profile)
    new_photo_count = 0
    changed_photo_count = 0
    reprocess_photo_count = 0
    for path in files:
        rel = path.relative_to(source_root)
        entry = _profile_entry(profile, rel)
        journal_event = resume_tracker.current_event(rel)
        if resume_tracker.event_is_current(rel, path):
            state.stats.resumed += 1
            if isinstance(journal_event, dict) and isinstance(journal_event.get("fingerprint"), dict):
                resumed_fingerprints.append(dict(journal_event["fingerprint"]))
        elif _entry_is_current(entry, path, state.current_identity_signature, expected_mode):
            state.stats.resumed += 1
            if isinstance(entry, dict):
                resume_tracker.seed_entry(rel, entry, moved=False)
                if isinstance(entry.get("fingerprint"), dict):
                    resumed_fingerprints.append(entry["fingerprint"])
        else:
            known_nonmatch, skip_reason, ledger_entry = state.checked_nonmatch_index.lookup(path, rel)
            if known_nonmatch:
                state.stats.resumed += 1
                if isinstance(ledger_entry, dict):
                    resumed_fingerprints.append(dict(ledger_entry))
                    resume_tracker.seed_entry(
                        rel,
                        {
                            "status": "nonmatched", "fingerprint": dict(ledger_entry),
                            "matching_signature": state.current_matching_signature, "run_mode": expected_mode,
                        },
                        moved=False,
                    )
                continue
            files_to_scan.append(path)
            pending_kind = _classify_pending_photo(entry, path)
            if pending_kind == "new":
                new_photo_count += 1
            elif pending_kind == "changed":
                changed_photo_count += 1
            else:
                reprocess_photo_count += 1

    append_log(
        _photo_change_log_message(
            "ფოლდერის შემოწმება",
            new_photo_count,
            changed_photo_count,
            reprocess_photo_count,
            state.stats.resumed,
        ),
        "success" if new_photo_count > 0 else "info",
    )
    append_log(
        f"სწრაფი გამოტოვება | metadata: {state.checked_nonmatch_index.stat_skips} | "
        f"hash დადასტურება: {state.checked_nonmatch_index.hash_skips} | "
        f"სახელით შედარება: გამორთულია",
        "info",
    )

    state.duplicate_index = DuplicateIndex()
    for resumed_fingerprint in resumed_fingerprints:
        state.duplicate_index.seed(resumed_fingerprint)
    snapshot = resume_tracker.progress_snapshot()
    total_files = int(snapshot["total"])
    state.stats.resumed = int(snapshot["completed"])
    state.progress_completed = int(snapshot["completed"])
    first_pending_index = resume_tracker.first_pending_index()
    restored_percent = (int(snapshot["completed"]) / max(1, total_files)) * 100.0
    profile["resume_checkpoint"] = {
        "version": RESUME_JOURNAL_VERSION,
        "total": total_files,
        "completed": int(snapshot["completed"]),
        "remaining": int(snapshot["remaining"]),
        "source_remaining": int(snapshot["source_remaining"]),
        "moved_count": int(snapshot["moved_out"]),
        "checked_but_not_moved": int(snapshot["completed_in_source"]),
        "progress_percent": round(restored_percent, 6),
        "next_index": min(total_files, first_pending_index) + (1 if first_pending_index < total_files else 0),
        "last_saved_at": now_iso(),
        "manifest_file": resume_tracker.manifest_path.name,
        "journal_file": resume_tracker.journal_path.name,
    }
    persist_state(force=True)
    append_log(
        f"ზუსტი გაგრძელება აღდგა | {state.stats.resumed}/{total_files} ({restored_percent:.1f}%) | "
        f"უკვე გადატანილი: {snapshot['moved_out']} | Source-ში დარჩენილი: {snapshot['source_remaining']} | "
        f"დასამუშავებელი დარჩა: {snapshot['remaining']} | შემდეგი პოზიცია: {min(total_files, first_pending_index) + (1 if first_pending_index < total_files else 0)}",
        "success" if state.stats.resumed else "info",
    )
    progress_bar["maximum"] = total_files if total_files else 1
    progress_bar["value"] = state.stats.resumed
    progress_label.config(
        text=f"გაგრძელება: {state.stats.resumed}/{total_files} ({restored_percent:.1f}%) | "
             f"უკვე გადატანილი: {snapshot['moved_out']} | Source-ში დარჩენილი: {snapshot['source_remaining']} | "
             f"დარჩა დასამუშავებელი: {snapshot['remaining']} | შემდეგი ფოტო: {min(total_files, first_pending_index) + (1 if first_pending_index < total_files else 0)}"
    )
    update_scan_summary(total_files, state.stats.resumed)
    if total_files == 0:
        db_finish_scan(scan_id, "empty", _scan_totals(0, 0))
        export_checked_nonmatches(source_root, "scan")
        resume_tracker.close()
        messagebox.showinfo("ინფორმაცია", "Source საქაღალდეში ფოტოები ვერ მოიძებნა")
        return
    if not files_to_scan:
        db_finish_scan(scan_id, "already_completed", _scan_totals(total_files, total_files))
        list_path = export_checked_nonmatches(source_root, "scan")
        append_log(f"უდამთხვევო შემოწმებული ფოტოების სია: {list_path}", "success")
        progress_label.config(text=f"ყველა ფაილი უკვე შემოწმებულია ({total_files})")
        resume_tracker.close()
        if clear_completed_json_state(state.scan_state_path):
            state.scan_state_path = None
            state.scan_state = {}
            state.current_profile = None
        notify_scan_completed(total_files, total_files)
        messagebox.showinfo("ინფორმაცია", "ყველა უცვლელი ფაილი სწრაფად გამოტოვებულია. შეცვლილი ფოტო თავიდან დამუშავდება.")
        return

    pbar = tqdm(total=total_files, initial=state.stats.resumed, desc="სკანირება", dynamic_ncols=True)
    started_at = time.time()
    state.scan_started_at = started_at
    state.scan_running = True
    start_btn.config(state=DISABLED)
    stop_btn.config(state=NORMAL)
    pause_btn.config(state=NORMAL, text="⏸  პაუზა")
    append_log(
        f"სკანირება დაიწყო | სულ {total_files} | ახალი {new_photo_count} | "
        f"შეცვლილი {changed_photo_count} | ხელახლა დასამუშავებელი {reprocess_photo_count} | "
        f"გამოტოვებული {state.stats.resumed} | შეწყვეტილი {interrupted_count} | "
        f"worker {worker_count}", "info",
    )
    for path in files_to_scan:
        state.file_q.put(path)
    threads: List[threading.Thread] = []
    for _ in range(worker_count):
        thread = threading.Thread(
            target=worker,
            args=(state.current_threshold, pbar, total_files, started_at, progress_bar, progress_label),
            daemon=True,
        )
        thread.start()
        threads.append(thread)
    state.scan_threads = threads

    def finish_on_main_thread(done_count: int, cancelled: bool) -> None:
        apply_scan_progress(pbar, total_files, started_at, progress_bar, progress_label)
        flush_db_writes()
        totals = _scan_totals(total_files, done_count)
        if state.scan_state is not None:
            state.scan_state["stats"] = totals
            state.scan_state["last_scan_status"] = "cancelled" if cancelled else "completed"
            state.scan_state["last_scan_finished_at"] = now_iso()
        persist_state(force=True)
        tracker = getattr(state, "resume_tracker", None)
        if isinstance(tracker, ExactResumeTracker):
            tracker.close()
        completed_fully = not cancelled and total_files > 0 and done_count >= total_files
        if completed_fully and clear_completed_json_state(state.scan_state_path):
            # Prevent the exit hook from recreating the JSON we just cleared.
            state.scan_state_path = None
            state.scan_state = {}
            state.current_profile = None
        db_finish_scan(scan_id, "cancelled" if cancelled else "completed", totals)
        list_path = export_checked_nonmatches(source_root, "scan")
        append_log(f"უდამთხვევო შემოწმებული ფოტოების სია განახლდა: {list_path}", "success")
        pbar.close()
        state.scan_running = False
        state.scan_threads = []
        state.pause_requested.clear()
        start_btn.config(state=NORMAL)
        stop_btn.config(state=DISABLED)
        pause_btn.config(state=DISABLED, text="⏸  პაუზა")
        progress_bar["value"] = done_count
        progress_label.config(text=("უსაფრთხოდ გაჩერდა" if cancelled else "დასრულდა") + f": {done_count}/{total_files}")
        if completed_fully:
            notify_scan_completed(done_count, total_files)
        update_scan_summary(total_files, done_count)
        append_log(
            f"{'გაჩერდა' if cancelled else 'დასრულდა'} | match={len(state.matched)} | "
            f"review={state.review_count} | duplicate={state.stats.duplicates} | error={state.stats.errors}",
            "warning" if cancelled else "success",
        )
        messagebox.showinfo(
            "გაჩერებულია" if cancelled else "დასრულდა",
            f"დამუშავებული: {done_count}/{total_files}\n"
            f"დამთხვევა: {len(state.matched)}\nხელით შესამოწმებელი: {state.review_count}\n"
            f"უდამთხვევო: {len(state.nonmatched)}\nდუბლიკატი: {state.stats.duplicates}\n"
            f"შეცდომა: {state.stats.errors}",
        )
        if LIVE_WATCH_ALWAYS_ENABLED and not state.close_after_stop:
            live_watch_var.set(True)
            root.after(200, start_live_watch)
        if state.close_after_stop:
            flush_state_on_exit()
            scan_db_manager.close_persistent()
            router_db_manager.close_persistent()
            root.destroy()

    def wait_completion() -> None:
        state.file_q.join()
        for _ in threads:
            state.file_q.put(None)
        for thread in threads:
            thread.join()
        post_ui(lambda: finish_on_main_thread(int(state.progress_completed), state.stop_requested.is_set()))

    threading.Thread(target=wait_completion, daemon=True).start()
    save_app_settings()


def _scan_source_and_mode(scan_id: str) -> Tuple[str, str]:
    with state.db_lock:
        row = scan_cur.execute("SELECT source_folder,mode FROM scans WHERE id=?", (scan_id,)).fetchone()
    return (str(row[0]), str(row[1])) if row else ("", "scan")


def _sync_persisted_state(
    scan_id: str,
    mode: str,
    relative_path: str,
    status: str,
    source_path: str = "",
    destinations: Optional[List[str]] = None,
    fingerprint: Optional[Dict[str, Any]] = None,
    run_mode_value: str = "real",
    matching_signature_value: str = "",
) -> None:
    source_root, stored_mode = _scan_source_and_mode(scan_id)
    actual_mode = mode or stored_mode
    if not source_root:
        return
    rel = Path(relative_path)
    if actual_mode == "router":
        path = get_router_state_path(Path(source_root))
        data = load_json(path)
        files = data.setdefault("files", {})
        existing = files.get(rel.as_posix(), {}) if isinstance(files.get(rel.as_posix()), dict) else {}
        files[rel.as_posix()] = {
            **existing, "relative_path": rel.as_posix(), "status": status,
            "checked_at": now_iso(), "source_path": source_path,
            "destination_paths": destinations or [], "fingerprint": fingerprint or existing.get("fingerprint", {}),
            "run_mode": run_mode_value,
            "matching_signature": matching_signature_value or existing.get("matching_signature", ""),
        }
        data["updated_at"] = now_iso()
        save_json_atomic(path, data)
    else:
        path = get_state_path(Path(source_root))
        data = migrate_legacy_state(load_json(path), Path(source_root))
        folder = folder_key_from_rel(rel)
        bucket = data.setdefault("folders", {}).setdefault(folder, {})
        existing = bucket.get(rel.name, {}) if isinstance(bucket.get(rel.name), dict) else {}
        bucket[rel.name] = {
            **existing, "file_name": rel.name, "relative_path": rel.as_posix(), "status": status,
            "checked_at": now_iso(), "source_path": source_path,
            "destination_paths": destinations or [], "fingerprint": fingerprint or existing.get("fingerprint", {}),
            "run_mode": run_mode_value,
            "matching_signature": matching_signature_value or existing.get("matching_signature", ""),
        }
        data["updated_at"] = now_iso()
        save_json_atomic(path, data)
        with state.state_lock:
            if state.scan_state_path and state.scan_state_path.resolve() == path.resolve():
                state.scan_state = data
                state.current_profile = data
    if status in {"nonmatched", "unmatched"} and isinstance(fingerprint, dict) and fingerprint:
        recognition_sig = _recognition_signature_for_scan(scan_id, actual_mode)
        db_upsert_checked_nonmatch(
            source_root, rel, actual_mode, recognition_sig, fingerprint, status,
            "", 0.0,
        )


def _recalculate_scan_totals(scan_id: str) -> Dict[str, Any]:
    with state.db_lock:
        rows = scan_cur.execute(
            "SELECT relative_path,status FROM file_results WHERE scan_id=? ORDER BY id", (scan_id,)
        ).fetchall()
    latest: Dict[str, str] = {}
    for relative_path, status in rows:
        latest[str(relative_path)] = str(status)
    totals: Dict[str, Any] = {"processed": len(latest), "total": len(latest)}
    for status in latest.values():
        totals[status] = int(totals.get(status, 0)) + 1
    with state.db_lock:
        scan_cur.execute(
            "UPDATE scans SET totals_json=? WHERE id=?",
            (json.dumps(totals, ensure_ascii=False), scan_id),
        )
        _db_commit_locked(force=True)
    return totals




def rollback_operations(scan_id: Optional[str] = None, only_last: bool = False) -> None:
    if state.scan_running:
        messagebox.showwarning("მიმდინარეობს", "Rollback-მდე სკანირება გააჩერე")
        return
    stop_live_watch(wait=True)
    with state.db_lock:
        if not scan_id:
            row = scan_cur.execute("SELECT scan_id FROM operations WHERE undone=0 ORDER BY id DESC LIMIT 1").fetchone()
            scan_id = str(row[0]) if row else ""
        limit = " LIMIT 1" if only_last else ""
        rows = scan_cur.execute(
            "SELECT id,kind,source_path,destination_path,file_sha256,mode,logical_source_path,relative_path "
            f"FROM operations WHERE scan_id=? AND undone=0 ORDER BY id DESC{limit}",
            (scan_id,),
        ).fetchall()
    if not rows:
        messagebox.showinfo("Undo", "დასაბრუნებელი ოპერაცია ვერ მოიძებნა")
        return
    if not messagebox.askyesno(
        "Undo / Rollback",
        f"დავაბრუნო {'ბოლო ოპერაცია' if only_last else str(len(rows)) + ' ოპერაცია'}?\nScan ID: {scan_id}",
    ):
        return
    source_root, scan_mode = _scan_source_and_mode(str(scan_id))
    restored = skipped = failed = 0
    affected: Dict[str, Tuple[str, Dict[str, Any]]] = {}
    for op_id, kind, source_path, destination_path, expected_sha, mode, logical_source, relative_path in rows:
        dst = Path(destination_path)
        try:
            if kind == "copy":
                if not _operation_unchanged(dst, str(expected_sha or "")):
                    skipped += 1
                    continue
                dst.unlink()
                restored += 1
                restored_path = Path(logical_source or source_path)
            else:
                if not _operation_unchanged(dst, str(expected_sha or "")):
                    skipped += 1
                    continue
                target = Path(logical_source or source_path)
                restore_target = target if not target.exists() else unique_destination_path(target.with_name(target.stem + "_restored" + target.suffix))
                restore_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(restore_target))
                restored += 1
                restored_path = restore_target
            with state.db_lock:
                scan_cur.execute("UPDATE operations SET undone=1,undone_at=? WHERE id=?", (now_iso(), int(op_id)))
                _db_commit_locked(force=True)
            rel_value = str(relative_path or "")
            if not rel_value and source_root:
                try:
                    rel_value = restored_path.resolve().relative_to(Path(source_root).resolve()).as_posix()
                except Exception:
                    rel_value = Path(logical_source or source_path).name
            fp = file_fingerprint(restored_path, include_sha=True) if restored_path.exists() else {}
            affected[rel_value] = (str(restored_path), fp)
        except Exception as exc:
            failed += 1
            logger.error(f"Rollback failed: {exc}")
    for rel_value, (restored_path, fp) in affected.items():
        with state.db_lock:
            remaining = scan_cur.execute(
                "SELECT kind,destination_path FROM operations WHERE scan_id=? AND relative_path=? AND undone=0 ORDER BY id",
                (str(scan_id), rel_value),
            ).fetchall()
            previous = scan_cur.execute(
                "SELECT status FROM file_results WHERE scan_id=? AND relative_path=? "
                "AND status NOT IN ('rolled_back','rollback_partial') ORDER BY id DESC LIMIT 1",
                (str(scan_id), rel_value),
            ).fetchone()
        if remaining:
            active_destinations = [str(path) for _kind, path in remaining if Path(path).exists()]
            previous_status = str(previous[0]) if previous else "matched"
            state_status = "matched" if previous_status in {"review_approved", "matched"} else previous_status
            sync_fp = fp
            if not sync_fp and active_destinations:
                try:
                    sync_fp = file_fingerprint(active_destinations[0], include_sha=True)
                except Exception:
                    sync_fp = {}
            _sync_persisted_state(
                str(scan_id), str(scan_mode), rel_value, state_status, restored_path,
                active_destinations, sync_fp, "real", _real_matching_signature_for_scan(str(scan_id), str(scan_mode)),
            )
            db_record_file_result(
                str(scan_id), str(scan_mode), rel_value, restored_path, "rollback_partial",
                active_destinations, fingerprint=sync_fp,
            )
        else:
            _sync_persisted_state(
                str(scan_id), str(scan_mode), rel_value, "rolled_back", restored_path, [], fp, "real"
            )
            db_record_file_result(
                str(scan_id), str(scan_mode), rel_value, restored_path, "rolled_back", [], fingerprint=fp
            )
            with state.db_lock:
                scan_cur.execute(
                    "UPDATE review_queue SET status='cancelled',decided_at=? "
                    "WHERE scan_id=? AND relative_path=? AND status='pending'",
                    (now_iso(), str(scan_id), rel_value),
                )
                _db_commit_locked(force=True)
    _recalculate_scan_totals(str(scan_id))
    messagebox.showinfo(
        "Rollback შედეგი",
        f"დაბრუნებული: {restored}\nგამოტოვებული (შეცვლილი/აკლია): {skipped}\nშეცდომა: {failed}",
    )


def _resolve_review_reference(scan_id: str, mode: str, top_json: str) -> str:
    try:
        top = json.loads(top_json or "[]")
        first = top[0] if isinstance(top, list) and top else {}
        if mode in {"scan", "live"}:
            reference = str(first.get("reference", ""))
            if reference:
                return reference
            with state.db_lock:
                row = scan_cur.execute("SELECT refs_json FROM scans WHERE id=?", (scan_id,)).fetchone()
            refs = json.loads(row[0] or "[]") if row else []
            if refs and isinstance(refs[0], dict):
                files = refs[int(first.get("index", 0))].get("files", [])
                return str(files[0]) if files else ""
            return str(refs[0]) if refs else ""
        slot_index = int(first.get("slot", 0))
        with state.db_lock:
            row = scan_cur.execute(
                "SELECT refs_json FROM identities WHERE scan_id=? AND identity_index=? ORDER BY id DESC LIMIT 1",
                (scan_id, slot_index),
            ).fetchone()
        refs = json.loads(row[0] or "[]") if row else []
        return str(refs[0]) if refs else ""
    except Exception:
        return ""


def _recognition_signature_for_scan(scan_id: str, mode: str) -> str:
    try:
        with state.db_lock:
            row = scan_cur.execute("SELECT params_json FROM scans WHERE id=?", (scan_id,)).fetchone()
        params = json.loads(row[0] or "{}") if row else {}
        existing = str(params.get("recognition_signature", ""))
        if existing:
            return existing
        threshold = float(params.get("threshold", config.threshold_default / 100.0))
        if mode == "router":
            return _recognition_signature(
                threshold, router_signature_value=str(params.get("router_signature", ""))
            )
        return _recognition_signature(
            threshold, reference_signature=str(params.get("reference_signature", ""))
        )
    except Exception:
        return ""


def _real_matching_signature_for_scan(scan_id: str, mode: str) -> str:
    try:
        with state.db_lock:
            row = scan_cur.execute("SELECT params_json FROM scans WHERE id=?", (scan_id,)).fetchone()
        params = json.loads(row[0] or "{}") if row else {}
        threshold = float(params.get("threshold", config.threshold_default / 100.0))
        if mode == "router":
            return _matching_signature(
                threshold, router_signature_value=str(params.get("router_signature", ""))
            )
        return _matching_signature(
            threshold, reference_signature=str(params.get("reference_signature", getattr(state, "current_ref_signature", "")))
        )
    except Exception:
        return ""


def _decide_review(row: Tuple[Any, ...], approved: bool) -> None:
    review_id, scan_id, mode, source_path, relative_path, score, second, top_json, rec_json = row
    source = Path(source_path)
    fingerprint: Dict[str, Any] = {}
    destinations_done: List[str] = []
    try:
        if source.exists():
            fingerprint = file_fingerprint(source, include_sha=False)
            ensure_fingerprint_sha(fingerprint, source)
        if approved:
            recommendation = json.loads(rec_json or "{}")
            destinations = [Path(value) for value in recommendation.get("destinations", [])]
            if not source.exists():
                raise FileNotFoundError(f"Source აღარ არსებობს: {source}")
            if not destinations:
                raise ValueError("Destination რეკომენდაციაში არ არის")
            _set_operation_context(str(scan_id), str(mode), source, str(relative_path))
            try:
                primary = move_file_safely(source, destinations[0], fingerprint)
                destinations_done.append(str(primary))
                for extra in destinations[1:]:
                    destinations_done.append(str(copy_file_safely(primary, extra, fingerprint)))
            finally:
                _clear_operation_context()
            queue_status = "approved"
            state_status = "matched"
            db_status = "review_approved"
        else:
            queue_status = "rejected"
            state_status = "unmatched" if str(mode) == "router" else "nonmatched"
            db_status = "review_rejected"
        db_record_file_result(
            str(scan_id), str(mode), str(relative_path), str(source_path), db_status,
            destinations_done, float(score), float(second), json.loads(top_json or "[]"), fingerprint,
        )
        with state.db_lock:
            scan_cur.execute(
                "UPDATE review_queue SET status=?,decided_at=? WHERE id=?",
                (queue_status, now_iso(), int(review_id)),
            )
            _db_commit_locked(force=True)
        _sync_persisted_state(
            str(scan_id), str(mode), str(relative_path), state_status, str(source_path),
            destinations_done, fingerprint, "real",
            _real_matching_signature_for_scan(str(scan_id), str(mode)),
        )
        _recalculate_scan_totals(str(scan_id))
    finally:
        _clear_operation_context()


def open_review_queue() -> None:
    with state.db_lock:
        rows = list(scan_cur.execute(
            """SELECT id,scan_id,mode,source_path,relative_path,score,second_score,
                      top_matches_json,recommended_json
               FROM review_queue WHERE status='pending' ORDER BY id"""
        ).fetchall())
    if not rows:
        messagebox.showinfo("Review", "Pending ფოტო არ არის")
        return
    win = Toplevel(root)
    win.title("ხელით შესამოწმებელი ფოტოები")
    win.configure(bg="#0B1120")
    register_window_theme(win, "review_queue")
    review_width, review_height = configure_responsive_geometry(
        win, 1180, 820, minimum_width=640, minimum_height=500,
        width_ratio=0.96, height_ratio=0.92,
    )
    review_page, _review_canvas = create_scrollable_page(
        win, minimum_content_width=760, horizontal=True
    )
    index_var = IntVar(value=0)
    images: Dict[str, Any] = {}
    title = Label(review_page, font=("Segoe UI", 13, "bold"), fg="#E8EDF5", bg="#0B1120")
    title.pack(pady=(12, 5))
    details = Label(review_page, font=("Segoe UI", 9), fg="#7B93B8", bg="#0B1120", wraplength=1060, justify=LEFT)
    details.pack(padx=15)
    panel = Frame(review_page, bg="#0B1120")
    panel.pack(fill=BOTH, expand=True, padx=15, pady=10)
    source_label = Label(panel, text="Source", bg="#131C2E", fg="#7B93B8")
    source_label.pack(side=LEFT, fill=BOTH, expand=True, padx=5)
    ref_label = Label(panel, text="Reference", bg="#131C2E", fg="#7B93B8")
    ref_label.pack(side=LEFT, fill=BOTH, expand=True, padx=5)

    def make_preview(path_value: str) -> Any:
        image = load_image_bgr(path_value)
        if image is None:
            raise ValueError("ფოტო ვერ გაიხსნა")
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        available_width = max(260, int(win.winfo_width() or review_width))
        available_height = max(260, int(win.winfo_height() or review_height))
        preview_side = max(220, min(540, (available_width - 110) // 2, available_height - 240))
        pil.thumbnail((preview_side, preview_side))
        if ImageTk is None:
            raise RuntimeError("Pillow ImageTk არ არის ხელმისაწვდომი")
        return ImageTk.PhotoImage(pil)

    def render() -> None:
        row = rows[index_var.get()]
        review_id, scan_id, mode, source_path, relative_path, score, second, top_json, rec_json = row
        title.config(text=f"{index_var.get()+1}/{len(rows)} — {relative_path}")
        details.config(
            text=f"რეჟიმი: {mode} | საუკეთესო: {float(score):.4f} | მეორე: {float(second):.4f} | "
                 f"სხვაობა: {float(score)-float(second):.4f}\nTop-3: {top_json}"
        )
        try:
            images["source"] = make_preview(str(source_path))
            source_label.config(image=images["source"], text="")
        except Exception as exc:
            source_label.config(image="", text=str(exc))
        try:
            reference = _resolve_review_reference(str(scan_id), str(mode), str(top_json))
            images["ref"] = make_preview(reference)
            ref_label.config(image=images["ref"], text="")
        except Exception as exc:
            ref_label.config(image="", text=str(exc))

    def decide(approved: bool) -> None:
        try:
            _decide_review(rows[index_var.get()], approved)
            rows.pop(index_var.get())
            if not rows:
                win.destroy()
                messagebox.showinfo("Review", "ყველა pending ფოტო დამუშავებულია")
                return
            index_var.set(min(index_var.get(), len(rows) - 1))
            render()
        except Exception as exc:
            messagebox.showerror("Review შეცდომა", str(exc))

    buttons = Frame(review_page, bg="#0B1120")
    buttons.pack(fill=X, padx=20, pady=12)
    Button(buttons, text="◀ წინა", command=lambda: (index_var.set(max(0, index_var.get()-1)), render()),
           bg="#1A2540", fg="#7B93B8", relief=FLAT, padx=16, pady=10).pack(side=LEFT)
    Button(buttons, text="✓ დამთხვევაა — გადაიტანე", command=lambda: decide(True),
           bg="#2E7D5B", fg="white", relief=FLAT, padx=18, pady=10).pack(side=LEFT, padx=8)
    Button(buttons, text="✗ არ ემთხვევა", command=lambda: decide(False),
           bg="#8A3B46", fg="white", relief=FLAT, padx=18, pady=10).pack(side=LEFT)
    Button(buttons, text="შემდეგი ▶", command=lambda: (index_var.set(min(len(rows)-1, index_var.get()+1)), render()),
           bg="#1A2540", fg="#7B93B8", relief=FLAT, padx=16, pady=10).pack(side=RIGHT)
    render()


def start_live_watch() -> bool:
    with LIVE_WATCH_LOCK:
        thread = getattr(state, "live_watch_thread", None)
        if thread is not None and thread.is_alive():
            return True
        if not state.ref_files or not state.src_folder or not state.out_folder:
            return False
        try:
            model_signature = ensure_face_engine(
                _global_tk_value("model_profile_var")
            )
            current_reference_signature = ref_signature(state.ref_files)
            if (
                getattr(state, "reference_model_signature", "") != model_signature
                or getattr(state, "reference_content_signature", "") != current_reference_signature
            ):
                _reload_main_identities_for_model()
        except Exception as exc:
            logger.error(f"Live Watch reference/model preparation failed: {exc}")
            return False
        state.live_watch_stop.clear()
        source_root = Path(state.src_folder).resolve()
        output_root = Path(state.out_folder).resolve()
        if not source_root.is_dir():
            logger.error("Live Watch source folder no longer exists")
            return False
        try:
            output_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error(f"Live Watch output folder unavailable: {exc}")
            return False
        threshold = float(getattr(state, "current_threshold", 0.0) or slider.get() / 100.0)
        live_id = f"live-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        state.live_session_id = live_id
        state.active_scan_id = live_id
        state.current_threshold = threshold
        state.current_ref_signature = ref_signature(state.ref_files)
        state.current_matching_signature = _matching_signature(threshold, reference_signature=state.current_ref_signature)
        state.current_identity_signature = _identity_signature(reference_signature=state.current_ref_signature)
        state.current_recognition_signature = _recognition_signature(
            threshold, reference_signature=state.current_ref_signature
        )
        profile = prepare_scan_state(source_root, output_root, threshold, 1)
        profile.pop("dry_run", None)
        profile.update({
            "active_scan_id": live_id, "run_mode": "real",
            "matching_signature": state.current_matching_signature,
            "identity_signature": state.current_identity_signature,
            "recognition_signature": state.current_recognition_signature,
        })
        persist_state(force=True)
        state.duplicate_index = DuplicateIndex()
        _seed_duplicate_index(state.duplicate_index, profile)
        db_start_scan(
            live_id, "live", str(source_root), [str(output_root)], state.ref_files,
            {"threshold": threshold, "matching_signature": state.current_matching_signature,
             "recognition_signature": state.current_recognition_signature,
             "reference_signature": state.current_ref_signature},
        )
        _seed_checked_nonmatches_from_main(profile, source_root, state.current_recognition_signature)
        state.checked_nonmatch_index = CheckedNonmatchIndex(
            source_root, "scan", state.current_recognition_signature
        )
        known: Dict[str, Tuple[int, int]] = {}
        expected_mode = "real"
        reference_paths = {str(Path(path).resolve()) for path in state.ref_files}
        announced_signatures: Dict[str, Tuple[int, int]] = {}
        live_initial_new = 0
        live_initial_changed = 0
        live_initial_reprocess = 0
        # Seed only files that are genuinely complete for the current references,
        # model and threshold. Files added before Live Watch was
        # enabled (or changed while it was off) remain unknown and are processed.
        for path in iter_image_files(source_root):
            try:
                resolved = str(path.resolve())
                if resolved in reference_paths:
                    continue
                rel = path.relative_to(source_root)
                entry = _profile_entry(profile, rel)
                stat = path.stat()
                signature = (int(stat.st_size), int(stat.st_mtime_ns))
                announced_signatures[resolved] = signature
                if _entry_is_current(entry, path, state.current_identity_signature, expected_mode):
                    known[resolved] = signature
                    continue
                known_nonmatch, _reason, _ledger = state.checked_nonmatch_index.lookup(path, rel)
                if known_nonmatch:
                    known[resolved] = signature
                    continue
                pending_kind = _classify_pending_photo(entry, path)
                if pending_kind == "new":
                    live_initial_new += 1
                elif pending_kind == "changed":
                    live_initial_changed += 1
                else:
                    live_initial_reprocess += 1
            except (OSError, ValueError):
                pass
        pending: Dict[str, Dict[str, Any]] = {}
        if live_initial_new or live_initial_changed or live_initial_reprocess:
            append_log(
                _photo_change_log_message(
                    "Live Watch საწყისი შემოწმება",
                    live_initial_new,
                    live_initial_changed,
                    live_initial_reprocess,
                    len(known),
                ),
                "success" if live_initial_new > 0 else "info",
            )

        def watcher() -> None:
            totals = {
                "processed": 0,
                "errors": 0,
                "new_detected": live_initial_new,
                "changed_detected": live_initial_changed,
                "reprocess_detected": live_initial_reprocess,
            }
            post_ui(lambda: append_log("Live Watch ჩაირთო", "success"))
            try:
                while not state.live_watch_stop.wait(float(config.live_watch_interval)):
                    if state.scan_running or state.pause_requested.is_set():
                        continue
                    seen_now: Set[str] = set()
                    discovered_new = 0
                    discovered_changed = 0
                    discovered_reprocess = 0
                    for path in iter_image_files(source_root):
                        if state.live_watch_stop.is_set():
                            break
                        resolved = str(path.resolve())
                        if resolved in reference_paths:
                            continue
                        seen_now.add(resolved)
                        try:
                            stat = path.stat()
                            signature = (int(stat.st_size), int(stat.st_mtime_ns))
                        except OSError:
                            continue

                        # Announce each genuinely new path once. A completed file that
                        # changes later is announced once again as "changed". Size
                        # fluctuations while a file is still being copied are not
                        # counted repeatedly.
                        should_announce = resolved not in announced_signatures
                        if resolved in known and known.get(resolved) != signature:
                            should_announce = announced_signatures.get(resolved) != signature
                        if should_announce:
                            try:
                                rel = path.relative_to(source_root)
                                entry = _profile_entry(profile, rel)
                                pending_kind = _classify_pending_photo(entry, path)
                            except Exception:
                                pending_kind = "new" if resolved not in announced_signatures else "changed"
                            if pending_kind == "new":
                                discovered_new += 1
                            elif pending_kind == "changed":
                                discovered_changed += 1
                            else:
                                discovered_reprocess += 1
                            announced_signatures[resolved] = signature

                        if known.get(resolved) == signature and resolved not in pending:
                            continue
                        try:
                            rel_for_skip = path.relative_to(source_root)
                            known_nonmatch, _skip_reason, _ledger = state.checked_nonmatch_index.lookup(path, rel_for_skip)
                            if known_nonmatch:
                                known[resolved] = signature
                                pending.pop(resolved, None)
                                continue
                        except Exception:
                            pass
                        item = pending.get(resolved)
                        if item and float(item.get("next_retry", 0.0) or 0.0) > time.monotonic():
                            continue
                        if item and item.get("signature") == signature:
                            item["stable"] = int(item.get("stable", 0)) + 1
                        else:
                            pending[resolved] = {"signature": signature, "stable": 0, "attempts": 0, "next_retry": 0.0}
                            continue
                        if int(pending[resolved]["stable"]) < 2:
                            continue
                        result = process_main_file(path, threshold, live_mode=True)
                        if result.get("status") == "error":
                            attempts = int(pending[resolved].get("attempts", 0)) + 1
                            pending[resolved]["attempts"] = attempts
                            pending[resolved]["stable"] = 0
                            pending[resolved]["next_retry"] = time.monotonic() + min(60.0, float(2 ** min(attempts, 5)))
                            totals["errors"] += 1
                            continue
                        known[resolved] = signature
                        announced_signatures[resolved] = signature
                        pending.pop(resolved, None)
                        totals["processed"] += 1
                        post_ui(lambda r=result, name=path.name: append_log(f"Live: {name} -> {r.get('status')}", "info"))
                    if discovered_new or discovered_changed or discovered_reprocess:
                        totals["new_detected"] += discovered_new
                        totals["changed_detected"] += discovered_changed
                        totals["reprocess_detected"] += discovered_reprocess
                        message = _photo_change_log_message(
                            "Live Watch ცვლილება",
                            discovered_new,
                            discovered_changed,
                            discovered_reprocess,
                        )
                        post_ui(
                            lambda msg=message, has_new=discovered_new > 0:
                            append_log(msg, "success" if has_new else "info")
                        )
                    for missing in list(pending):
                        if missing not in seen_now and not Path(missing).exists():
                            pending.pop(missing, None)
                    for missing in list(announced_signatures):
                        if missing not in seen_now and not Path(missing).exists():
                            announced_signatures.pop(missing, None)
                            known.pop(missing, None)
            finally:
                db_finish_scan(live_id, "stopped", totals)
                list_path = export_checked_nonmatches(source_root, "scan")
                post_ui(lambda p=list_path: append_log(f"უდამთხვევო ფოტოების სია განახლდა: {p}", "success"))
                flush_db_writes()
                with LIVE_WATCH_LOCK:
                    if getattr(state, "live_session_id", "") == live_id:
                        state.live_watch_thread = None
                        state.live_session_id = ""
                        state.active_scan_id = ""
                post_ui(lambda: append_log("Live Watch გამორთულია", "warning"))

        thread = threading.Thread(target=watcher, daemon=True, name=f"LiveWatch-{live_id}")
        state.live_watch_thread = thread
        thread.start()
        return True


def stop_live_watch(wait: bool = True) -> None:
    state.live_watch_stop.set()
    with LIVE_WATCH_LOCK:
        thread = getattr(state, "live_watch_thread", None)
    if wait and thread is not None and thread.is_alive() and thread is not threading.current_thread():
        thread.join(timeout=max(3.0, float(config.live_watch_interval) * 3.0))
    with LIVE_WATCH_LOCK:
        if thread is not None and not thread.is_alive():
            state.live_watch_thread = None


def toggle_live_watch() -> None:
    """Compatibility callback: Live Watch is always enabled and hidden in v4.6."""
    live_watch_var.set(True)
    if not state.scan_running:
        start_live_watch()
    save_app_settings()


def ensure_live_watch_started() -> None:
    """Start the hidden always-on watcher once all required inputs are valid."""
    if not LIVE_WATCH_ALWAYS_ENABLED or state.scan_running:
        return
    live_watch_var.set(True)
    if state.ref_files and state.src_folder and state.out_folder:
        start_live_watch()


def restart_hidden_live_watch() -> None:
    """Apply changed model/settings without blocking the Tk main thread."""
    if state.scan_running:
        return
    def restart() -> None:
        stop_live_watch(wait=True)
        post_ui(ensure_live_watch_started)
    threading.Thread(target=restart, daemon=True).start()


def _restore_saved_ui_state() -> None:
    try:
        state.src_folder = str(APP_SETTINGS.get("source_folder", ""))
        state.out_folder = str(APP_SETTINGS.get("output_folder", ""))
        lbl_src.config(text=f"წყარო: {state.src_folder or 'არ არის არჩეული'}")
        lbl_out.config(text=f"შედეგი: {state.out_folder or 'არ არის არჩეული'}")
        slider.set(_safe_int(APP_SETTINGS.get("threshold"), config.threshold_default, config.threshold_min, config.threshold_max))
        worker_var.set(str(_safe_int(APP_SETTINGS.get("worker_count"), config.worker_count, config.worker_min, config.worker_max)))
        performance_profile_var.set(str(APP_SETTINGS.get("performance_profile", "ავტომატური")))
        model_profile_var.set(str(APP_SETTINGS.get("model_profile", "მაქსიმალური სიზუსტე")))
        live_watch_var.set(True)
        duplicate_mode_var.set(str(APP_SETTINGS.get("duplicate_mode", "მხოლოდ ანგარიშში")))
        duplicate_distance_var.set(str(_safe_int(APP_SETTINGS.get("duplicate_distance"), config.duplicate_phash_distance, 0, 16)))
        ambiguity_margin_var.set(str(_safe_float(APP_SETTINGS.get("ambiguity_margin"), config.ambiguity_margin, 0.0, 0.20)))
        review_margin_var.set(str(_safe_float(APP_SETTINGS.get("review_margin"), config.review_margin, 0.01, 0.12)))
        payload = APP_SETTINGS.get("reference_identities", [])
        if not payload:
            payload = [
                {"name": Path(path).stem, "files": [path]}
                for path in APP_SETTINGS.get("reference_files", []) if Path(path).exists()
            ]
        payload = [
            {"name": str(item.get("name", "")), "files": [path for path in item.get("files", []) if Path(path).exists()]}
            for item in payload if isinstance(item, dict)
        ]
        payload = [item for item in payload if item["files"]]
        if payload:
            selected_profile = str(model_profile_var.get())
            def load_refs() -> None:
                try:
                    ensure_face_engine(selected_profile)
                    identities: List[Dict[str, Any]] = []
                    all_embeddings: List[np.ndarray] = []
                    for index, item in enumerate(payload, start=1):
                        embeddings = [get_emb(path) for path in item["files"]]
                        matrix, centroid = _identity_profile(embeddings)
                        identities.append({
                            "index": index, "name": item["name"] or f"ადამიანი #{index}",
                            "files": item["files"], "embeddings": embeddings,
                            "matrix": matrix, "centroid": centroid,
                        })
                        all_embeddings.extend(embeddings)
                    state.main_identities = identities
                    state.ref_files = [path for item in identities for path in item["files"]]
                    state.ref_embs = all_embeddings
                    state.ref_embs_matrix = np.vstack(all_embeddings).astype(np.float32)
                    state.ref_db_value = build_reference_db_value(state.ref_files)
                    state.current_ref_signature = ref_signature(state.ref_files)
                    state.reference_model_signature = _current_model_signature()
                    state.reference_content_signature = ref_signature(state.ref_files)
                    post_ui(lambda: lbl_ref.config(
                        text=f"აღდგენილია {len(state.ref_files)} reference ფოტო | იდენტობა: {len(identities)}"
                    ))
                    post_ui(lambda: root.after(250, ensure_live_watch_started))
                except Exception as exc:
                    logger.warning(f"Saved references restore failed: {exc}")
            threading.Thread(target=load_refs, daemon=True).start()
    except Exception as exc:
        logger.warning(f"UI restore failed: {exc}")


# Thread-safe Tk dispatcher: worker threads never call Tk directly.
UI_CALL_QUEUE: queue.Queue = queue.Queue()

def post_ui(callback: Callable[[], Any]) -> None:
    UI_CALL_QUEUE.put(callback)

def _drain_ui_queue() -> None:
    processed = 0
    while processed < 200:
        try:
            callback = UI_CALL_QUEUE.get_nowait()
        except queue.Empty:
            break
        try:
            callback()
        except Exception as exc:
            logger.warning(f"UI callback failed: {exc}")
        finally:
            UI_CALL_QUEUE.task_done()
        processed += 1
    try:
        root.after(35, _drain_ui_queue)
    except Exception:
        pass

# ----------- GUI --------------
root = Tk()
root.after(35, _drain_ui_queue)
root.title("სახის სკანერი PRO")
root.configure(bg="#0B1120")

# Auto-size: detect screen and set window size accordingly
def _setup_window_geometry() -> None:
    configure_responsive_geometry(root, 720, 940, minimum_width=520, minimum_height=480,
                                  width_ratio=0.96, height_ratio=0.94)

_setup_window_geometry()


def on_app_close() -> None:
    global _APP_EXIT_AFTER_ROUTER
    controller = globals().get("_ACTIVE_ROUTER_CONTROLLER", {})
    try:
        if controller and callable(controller.get("is_running")) and controller["is_running"]():
            if not _APP_EXIT_AFTER_ROUTER:
                if not messagebox.askyesno("გასვლა", "1-20 კაციანი გადანაწილება მიმდინარეობს. უსაფრთხოდ გავაჩერო და გავიდე?"):
                    return
                _APP_EXIT_AFTER_ROUTER = True
                controller["stop"]()
            return
        if controller and callable(controller.get("destroy")):
            controller["destroy"]()
    except Exception as exc:
        logger.warning(f"Router shutdown warning: {exc}")
    stop_live_watch()
    save_app_settings()
    if state.scan_running and not state.stop_requested.is_set():
        if not messagebox.askyesno("გასვლა", "სკანირება მიმდინარეობს. უსაფრთხოდ გავაჩერო და გავიდე?"):
            return
        stop_scan(close_after=True)
        return
    flush_state_on_exit()
    scan_db_manager.close_persistent()
    router_db_manager.close_persistent()
    root.destroy()


root.protocol("WM_DELETE_WINDOW", on_app_close)

# Style
style = Style()
style.theme_use('clam')
style.configure("Custom.Horizontal.TProgressbar",
                troughcolor='#1A2540', bordercolor='#4D7CFF',
                background='#4D7CFF', lightcolor='#4D7CFF', darkcolor='#4D7CFF')


# ---- Scrollable wrapper ----
_outer = Frame(root, bg="#0B1120")
_outer.pack(fill=BOTH, expand=True)

_outer.grid_rowconfigure(0, weight=1)
_outer.grid_columnconfigure(0, weight=1)
_main_canvas = Canvas(_outer, bg="#0B1120", highlightthickness=0)
_main_sb = Scrollbar(_outer, orient=VERTICAL, command=_main_canvas.yview)
_main_xsb = Scrollbar(_outer, orient=HORIZONTAL, command=_main_canvas.xview)
_main_canvas.configure(yscrollcommand=_main_sb.set, xscrollcommand=_main_xsb.set)
_main_canvas.grid(row=0, column=0, sticky="nsew")
_main_sb.grid(row=0, column=1, sticky="ns")
_main_xsb.grid(row=1, column=0, sticky="ew")

R = Frame(_main_canvas, bg="#0B1120")   # R = root_frame, all widgets go here
_scroll_win = _main_canvas.create_window((0, 0), window=R, anchor="nw")

def _reconfigure(event=None):
    _main_canvas.configure(scrollregion=_main_canvas.bbox("all"))

def _fit_width(event=None):
    canvas_width = max(1, _main_canvas.winfo_width())
    requested_width = max(520, R.winfo_reqwidth())
    _main_canvas.itemconfig(_scroll_win, width=max(canvas_width, requested_width))
    _reconfigure()

R.bind("<Configure>", _reconfigure)
_main_canvas.bind("<Configure>", _fit_width)

def _on_mw(event):
    horizontal_scroll = bool(getattr(event, "state", 0) & 0x0001)
    direction = -1 if int(getattr(event, "delta", 0) or 0) > 0 else 1
    try:
        target = root.winfo_containing(event.x_root, event.y_root)
    except Exception:
        target = None
    current = target
    while current is not None and current is not root:
        if current is not _main_canvas and _widget_can_scroll(current, horizontal_scroll):
            try:
                scroll_method = getattr(current, "xview_scroll" if horizontal_scroll else "yview_scroll")
                scroll_method(direction * 3, "units")
                return "break"
            except Exception:
                break
        try:
            current = current.master
        except Exception:
            break
    if horizontal_scroll:
        _main_canvas.xview_scroll(direction * 3, "units")
    else:
        _main_canvas.yview_scroll(direction * 3, "units")
    return "break"

root.bind("<MouseWheel>", _on_mw, add="+")


# ---- Header ----
_hdr = Frame(R, bg="#0B1120")
_hdr.pack(fill=X, pady=15)
Label(_hdr, text="სახის სკანერი PRO", font=("Segoe UI", 22, "bold"),
      fg="#E8EDF5", bg="#0B1120").pack()
Label(_hdr, text="✦  AI-ზე დაფუძნებული სახის ამოცნობის სისტემა  ✦",
      font=("Segoe UI", 10), fg="#7B93B8", bg="#0B1120").pack()

Frame(R, height=1, bg="#1F2D4A").pack(fill=X, padx=20, pady=5)

content_frame = Frame(R, bg="#0B1120")
content_frame.pack(fill=X, padx=20, pady=8)


def create_button(parent: Frame, text: str, command: Callable[[], Any], emoji: str = "") -> Button:
    btn_frame = Frame(parent, bg="#0B1120", bd=0)
    btn_frame.pack(pady=4, fill=X)
    btn = Button(btn_frame, text=f"{emoji} {text}", command=command,
                 font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#4D7CFF",
                 activebackground="#4D7CFF", activeforeground="#0B1120",
                 relief=FLAT, bd=0, padx=20, pady=11, cursor="hand2")
    btn.pack(fill=X)
    btn.bind("<Enter>", lambda e: btn.config(bg="#4D7CFF", fg="#E8EDF5"))
    btn.bind("<Leave>", lambda e: btn.config(bg="#1A2540", fg="#4D7CFF"))
    return btn


# Utility buttons
create_button(content_frame, "ფოტოს ხარისხის შემოწმება", open_quality_checker, "🔬")
create_button(content_frame, "1-20 კაციანი გადანაწილება", open_multi_person_router, "👥")

# ---- JSON ისტორიის ღილაკი ----
def open_json_history_main() -> None:
    """Open main JSON history window directly from the start page"""
    _open_history_window(root, mode="scan")

create_button(content_frame, "ისტორია", open_json_history_main, "🕓")

Frame(R, height=1, bg="#1A2540").pack(fill=X, padx=20, pady=(2, 4))

# Reference Faces
create_button(content_frame, "საცნობარო ფოტოები", pick_refs, "👤")
lbl_ref = Label(content_frame, text="საცნობარო ფოტოები არ არის არჩეული",
                font=("Segoe UI", 9), fg="#7B93B8", bg="#0B1120")
lbl_ref.pack()

# Source Folder
create_button(content_frame, "წყაროს საქაღალდე", pick_src, "📂")
lbl_src = Label(content_frame, text="წყარო: არ არის არჩეული",
                font=("Segoe UI", 9), fg="#7B93B8", bg="#0B1120")
lbl_src.pack()

# Output Folder
create_button(content_frame, "შედეგის საქაღალდე", pick_out, "📁")
lbl_out = Label(content_frame, text="შედეგი: არ არის არჩეული",
                font=("Segoe UI", 9), fg="#7B93B8", bg="#0B1120")
lbl_out.pack()

Frame(R, height=1, bg="#1A2540").pack(fill=X, padx=20, pady=(8, 2))

# ---- Threshold ----
_thr_frame = Frame(R, bg="#0B1120")
_thr_frame.pack(fill=X, padx=20, pady=(6, 2))
Label(_thr_frame, text="⊙  დამთხვევის ზღვარი", font=("Segoe UI", 10, "bold"),
      fg="#E8EDF5", bg="#0B1120").pack()
slider = Scale(_thr_frame, from_=config.threshold_min, to=config.threshold_max,
               orient=HORIZONTAL, length=400, bg="#0B1120", fg="#7B93B8",
               troughcolor="#1A2540", highlightthickness=0,
               activebackground="#4D7CFF", font=("Segoe UI", 9))
slider.set(config.threshold_default)
slider.pack(pady=4)
threshold_hint_label = Label(_thr_frame,
                             text="ფართო ძებნა ნაკლებ ზღვარს იყენებს; მკაცრი რეჟიმი ამცირებს ცრუ დამთხვევას",
                             font=("Segoe UI", 8), fg="#7B93B8", bg="#0B1120", wraplength=560)
threshold_hint_label.pack(pady=(0, 4))
Button(_thr_frame, text="🎯  ზღვრის ავტომატური კალიბრაცია", command=auto_calibrate_threshold,
       font=("Segoe UI", 9, "bold"), bg="#1A2540", fg="#4D7CFF",
       activebackground="#4D7CFF", activeforeground="#0B1120",
       relief=FLAT, bd=0, padx=14, pady=7, cursor="hand2").pack()

# ---- Worker count ----
_wkr_frame = Frame(R, bg="#0B1120")
_wkr_frame.pack(fill=X, padx=20, pady=(4, 2))
Label(_wkr_frame, text="⚙  Worker-ების რაოდენობა", font=("Segoe UI", 10, "bold"),
      fg="#E8EDF5", bg="#0B1120").pack()
Label(_wkr_frame, text="1-20  (ნაგულისხმევი 4 | 5-6 ხშირად უკეთესია)",
      font=("Segoe UI", 8), fg="#7B93B8", bg="#0B1120").pack(pady=(2, 4))

worker_var = StringVar(value=str(config.worker_count))
_wkr_opts = [str(i) for i in range(1, 21)]
worker_menu = OptionMenu(_wkr_frame, worker_var, *_wkr_opts)
worker_menu.config(font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#4D7CFF",
                   activebackground="#4D7CFF", activeforeground="#0B1120",
                   relief=FLAT, bd=0, highlightthickness=0, width=6, cursor="hand2")
worker_menu["menu"].config(font=("Segoe UI", 10), bg="#1A2540", fg="#4D7CFF",
                           activebackground="#4D7CFF", activeforeground="#0B1120", bd=0)
worker_menu.pack(pady=2)

# ---- Accuracy / performance / safety settings ----
_advanced_visible = BooleanVar(value=False)
_adv_frame = Frame(R, bg="#131C2E", highlightbackground="#1F2D4A", highlightthickness=1)

def _toggle_advanced_settings() -> None:
    visible = not bool(_advanced_visible.get())
    _advanced_visible.set(visible)
    if visible:
        _adv_frame.pack(fill=X, padx=20, pady=(4, 4), before=_advanced_separator)
        _advanced_toggle.config(text="⚙  დამატებითი პარამეტრების დამალვა  ▲")
    else:
        _adv_frame.pack_forget()
        _advanced_toggle.config(text="⚙  დამატებითი პარამეტრები  ▼")

_advanced_toggle = Button(
    R, text="⚙  დამატებითი პარამეტრები  ▼", command=_toggle_advanced_settings,
    font=("Segoe UI", 10, "bold"), bg="#1A2540", fg="#4D7CFF",
    activebackground="#4D7CFF", activeforeground="#0B1120",
    relief=FLAT, bd=0, padx=14, pady=8, cursor="hand2",
)
_advanced_toggle.pack(fill=X, padx=20, pady=(8, 2))
_adv_grid = Frame(_adv_frame, bg="#131C2E")
_adv_grid.pack(fill=X, padx=10, pady=(10, 8))

performance_profile_var = StringVar(value="ავტომატური")
model_profile_var = StringVar(value="მაქსიმალური სიზუსტე")
duplicate_mode_var = StringVar(value="მხოლოდ ანგარიშში")
duplicate_distance_var = StringVar(value=str(config.duplicate_phash_distance))
ambiguity_margin_var = StringVar(value=str(config.ambiguity_margin))
review_margin_var = StringVar(value="0.04")
live_watch_var = BooleanVar(value=True)

for col in range(2):
    _adv_grid.grid_columnconfigure(col, weight=1)

Label(_adv_grid, text="რესურსების პროფილი", fg="#7B93B8", bg="#131C2E", font=("Segoe UI", 8)).grid(row=0, column=0, sticky="w", padx=5)
_perf_menu = OptionMenu(_adv_grid, performance_profile_var, "ავტომატური", "ეკონომიური", "დაბალანსებული", "მაქსიმალური")
_perf_menu.config(bg="#1A2540", fg="#4D7CFF", relief=FLAT, highlightthickness=0, width=18)
_perf_menu["menu"].config(bg="#1A2540", fg="#4D7CFF")
_perf_menu.grid(row=1, column=0, sticky="ew", padx=5, pady=(0, 6))

Label(_adv_grid, text="AI მოდელის პროფილი", fg="#7B93B8", bg="#131C2E", font=("Segoe UI", 8)).grid(row=0, column=1, sticky="w", padx=5)
_model_menu = OptionMenu(_adv_grid, model_profile_var, *MODEL_PROFILES.keys())
_model_menu.config(bg="#1A2540", fg="#4D7CFF", relief=FLAT, highlightthickness=0, width=18)
_model_menu["menu"].config(bg="#1A2540", fg="#4D7CFF")
_model_menu.grid(row=1, column=1, sticky="ew", padx=5, pady=(0, 6))

Label(_adv_grid, text="დუბლიკატების მოქმედება", fg="#7B93B8", bg="#131C2E", font=("Segoe UI", 8)).grid(row=2, column=0, sticky="w", padx=5)
_dup_menu = OptionMenu(_adv_grid, duplicate_mode_var, "მხოლოდ ანგარიშში", "ცალკე საქაღალდეში")
_dup_menu.config(bg="#1A2540", fg="#4D7CFF", relief=FLAT, highlightthickness=0, width=18)
_dup_menu["menu"].config(bg="#1A2540", fg="#4D7CFF")
_dup_menu.grid(row=3, column=0, sticky="ew", padx=5, pady=(0, 6))

Label(_adv_grid, text="Review დიაპაზონი (მაგ. 0.04)", fg="#7B93B8", bg="#131C2E", font=("Segoe UI", 8)).grid(row=2, column=1, sticky="w", padx=5)
Entry(_adv_grid, textvariable=review_margin_var, bg="#1A2540", fg="#E8EDF5", insertbackground="#E8EDF5", relief=FLAT).grid(row=3, column=1, sticky="ew", padx=5, pady=(0, 6), ipady=5)

Label(_adv_grid, text="მსგავსი დუბლიკატის pHash (0-16)", fg="#7B93B8", bg="#131C2E", font=("Segoe UI", 8)).grid(row=4, column=0, sticky="w", padx=5)
Entry(_adv_grid, textvariable=duplicate_distance_var, bg="#1A2540", fg="#E8EDF5", insertbackground="#E8EDF5", relief=FLAT).grid(row=5, column=0, sticky="ew", padx=5, pady=(0, 6), ipady=5)
Label(_adv_grid, text="იდენტობებს შორის მინ. სხვაობა", fg="#7B93B8", bg="#131C2E", font=("Segoe UI", 8)).grid(row=4, column=1, sticky="w", padx=5)
Entry(_adv_grid, textvariable=ambiguity_margin_var, bg="#1A2540", fg="#E8EDF5", insertbackground="#E8EDF5", relief=FLAT).grid(row=5, column=1, sticky="ew", padx=5, pady=(0, 6), ipady=5)

for _var in (performance_profile_var, model_profile_var, duplicate_mode_var, duplicate_distance_var, ambiguity_margin_var, review_margin_var):
    try:
        _var.trace_add("write", lambda *_: save_app_settings())
    except Exception:
        pass

_advanced_separator = Frame(R, height=1, bg="#1A2540")
_advanced_separator.pack(fill=X, padx=20, pady=(8, 4))

# ---- Start / Stop ----
_btn_row = Frame(R, bg="#0B1120")
_btn_row.pack(fill=X, padx=20, pady=8)

start_btn = Button(_btn_row, text="▶  სკანირების დაწყება", command=start_scan,
                   font=("Segoe UI", 14, "bold"), bg="#4D7CFF", fg="white",
                   activebackground="#3A68E8", activeforeground="white",
                   relief=FLAT, bd=0, padx=24, pady=14, cursor="hand2")
start_btn.pack(side=LEFT, padx=(0, 8))

stop_btn = Button(_btn_row, text="■  გაჩერება", command=stop_scan,
                  font=("Segoe UI", 14, "bold"), bg="#1A2540", fg="#F5A623",
                  activebackground="#F5A623", activeforeground="#0B1120",
                  relief=FLAT, bd=0, padx=24, pady=14, cursor="hand2", state=DISABLED)
stop_btn.pack(side=LEFT, padx=(0, 8))

pause_btn = Button(_btn_row, text="⏸  პაუზა", command=toggle_pause_scan,
                   font=("Segoe UI", 12, "bold"), bg="#1A2540", fg="#7B93B8",
                   activebackground="#4D7CFF", activeforeground="white",
                   relief=FLAT, bd=0, padx=18, pady=14, cursor="hand2", state=DISABLED)
pause_btn.pack(side=LEFT)


def start_hover(e: Event) -> None:
    if start_btn['state'] != DISABLED: start_btn.config(bg="#3A68E8")
def start_leave(e: Event) -> None:
    if start_btn['state'] != DISABLED: start_btn.config(bg="#4D7CFF")
def stop_hover(e: Event) -> None:
    if stop_btn['state'] != DISABLED: stop_btn.config(bg="#F5A623", fg="#0B1120")
def stop_leave(e: Event) -> None:
    if stop_btn['state'] != DISABLED: stop_btn.config(bg="#1A2540", fg="#F5A623")

start_btn.bind("<Enter>", start_hover)
start_btn.bind("<Leave>", start_leave)
stop_btn.bind("<Enter>", stop_hover)
stop_btn.bind("<Leave>", stop_leave)

# ---- Progress ----
_prog_frame = Frame(R, bg="#131C2E", bd=0, relief=FLAT, highlightbackground="#1F2D4A", highlightthickness=1)
_prog_frame.pack(fill=X, padx=20, pady=8)

progress_label = Label(_prog_frame, text="მზადაა სკანირებისთვის...",
                       font=("Segoe UI", 9), fg="#7B93B8", bg="#131C2E",
                       anchor="w", justify=LEFT, wraplength=560)
progress_label.pack(fill=X, padx=8, pady=(8, 2))

progress_bar = Progressbar(_prog_frame, mode='determinate',
                           style="Custom.Horizontal.TProgressbar")
progress_bar.pack(fill=X, padx=8, pady=(2, 8))

# ---- Main scan information (same style/content structure as 1-20 router) ----
scan_summary_text = Text(
    R,
    height=4,
    bg="#1A2540",
    fg="#7B93B8",
    font=("Segoe UI", 9),
    relief=FLAT,
    bd=0,
    wrap=WORD,
    state=DISABLED,
)
scan_summary_text.pack(fill=X, padx=20, pady=(0, 8))
scan_summary_text.config(state=NORMAL)
scan_summary_text.insert(
    END,
    "აქ გამოჩნდება დამუშავებული, დარჩენილი, დამთხვევები, შეცდომები, დრო და სიჩქარე.\n"
)
scan_summary_text.config(state=DISABLED)

# ---- History button ----
def open_scan_history() -> None:
    _open_history_window(root, mode="scan")

_hist_btn_frame = Frame(R, bg="#0B1120")
_hist_btn_frame.pack(fill=X, padx=20, pady=(0, 4))
Button(_hist_btn_frame, text="🕓  სკანირების ისტორია",
       command=open_scan_history,
       font=("Segoe UI", 9, "bold"), bg="#1A2540", fg="#4D7CFF",
       activebackground="#4D7CFF", activeforeground="#E8EDF5",
       relief=FLAT, bd=0, padx=14, pady=8, cursor="hand2").pack(fill=X)

_tools_frame = Frame(R, bg="#0B1120")
_tools_frame.pack(fill=X, padx=20, pady=(2, 8))
_tools = [
    ("🔍 Review Queue", open_review_queue),
    ("↶ ბოლო ოპერაციის Undo", undo_last_operation),
    ("⟲ ბოლო სკანირების Rollback", rollback_last_scan),
]
for idx, (txt, cmd) in enumerate(_tools):
    btn = Button(_tools_frame, text=txt, command=cmd, font=("Segoe UI", 8, "bold"),
                 bg="#1A2540", fg="#7B93B8", activebackground="#4D7CFF",
                 activeforeground="#0B1120", relief=FLAT, bd=0, padx=8, pady=7, cursor="hand2")
    btn.grid(row=idx // 2, column=idx % 2, sticky="ew", padx=3, pady=3)
_tools_frame.grid_columnconfigure(0, weight=1)
_tools_frame.grid_columnconfigure(1, weight=1)

# ---- Log panel ----
_log_outer = Frame(R, bg="#0B1120")
_log_outer.pack(fill=X, padx=20, pady=(0, 4))

_log_hdr = Frame(_log_outer, bg="#0B1120")
_log_hdr.pack(fill=X)
Label(_log_hdr, text="◈  ლოგი", font=("Segoe UI", 9, "bold"),
      fg="#7B93B8", bg="#0B1120").pack(side=LEFT)


def clear_log() -> None:
    log_text.config(state=NORMAL)
    log_text.delete("1.0", END)
    log_text.config(state=DISABLED)


Button(_log_hdr, text="გასუფთავება", command=clear_log,
       font=("Segoe UI", 8), bg="#1A2540", fg="#7B93B8",
       activebackground="#4D7CFF", activeforeground="#0B1120",
       relief=FLAT, bd=0, padx=8, pady=2, cursor="hand2").pack(side=RIGHT)

_log_inner = Frame(_log_outer, bg="#0B1120")
_log_inner.pack(fill=X, pady=(3, 0))

_log_sb = Scrollbar(_log_inner)
_log_sb.pack(side=RIGHT, fill=Y)

log_text = Text(_log_inner, height=5, bg="#131C2E", fg="#7B93B8",
                font=("Courier", 8), relief=FLAT, bd=0, wrap=WORD,
                state=DISABLED, yscrollcommand=_log_sb.set)
log_text.pack(side=LEFT, fill=X, expand=True)
_log_sb.config(command=log_text.yview)
log_text.tag_config("info",    foreground="#4D7CFF")
log_text.tag_config("error",   foreground="#E05555")
log_text.tag_config("warning", foreground="#F5A623")
log_text.tag_config("success", foreground="#2ECC7A")

_log_autoscroll = True


def _log_sb_cb(*args):
    global _log_autoscroll
    _log_sb.set(*args)
    try:
        _log_autoscroll = float(args[1]) >= 0.999
    except Exception:
        pass


log_text.config(yscrollcommand=_log_sb_cb)


def append_log(message: str, level: str = "info") -> None:
    log_text.config(state=NORMAL)
    ts = datetime.now().strftime("%H:%M:%S")
    log_text.insert(END, f"[{ts}] {message}\n", level)
    log_text.config(state=DISABLED)
    if _log_autoscroll:
        log_text.see(END)


class TkLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            lvl = "error" if record.levelno >= logging.ERROR else "warning" if record.levelno >= logging.WARNING else "info"
            post_ui(lambda m=msg, l=lvl: append_log(m, l))
        except Exception:
            pass


_tk_log_handler = TkLogHandler()
_tk_log_handler.setFormatter(logging.Formatter('%(levelname)s - %(message)s'))
logger.addHandler(_tk_log_handler)

# ---- Footer ----
Label(R, text="⚡  InsightFace AI", font=("Segoe UI", 8),
      fg="#3E5070", bg="#0B1120").pack(pady=10)

register_window_theme(root, "main")
root.after(100, _restore_saved_ui_state)

if __name__ == "__main__":
    root.mainloop()