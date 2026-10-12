#!/usr/bin/env python3
"""Walk a photo library folder and build a local SQLite index (source is not modified)."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import get_photos_root  # noqa: E402

DB_PATH = ROOT / "data" / "index.db"
PROGRESS_PATH = ROOT / "data" / "index_progress.json"

SKIP_DIRS = {"@eaDir", "#recycle", ".DS_Store", "Thumbs", ".thumbnails", ".TemporaryItems", ".explorer-trash"}
IMG_EXT = {
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".heif",
    ".raw",
    ".cr2",
    ".nef",
    ".arw",
    ".dng",
    ".orf",
    ".rw2",
    ".raf",
    ".x3f",
}
VIDEO_EXT = {".mov", ".mp4", ".m4v", ".avi", ".mkv", ".mpg", ".mpeg"}

YEAR_RE = re.compile(r"^(19|20)\d{2}$")
DATE_FOLDER_RE = re.compile(
    r"^(?P<y>(19|20)\d{2})[-_/ ]?(?P<m>\d{1,2})[-_/ ]?(?P<d>\d{1,2})(?:\b|[^0-9])"
)
YEAR_IN_NAME_RE = re.compile(r"\b((?:19|20)\d{2})\b")


def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS meta (
          key TEXT PRIMARY KEY,
          value TEXT
        );
        CREATE TABLE IF NOT EXISTS files (
          id INTEGER PRIMARY KEY,
          rel_path TEXT NOT NULL UNIQUE,
          name TEXT NOT NULL,
          ext TEXT NOT NULL,
          kind TEXT NOT NULL,
          size INTEGER NOT NULL,
          mtime REAL NOT NULL,
          year INTEGER,
          month INTEGER,
          day INTEGER,
          top TEXT,
          folder TEXT,
          indexed_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_files_year ON files(year);
        CREATE INDEX IF NOT EXISTS idx_files_top ON files(top);
        CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
        CREATE INDEX IF NOT EXISTS idx_files_name ON files(name);
        CREATE INDEX IF NOT EXISTS idx_files_folder ON files(folder);
        """
    )
    return conn


def infer_ymd(rel_parts: list[str], mtime: float) -> tuple[int | None, int | None, int | None]:
    year = month = day = None
    for part in rel_parts:
        m = DATE_FOLDER_RE.match(part)
        if m:
            year = int(m.group("y"))
            month = int(m.group("m"))
            day = int(m.group("d"))
            if 1 <= month <= 12 and 1 <= day <= 31:
                return year, month, day
        if YEAR_RE.match(part):
            year = int(part)
    if year is None:
        for part in rel_parts:
            m = YEAR_IN_NAME_RE.search(part)
            if m:
                year = int(m.group(1))
                break
    if year is None and mtime:
        try:
            dt = datetime.fromtimestamp(mtime)
            year, month, day = dt.year, dt.month, dt.day
        except (OverflowError, OSError, ValueError):
            pass
    return year, month, day


def kind_for(ext: str) -> str | None:
    if ext in IMG_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    return None


