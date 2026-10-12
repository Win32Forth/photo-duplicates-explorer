#!/usr/bin/env python3
"""Duplicates Explorer — browse a photo library, find duplicates, and clean up safely."""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import (  # noqa: E402
    get_photos_root as config_get_photos_root,
    get_port,
    load_config,
    save_config,
    set_photos_root,
)

STATIC_DIR = ROOT / "static"
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "index.db"
THUMB_DIR = DATA_DIR / "thumbs"
DUP_PROGRESS_PATH = DATA_DIR / "dup_progress.json"
DELETE_LOG_PATH = DATA_DIR / "delete_log.jsonl"
INDEX_PROGRESS_PATH = DATA_DIR / "index_progress.json"
TRASH_DIRNAME = ".explorer-trash"
APP_NAME = "Duplicates Explorer"

_index_lock = threading.Lock()
_index_thread: threading.Thread | None = None

BROWSER_OK = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
HEIC_EXT = {".heic", ".heif"}
THUMB_SRC_OK = BROWSER_OK | HEIC_EXT | {".tif", ".tiff"}
QUICKTIME_EXT = {".mov", ".mp4", ".m4v", ".m4a"}
PREVIEW_EXT = {".tif", ".tiff"}
QUICKTIME_APP = "/System/Applications/QuickTime Player.app"
PREVIEW_APP = "/System/Applications/Preview.app"

_dup_lock = threading.Lock()
_dup_thread: threading.Thread | None = None


def get_photos_root() -> Path:
    root = config_get_photos_root()
    if root is not None:
        return Path(root)
    raise FileNotFoundError(
        "No photo library folder configured. Open the setup page and choose a folder."
    )


