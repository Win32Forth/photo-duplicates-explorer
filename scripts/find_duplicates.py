#!/usr/bin/env python3
"""
Hash size-collision candidates and mark exact-ish duplicates.

Uses a content fingerprint (size + first 1MB + last 64KB) so a full
900k-file SMB scan is not required. Only files that share a byte-size
with at least one other file are hashed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from config import get_photos_root  # noqa: E402

DB_PATH = ROOT / "data" / "index.db"
PROGRESS_PATH = ROOT / "data" / "dup_progress.json"


def ensure_schema(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    if "content_hash" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN content_hash TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_size ON files(size)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash)")
    conn.commit()


def write_progress(payload: dict) -> None:
    PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROGRESS_PATH.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def fingerprint(path: Path, size: int) -> str | None:
    try:
        h = hashlib.sha1()
        h.update(f"size:{size}|".encode("utf-8"))
        with path.open("rb") as f:
            chunk = f.read(1024 * 1024)
            h.update(chunk)
            if size > 1024 * 1024:
                f.seek(max(0, size - 65536))
                h.update(f.read(65536))
        return h.hexdigest()
    except OSError:
        return None


def photos_root(conn: sqlite3.Connection) -> Path:
    configured = get_photos_root()
    if configured and Path(configured).is_dir():
        return Path(configured)
    row = conn.execute("SELECT value FROM meta WHERE key='photos_root'").fetchone()
    if row and row[0]:
        return Path(row[0])
    raise SystemExit("No photo folder configured. Set one in the app or data/config.json")


def run(db_path: Path, limit: int | None = None, rehash: bool = False) -> dict:
    if not db_path.exists():
        raise SystemExit(f"Missing index DB: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    root = photos_root(conn)
    if not root.is_dir():
        raise SystemExit(f"Photos root not mounted: {root}")

    where = "size IN (SELECT size FROM files GROUP BY size HAVING COUNT(*) > 1)"
    if not rehash:
        where += " AND (content_hash IS NULL OR content_hash = '')"

    rows = conn.execute(
        f"""
        SELECT id, rel_path, size FROM files
        WHERE {where}
        ORDER BY size DESC, rel_path
        {f'LIMIT {int(limit)}' if limit else ''}
        """
    ).fetchall()

    total = len(rows)
    t0 = time.time()
    hashed = 0
    failed = 0
    write_progress(
        {
            "status": "running",
            "started_at": datetime.now(timezone.utc).isoformat(),
            "candidates": total,
            "hashed": 0,
            "failed": 0,
        }
    )

    for i, row in enumerate(rows, 1):
        src = root / row["rel_path"]
        fp = fingerprint(src, int(row["size"])) if src.is_file() else None
        if fp:
            conn.execute(
                "UPDATE files SET content_hash = ? WHERE id = ?",
                (fp, row["id"]),
            )
            hashed += 1
        else:
            failed += 1

        if i % 50 == 0 or i == total:
            conn.commit()
            write_progress(
                {
                    "status": "running",
                    "candidates": total,
                    "hashed": hashed,
                    "failed": failed,
                    "elapsed_sec": round(time.time() - t0, 1),
                    "current": row["rel_path"],
                }
            )
            print(f"… hashed {hashed}/{total} (failed {failed})", flush=True)

    conn.commit()

    groups = conn.execute(
        """
        SELECT content_hash, COUNT(*) AS c
        FROM files
        WHERE content_hash IS NOT NULL AND content_hash != ''
        GROUP BY content_hash
        HAVING c > 1
        """
    ).fetchall()
    dup_files = sum(r["c"] for r in groups)
    summary = {
        "status": "complete",
        "candidates": total,
        "hashed": hashed,
        "failed": failed,
        "duplicate_groups": len(groups),
        "duplicate_files": dup_files,
        "extra_copies": max(0, dup_files - len(groups)),
        "elapsed_sec": round(time.time() - t0, 1),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    write_progress(summary)
    conn.execute(
        "INSERT INTO meta(key,value) VALUES('dup_scanned_at',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (summary["finished_at"],),
    )
    conn.commit()
    conn.close()
    print("Done:", summary)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=DB_PATH)
    ap.add_argument("--limit", type=int, default=None, help="Hash at most N candidates")
    ap.add_argument("--rehash", action="store_true")
    args = ap.parse_args()
    run(args.db, limit=args.limit, rehash=args.rehash)


if __name__ == "__main__":
    main()