def write_progress(payload: dict) -> None:
    import json

    PROGRESS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def index_photos(photos_root: Path, db_path: Path, include_video: bool) -> None:
    if not photos_root.is_dir():
        raise SystemExit(f"Photos root not found: {photos_root}\nMount the Synology share first.")

    conn = connect(db_path)
    now = time.time()
    seen: set[str] = set()
    scanned = 0
    upserted = 0
    skipped = 0
    t0 = time.time()

    write_progress(
        {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "photos_root": str(photos_root),
            "scanned": 0,
            "upserted": 0,
        }
    )

    sql = """
      INSERT INTO files (rel_path, name, ext, kind, size, mtime, year, month, day, top, folder, indexed_at)
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
      ON CONFLICT(rel_path) DO UPDATE SET
        name=excluded.name,
        ext=excluded.ext,
        kind=excluded.kind,
        size=excluded.size,
        mtime=excluded.mtime,
        year=excluded.year,
        month=excluded.month,
        day=excluded.day,
        top=excluded.top,
        folder=excluded.folder,
        indexed_at=excluded.indexed_at
    """

    batch: list[tuple] = []

    for dirpath, dirnames, filenames in os.walk(photos_root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
        )
        rel_dir = os.path.relpath(dirpath, photos_root)
        if rel_dir == ".":
            rel_parts: list[str] = []
            folder = ""
            top = ""
        else:
            rel_parts = rel_dir.split(os.sep)
            folder = rel_dir.replace("\\", "/")
            top = rel_parts[0]

        for fn in filenames:
            if fn.startswith(".") or fn in ("Thumbs.db", "desktop.ini"):
                continue
            scanned += 1
            ext = os.path.splitext(fn)[1].lower()
            kind = kind_for(ext)
            if kind is None:
                skipped += 1
                continue
            if kind == "video" and not include_video:
                skipped += 1
                continue

            abs_path = os.path.join(dirpath, fn)
            try:
                st = os.stat(abs_path)
            except OSError:
                skipped += 1
                continue

            rel_path = fn if not folder else f"{folder}/{fn}"
            rel_path = rel_path.replace("\\", "/")
            year, month, day = infer_ymd(rel_parts + [fn], st.st_mtime)
            seen.add(rel_path)
            batch.append(
                (
                    rel_path,
                    fn,
                    ext,
                    kind,
                    int(st.st_size),
                    float(st.st_mtime),
                    year,
                    month,
                    day,
                    top,
                    folder,
                    now,
                )
            )
            upserted += 1

            if len(batch) >= 400:
                conn.executemany(sql, batch)
                conn.commit()
                batch.clear()
                write_progress(
                    {
                        "status": "running",
                        "photos_root": str(photos_root),
                        "scanned": scanned,
                        "upserted": upserted,
                        "skipped": skipped,
                        "elapsed_sec": round(time.time() - t0, 1),
                        "current_folder": folder or "(root)",
                    }
                )
                print(
                    f"… {upserted} media / {scanned} files  [{folder or 'root'}]",
                    flush=True,
                )

    if batch:
        conn.executemany(sql, batch)
        conn.commit()

    # Remove entries that disappeared (optional, only under this root index)
    existing = {row[0] for row in conn.execute("SELECT rel_path FROM files")}
    gone = existing - seen
    if gone:
        conn.executemany("DELETE FROM files WHERE rel_path = ?", [(p,) for p in gone])
        conn.commit()

    conn.execute(
        "INSERT INTO meta(key,value) VALUES('photos_root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(photos_root),),
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('indexed_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('count',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(upserted),),
    )
    conn.commit()

    counts = dict(conn.execute("SELECT kind, COUNT(*) FROM files GROUP BY kind"))
    years = conn.execute(
        "SELECT COUNT(DISTINCT year) FROM files WHERE year IS NOT NULL"
    ).fetchone()[0]
    tops = conn.execute("SELECT COUNT(DISTINCT top) FROM files WHERE top != ''").fetchone()[0]
    conn.close()

    summary = {
        "status": "complete",
        "photos_root": str(photos_root),
        "scanned": scanned,
        "upserted": upserted,
        "skipped": skipped,
        "removed": len(gone),
        "by_kind": counts,
        "years": years,
        "tops": tops,
        "elapsed_sec": round(time.time() - t0, 1),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    write_progress(summary)
    print("Done:", summary)


def main() -> None:
    ap = argparse.ArgumentParser(description="Index a photo library folder for Duplicates Explorer.")
    ap.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Photo library root (default: configured photos_root in data/config.json)",
    )
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument(
        "--include-video",
        action="store_true",
        default=True,
        help="Include videos in the index (default on).",
    )
    ap.add_argument("--images-only", action="store_true", help="Skip videos.")
    args = ap.parse_args()
    root = args.root or get_photos_root()
    if not root:
        raise SystemExit(
            "No photo folder configured. Run the app setup, or pass --root /path/to/photos"
        )
    include_video = not args.images_only
    index_photos(Path(root), args.db, include_video=include_video)


if __name__ == "__main__":
    main()