def setup_status() -> dict:
    cfg = load_config()
    root = config_get_photos_root()
    root_path = Path(root) if root else None
    ready = bool(root_path and root_path.is_dir())
    index_exists = DB_PATH.exists()
    count = 0
    if index_exists:
        try:
            conn = sqlite3.connect(str(DB_PATH))
            count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            conn.close()
        except Exception:
            count = 0
    progress = {}
    if INDEX_PROGRESS_PATH.exists():
        try:
            progress = json.loads(INDEX_PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            progress = {}
    return {
        "app": APP_NAME,
        "configured": bool((cfg.get("photos_root") or "").strip()),
        "ready": ready,
        "photos_root": str(root_path) if root_path else None,
        "root_exists": bool(root_path and root_path.is_dir()),
        "index_exists": index_exists,
        "indexed_files": count,
        "index_progress": progress,
        "indexing": bool(_index_thread and _index_thread.is_alive()),
        "port": get_port(),
        "platform": sys.platform,
        "can_choose_folder": sys.platform == "darwin",
    }


def choose_folder_macos() -> dict:
    if sys.platform != "darwin":
        return {"ok": False, "error": "Folder picker is only available on macOS"}
    script = 'POSIX path of (choose folder with prompt "Choose a photo library folder")'
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    if r.returncode != 0:
        err = (r.stderr or r.stdout or "cancelled").strip()
        if "User canceled" in err or "(-128)" in err:
            return {"ok": False, "error": "cancelled", "cancelled": True}
        return {"ok": False, "error": err or "Folder picker failed"}
    path = (r.stdout or "").strip()
    if not path:
        return {"ok": False, "error": "No folder selected", "cancelled": True}
    try:
        chosen = set_photos_root(path)
        return {"ok": True, "photos_root": str(chosen)}
    except FileNotFoundError as e:
        return {"ok": False, "error": str(e)}


def start_indexing(root: Path | None = None) -> dict:
    global _index_thread
    with _index_lock:
        if _index_thread and _index_thread.is_alive():
            return {"ok": False, "error": "Indexing already running", "progress": _read_index_progress()}

        photos = Path(root) if root else config_get_photos_root()
        if not photos or not Path(photos).is_dir():
            return {"ok": False, "error": "Configure a valid photo folder first"}

        def worker():
            from scripts.index_photos import index_photos

            try:
                index_photos(Path(photos), DB_PATH, include_video=True)
            except Exception as e:
                INDEX_PROGRESS_PATH.write_text(
                    json.dumps(
                        {
                            "status": "error",
                            "error": str(e),
                            "finished_at": datetime.now().isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

        _index_thread = threading.Thread(target=worker, daemon=True, name="index-photos")
        _index_thread.start()
        return {"ok": True, "started": True, "photos_root": str(photos)}


def _read_index_progress() -> dict:
    if INDEX_PROGRESS_PATH.exists():
        try:
            return json.loads(INDEX_PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=60)
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if cols and "content_hash" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
        conn.commit()
    if cols:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash)")
        conn.commit()


def row_to_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


def safe_rel(rel: str) -> str | None:
    rel = unquote(rel or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        return None
    if rel == TRASH_DIRNAME or rel.startswith(TRASH_DIRNAME + "/"):
        # Allow browsing trash via API only when explicitly requested later; block accidental delete of trash paths from normal UI deletes of originals
        pass
    return rel


def thumb_path_for(rel: str) -> Path:
    h = hashlib.sha1(rel.encode("utf-8")).hexdigest()
    return THUMB_DIR / h[:2] / f"{h}.jpg"


def make_thumb(src: Path, dest: Path, size: int = 480) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    ext = src.suffix.lower()
    try:
        if ext in HEIC_EXT:
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "in.jpg"
                r = subprocess.run(
                    [
                        "/usr/bin/sips",
                        "-s",
                        "format",
                        "jpeg",
                        "-Z",
                        str(size),
                        str(src),
                        "--out",
                        str(tmp),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                if r.returncode != 0 or not tmp.exists():
                    return False
                dest.write_bytes(tmp.read_bytes())
                return True
        from PIL import Image, ImageOps

        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((size, size))
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            elif im.mode == "L":
                im = im.convert("RGB")
            im.save(dest, "JPEG", quality=82, optimize=True)
        return True
    except Exception:
        return False


def native_open_app_for(ext: str) -> tuple[str, str] | None:
    """Return (app_path_or_name, label) for extensions that should open outside the browser."""
    ext = (ext or "").lower()
    if ext in QUICKTIME_EXT:
        app = QUICKTIME_APP if Path(QUICKTIME_APP).exists() else "QuickTime Player"
        return app, "QuickTime Player"
    if ext in PREVIEW_EXT:
        app = PREVIEW_APP if Path(PREVIEW_APP).exists() else "Preview"
        return app, "Preview"
    return None


def item_payload(d: dict) -> dict:
    ext = (d.get("ext") or "").lower()
    d["can_preview"] = ext in BROWSER_OK or ext in HEIC_EXT
    native = native_open_app_for(ext)
    d["open_in_quicktime"] = ext in QUICKTIME_EXT  # kept for older UI
    d["open_native"] = bool(native)
    d["open_native_app"] = native[1] if native else None
    d["thumb"] = f"/thumb?path={d['rel_path']}"
    d["original"] = f"/original?path={d['rel_path']}"
    return d


def open_in_native_app(rel: str) -> dict:
    rel = safe_rel(rel)
    if not rel:
        return {"ok": False, "error": "invalid path"}
    ext = Path(rel).suffix.lower()
    native = native_open_app_for(ext)
    if not native:
        return {
            "ok": False,
            "error": f"Native open only for {sorted(QUICKTIME_EXT | PREVIEW_EXT)}",
        }
    app, label = native
    photos = get_photos_root()
    src = photos / rel
    if not src.is_file():
        return {"ok": False, "error": "file not found"}
    try:
        subprocess.Popen(
            ["/usr/bin/open", "-a", app, str(src)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"ok": True, "path": rel, "app": app, "label": label}
    except OSError as e:
        return {"ok": False, "error": str(e)}


def open_in_quicktime(rel: str) -> dict:
    """Backward-compatible wrapper."""
    return open_in_native_app(rel)


def reveal_in_finder(rel: str) -> dict:
    """Reveal a file in Finder (selects it), or open its parent folder."""
    rel = safe_rel(rel)
    if not rel:
        return {"ok": False, "error": "invalid path"}
    photos = get_photos_root()
    src = photos / rel
    if src.is_file():
        target = src
        mode = "reveal"
        cmd = ["/usr/bin/open", "-R", str(target)]
    elif src.is_dir():
        target = src
        mode = "folder"
        cmd = ["/usr/bin/open", str(target)]
    else:
        parent = src.parent
        photos_s = str(photos)
        if parent.is_dir() and (str(parent) == photos_s or str(parent).startswith(photos_s + os.sep)):
            target = parent
            mode = "folder"
            cmd = ["/usr/bin/open", str(target)]
        else:
            return {"ok": False, "error": "file not found"}
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {
            "ok": True,
            "path": rel,
            "mode": mode,
            "opened": str(target),
        }
    except OSError as e:
        return {"ok": False, "error": str(e)}


def append_delete_log(entry: dict) -> None:
    DELETE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with DELETE_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def trash_dir() -> Path:
    return get_photos_root() / TRASH_DIRNAME


def trash_stats() -> dict:
    trash = trash_dir()
    if not trash.is_dir():
        return {
            "exists": False,
            "path": str(trash),
            "files": 0,
            "bytes": 0,
            "batches": 0,
        }
    files = 0
    total_bytes = 0
    batches = 0
    try:
        batches = sum(1 for p in trash.iterdir() if p.is_dir() and not p.name.startswith("."))
    except OSError:
        batches = 0
    for dirpath, dirnames, filenames in os.walk(trash):
        dirnames[:] = [d for d in dirnames if d not in ("@eaDir", "#recycle") and not d.startswith(".")]
        for fn in filenames:
            if fn.startswith("."):
                continue
            files += 1
            try:
                total_bytes += os.path.getsize(os.path.join(dirpath, fn))
            except OSError:
                pass
    return {
        "exists": True,
        "path": str(trash),
        "rel_path": TRASH_DIRNAME,
        "files": files,
        "bytes": total_bytes,
        "batches": batches,
    }


def empty_trash(confirm_phrase: str) -> dict:
    if confirm_phrase != "EMPTY":
        return {"ok": False, "error": 'Empty trash requires confirm_phrase: "EMPTY"'}
    photos = get_photos_root()
    if not photos.is_dir():
        return {"ok": False, "error": f"Photos root not mounted: {photos}"}
    trash = (photos / TRASH_DIRNAME).resolve()
    photos_resolved = photos.resolve()
    try:
        trash.relative_to(photos_resolved)
    except ValueError:
        return {"ok": False, "error": "trash path escaped photos root"}
    if trash.name != TRASH_DIRNAME:
        return {"ok": False, "error": "refusing to empty unexpected trash path"}

    before = trash_stats()
    if not trash.exists():
        trash.mkdir(parents=True, exist_ok=True)
        append_delete_log(
            {
                "action": "empty_trash",
                "ok": True,
                "removed_files": 0,
                "removed_bytes": 0,
                "at": datetime.now().isoformat(timespec="seconds"),
            }
        )
        return {"ok": True, "removed_files": 0, "removed_bytes": 0, "already_empty": True}

    # Remove contents, keep/recreate the trash folder itself
    errors: list[str] = []
    for child in list(trash.iterdir()):
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        except OSError as e:
            errors.append(f"{child.name}: {e}")

    trash.mkdir(parents=True, exist_ok=True)
    after = trash_stats()
    entry = {
        "action": "empty_trash",
        "ok": not errors and after["files"] == 0,
        "removed_files": max(0, before["files"] - after["files"]),
        "removed_bytes": max(0, before["bytes"] - after["bytes"]),
        "errors": errors,
        "at": datetime.now().isoformat(timespec="seconds"),
    }
    append_delete_log(entry)
    return {
        "ok": entry["ok"],
        "removed_files": entry["removed_files"],
        "removed_bytes": entry["removed_bytes"],
        "remaining_files": after["files"],
        "errors": errors,
    }


def delete_paths(paths: list[str], mode: str) -> dict:
    """mode: 'trash' moves into Photos/.explorer-trash/; 'permanent' unlinks."""
    photos = get_photos_root()
    if not photos.is_dir():
        return {"ok": False, "error": f"Photos root not mounted: {photos}"}

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    trash_root = photos / TRASH_DIRNAME / stamp
    results = []
    conn = db()
    try:
        for raw in paths:
            rel = safe_rel(raw)
            if not rel:
                results.append({"path": raw, "ok": False, "error": "invalid path"})
                continue
            if rel == TRASH_DIRNAME or rel.startswith(TRASH_DIRNAME + "/"):
                results.append({"path": rel, "ok": False, "error": "refusing to operate on trash path"})
                continue
            src = photos / rel
            if not src.exists():
                conn.execute("DELETE FROM files WHERE rel_path = ?", (rel,))
                results.append({"path": rel, "ok": True, "action": "index_only_removed", "note": "file already missing"})
                continue
            if not src.is_file():
                results.append({"path": rel, "ok": False, "error": "not a file"})
                continue
            try:
                if mode == "trash":
                    dest = trash_root / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if dest.exists():
                        dest = dest.with_name(f"{dest.stem}__{int(time.time())}{dest.suffix}")
                    shutil.move(str(src), str(dest))
                    action = "trashed"
                    dest_rel = str(dest.relative_to(photos)).replace("\\", "/")
                elif mode == "permanent":
                    src.unlink()
                    action = "deleted"
                    dest_rel = None
                else:
                    results.append({"path": rel, "ok": False, "error": "mode must be trash or permanent"})
                    continue

                conn.execute("DELETE FROM files WHERE rel_path = ?", (rel,))
                # Drop cached thumbs
                for suffix in ("", "::full"):
                    tp = thumb_path_for(rel + suffix)
                    if tp.exists():
                        try:
                            tp.unlink()
                        except OSError:
                            pass
                entry = {
                    "path": rel,
                    "ok": True,
                    "action": action,
                    "dest": dest_rel,
                    "at": datetime.now().isoformat(timespec="seconds"),
                }
                results.append(entry)
                append_delete_log(entry)
            except OSError as e:
                results.append({"path": rel, "ok": False, "error": str(e)})
        conn.commit()
    finally:
        conn.close()

    ok = sum(1 for r in results if r.get("ok"))
    return {
        "ok": ok == len(results),
        "mode": mode,
        "trash_batch": str(trash_root.relative_to(photos)) if mode == "trash" else None,
        "succeeded": ok,
        "failed": len(results) - ok,
        "results": results,
    }


def dup_progress() -> dict:
    if DUP_PROGRESS_PATH.exists():
        try:
            return json.loads(DUP_PROGRESS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def start_dup_scan(limit: int | None = None, rehash: bool = False) -> dict:
    global _dup_thread
    with _dup_lock:
        if _dup_thread and _dup_thread.is_alive():
            return {"ok": False, "error": "Duplicate scan already running", "progress": dup_progress()}

        def worker():
            from scripts.find_duplicates import run as dup_run

            try:
                dup_run(DB_PATH, limit=limit, rehash=rehash)
            except Exception as e:
                DUP_PROGRESS_PATH.write_text(
                    json.dumps(
                        {
                            "status": "error",
                            "error": str(e),
                            "finished_at": datetime.now().isoformat(),
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )

        _dup_thread = threading.Thread(target=worker, daemon=True, name="dup-scan")
        _dup_thread.start()
        return {"ok": True, "started": True, "progress": dup_progress()}


def size_dup_stats(conn: sqlite3.Connection) -> dict:
    groups = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    files = conn.execute(
        """
        SELECT COUNT(*) FROM files WHERE size IN (
          SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    hashed = conn.execute(
        """
        SELECT COUNT(*) FROM files
        WHERE content_hash IS NOT NULL AND content_hash != ''
          AND size IN (SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1)
        """
    ).fetchone()[0]
    confirmed_groups = conn.execute(
        """
        SELECT COUNT(*) FROM (
          SELECT content_hash FROM files
          WHERE content_hash IS NOT NULL AND content_hash != ''
          GROUP BY content_hash HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    confirmed_files = conn.execute(
        """
        SELECT COUNT(*) FROM files WHERE content_hash IN (
          SELECT content_hash FROM files
          WHERE content_hash IS NOT NULL AND content_hash != ''
          GROUP BY content_hash HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    return {
        "size_groups": groups,
        "size_candidate_files": files,
        "hashed_candidates": hashed,
        "confirmed_groups": confirmed_groups,
        "confirmed_files": confirmed_files,
        "extra_copies": max(0, confirmed_files - confirmed_groups),
    }


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(STATIC_DIR), **kwargs)

    def end_headers(self):
        if self.path.startswith("/api/") or self.path.endswith((".html", ".js", ".css")):
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        if args and str(args[0]).startswith("/api/"):
            return
        super().log_message(fmt, *args)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/" or path == "/index.html":
            return self._send_file(STATIC_DIR / "index.html", "text/html; charset=utf-8")
        if path.startswith("/api/"):
            return self._api_get(path, qs)
        if path == "/thumb":
            return self._thumb(qs)
        if path == "/original":
            return self._original(qs)
        return super().do_GET()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return self._json({"error": "invalid JSON"}, 400)
        return self._api_post(path, body)

    def _json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, file_path: Path, content_type: str):
        if not file_path.exists():
            self.send_error(404)
            return
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _api_get(self, path: str, qs: dict):
        if path == "/api/setup":
            return self._json(setup_status())

        if not DB_PATH.exists():
            status = setup_status()
            return self._json(
                {
                    "error": "No index yet. Choose a photo folder in Setup, then index it.",
                    "setup": status,
                    "photos_root": status.get("photos_root"),
                },
                503,
            )

        conn = db()
        try:
            if path == "/api/meta":
                meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta")}
                total = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
                images = conn.execute("SELECT COUNT(*) FROM files WHERE kind='image'").fetchone()[0]
                videos = conn.execute("SELECT COUNT(*) FROM files WHERE kind='video'").fetchone()[0]
                years = [
                    r[0]
                    for r in conn.execute(
                        "SELECT year FROM files WHERE year IS NOT NULL GROUP BY year ORDER BY year DESC"
                    )
                ]
                tops = [
                    {"name": r[0], "count": r[1]}
                    for r in conn.execute(
                        "SELECT top, COUNT(*) AS c FROM files WHERE top != '' GROUP BY top ORDER BY c DESC"
                    )
                ]
                progress = _read_index_progress()
                try:
                    photos_root = str(get_photos_root())
                except FileNotFoundError:
                    photos_root = meta.get("photos_root")
                return self._json(
                    {
                        "app": APP_NAME,
                        "meta": meta,
                        "total": total,
                        "images": images,
                        "videos": videos,
                        "years": years,
                        "tops": tops,
                        "photos_root": photos_root,
                        "progress": progress,
                        "duplicates": size_dup_stats(conn),
                        "dup_progress": dup_progress(),
                        "trash_folder": TRASH_DIRNAME,
                        "trash": trash_stats() if photos_root else None,
                        "setup": setup_status(),
                    }
                )

            if path == "/api/trash":
                return self._json({"trash": trash_stats()})

            if path == "/api/folders":
                top = (qs.get("top") or [""])[0]
                if top:
                    rows = conn.execute(
                        """
                        SELECT folder, COUNT(*) AS c FROM files
                        WHERE top = ? AND folder != ''
                        GROUP BY folder ORDER BY folder
                        """,
                        (top,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT top AS folder, COUNT(*) AS c FROM files
                        WHERE top != '' GROUP BY top ORDER BY top
                        """
                    ).fetchall()
                return self._json({"folders": [{"path": r[0], "count": r[1]} for r in rows]})

            if path == "/api/search":
                q = (qs.get("q") or [""])[0].strip()
                year = (qs.get("year") or [""])[0].strip()
                top = (qs.get("top") or [""])[0].strip()
                folder = (qs.get("folder") or [""])[0].strip()
                kind = (qs.get("kind") or ["image"])[0].strip() or "image"
                page = max(1, int((qs.get("page") or ["1"])[0] or 1))
                page_size = min(96, max(12, int((qs.get("page_size") or ["48"])[0] or 48)))

                where = ["1=1"]
                args: list = []
                if kind and kind != "all":
                    where.append("kind = ?")
                    args.append(kind)
                if year:
                    where.append("year = ?")
                    args.append(int(year))
                if top:
                    where.append("top = ?")
                    args.append(top)
                if folder:
                    where.append("(folder = ? OR folder LIKE ?)")
                    args.extend([folder, folder.rstrip("/") + "/%"])
                if q:
                    like = f"%{q.lower()}%"
                    where.append(
                        "(LOWER(name) LIKE ? OR LOWER(folder) LIKE ? OR LOWER(rel_path) LIKE ?)"
                    )
                    args.extend([like, like, like])

                where_sql = " AND ".join(where)
                total = conn.execute(f"SELECT COUNT(*) FROM files WHERE {where_sql}", args).fetchone()[0]
                offset = (page - 1) * page_size
                rows = conn.execute(
                    f"""
                    SELECT id, rel_path, name, ext, kind, size, mtime, year, month, day, top, folder, content_hash
                    FROM files WHERE {where_sql}
                    ORDER BY (year IS NULL), year DESC, folder, name
                    LIMIT ? OFFSET ?
                    """,
                    [*args, page_size, offset],
                ).fetchall()
                items = [item_payload(row_to_dict(r)) for r in rows]
                return self._json(
                    {
                        "total": total,
                        "page": page,
                        "page_size": page_size,
                        "pages": max(1, (total + page_size - 1) // page_size),
                        "items": items,
                    }
                )

            if path == "/api/duplicates":
                page = max(1, int((qs.get("page") or ["1"])[0] or 1))
                page_size = min(50, max(5, int((qs.get("page_size") or ["20"])[0] or 20)))
                groups = conn.execute(
                    """
                    SELECT content_hash AS hash, COUNT(*) AS c, SUM(size) AS total_size, MIN(size) AS size
                    FROM files
                    WHERE content_hash IS NOT NULL AND content_hash != ''
                    GROUP BY content_hash
                    HAVING c > 1
                    ORDER BY c DESC, total_size DESC
                    """
                ).fetchall()
                total_groups = len(groups)
                start = (page - 1) * page_size
                slice_groups = groups[start : start + page_size]
                out = []
                for g in slice_groups:
                    files = conn.execute(
                        """
                        SELECT id, rel_path, name, ext, kind, size, mtime, year, month, day, top, folder, content_hash
                        FROM files WHERE content_hash = ?
                        ORDER BY
                          CASE WHEN top LIKE 'Photos Library%' THEN 1 ELSE 0 END,
                          length(rel_path) ASC,
                          mtime ASC
                        """,
                        (g["hash"],),
                    ).fetchall()
                    items = [item_payload(row_to_dict(r)) for r in files]
                    # Suggest keeping the first (prefer non-Photos-Library, shorter path)
                    keep = items[0]["rel_path"] if items else None
                    out.append(
                        {
                            "hash": g["hash"],
                            "count": g["c"],
                            "size": g["size"],
                            "keep": keep,
                            "items": items,
                        }
                    )
                return self._json(
                    {
                        "total_groups": total_groups,
                        "page": page,
                        "page_size": page_size,
                        "pages": max(1, (total_groups + page_size - 1) // page_size),
                        "stats": size_dup_stats(conn),
                        "progress": dup_progress(),
                        "groups": out,
                    }
                )

            if path == "/api/duplicates/status":
                return self._json(
                    {
                        "stats": size_dup_stats(conn),
                        "progress": dup_progress(),
                        "scanning": bool(_dup_thread and _dup_thread.is_alive()),
                    }
                )

            return self._json({"error": "unknown api"}, 404)
        finally:
            conn.close()

    def _api_post(self, path: str, body: dict):
        if path == "/api/duplicates/scan":
            limit = body.get("limit")
            limit_i = int(limit) if limit not in (None, "") else None
            return self._json(start_dup_scan(limit=limit_i, rehash=bool(body.get("rehash"))))

        if path == "/api/delete":
            paths = body.get("paths") or []
            if not isinstance(paths, list) or not paths:
                return self._json({"error": "paths[] required"}, 400)
            if len(paths) > 500:
                return self._json({"error": "max 500 paths per request"}, 400)
            mode = (body.get("mode") or "trash").strip().lower()
            if mode not in ("trash", "permanent"):
                return self._json({"error": "mode must be trash or permanent"}, 400)
            if not body.get("confirm"):
                return self._json({"error": "confirm:true required"}, 400)
            if mode == "permanent" and body.get("confirm_phrase") != "DELETE":
                return self._json(
                    {
                        "error": 'Permanent delete requires confirm_phrase: "DELETE"',
                    },
                    400,
                )
            result = delete_paths([str(p) for p in paths], mode=mode)
            status = 200 if result.get("ok") else 207
            return self._json(result, status)

        if path in ("/api/open-quicktime", "/api/open-native"):
            rel = body.get("path") or ""
            result = open_in_native_app(str(rel))
            return self._json(result, 200 if result.get("ok") else 400)

        if path in ("/api/reveal", "/api/open-folder"):
            rel = body.get("path") or ""
            result = reveal_in_finder(str(rel))
            return self._json(result, 200 if result.get("ok") else 400)

        if path == "/api/trash/empty":
            if not body.get("confirm"):
                return self._json({"error": "confirm:true required"}, 400)
            result = empty_trash(str(body.get("confirm_phrase") or ""))
            return self._json(result, 200 if result.get("ok") else 400)

        if path == "/api/setup":
            root = (body.get("photos_root") or "").strip()
            if not root:
                return self._json({"error": "photos_root required"}, 400)
            try:
                chosen = set_photos_root(root)
            except FileNotFoundError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            started = None
            if body.get("start_index"):
                started = start_indexing(chosen)
            return self._json(
                {
                    "ok": True,
                    "photos_root": str(chosen),
                    "setup": setup_status(),
                    "index": started,
                }
            )

        if path == "/api/choose-folder":
            result = choose_folder_macos()
            if result.get("ok") and body.get("start_index"):
                result["index"] = start_indexing(Path(result["photos_root"]))
            if result.get("ok"):
                result["setup"] = setup_status()
            return self._json(result, 200 if result.get("ok") or result.get("cancelled") else 400)

        if path == "/api/index/start":
            result = start_indexing()
            return self._json(result, 200 if result.get("ok") else 400)

        return self._json({"error": "unknown api"}, 404)

    def _thumb(self, qs: dict):
        rel = safe_rel((qs.get("path") or [""])[0])
        if not rel:
            self.send_error(400)
            return
        photos = get_photos_root()
        src = photos / rel
        if not src.is_file():
            self.send_error(404)
            return
        ext = src.suffix.lower()
        dest = thumb_path_for(rel)
        if not dest.exists():
            if ext not in THUMB_SRC_OK:
                self.send_error(415)
                return
            if not make_thumb(src, dest):
                self.send_error(500)
                return
        data = dest.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def _original(self, qs: dict):
        rel = safe_rel((qs.get("path") or [""])[0])
        if not rel:
            self.send_error(400)
            return
        photos = get_photos_root()
        src = photos / rel
        if not src.is_file():
            self.send_error(404)
            return
        ext = src.suffix.lower()
        if ext in HEIC_EXT:
            dest = thumb_path_for(rel + "::full")
            if not dest.exists():
                if not make_thumb(src, dest, size=2048):
                    self.send_error(500)
                    return
            data = dest.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return

        ctype = mimetypes.guess_type(str(src))[0] or "application/octet-stream"
        data = src.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{src.name}"')
        self.end_headers()
        self.wfile.write(data)


def main():
    import argparse

    ap = argparse.ArgumentParser(description=f"{APP_NAME} local server")
    ap.add_argument("--root", type=Path, default=None, help="Photo library folder")
    ap.add_argument("--port", type=int, default=None, help="Port (default from config or 8766)")
    args = ap.parse_args()

    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    if args.root:
        try:
            set_photos_root(args.root)
        except FileNotFoundError as e:
            raise SystemExit(str(e))

    port = get_port(args.port)
    if args.port:
        save_config({"port": port})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"{APP_NAME} → http://127.0.0.1:{port}/", flush=True)
    try:
        print(f"Photos root: {get_photos_root()}", flush=True)
    except FileNotFoundError:
        print("Photos root: (not configured — open the Setup tab)", flush=True)
    if not DB_PATH.exists():
        print(
            "No index yet. Use Setup in the UI, or: python3 scripts/index_photos.py",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
