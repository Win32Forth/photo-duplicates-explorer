# Duplicates Explorer

Local tool for large photo libraries: browse by year/folder, find duplicates, and clean up safely.

Works with any folder you can see from your computer — Synology/NAS mounts, external drives, or a local disk. It does **not** require WordPress, Synology Photos, or a paid cloud plan.

**UI:** http://127.0.0.1:8766/

Repository: https://github.com/Win32Forth/photo-duplicates-explorer

## Install from GitHub

```bash
git clone https://github.com/Win32Forth/photo-duplicates-explorer.git
cd photo-duplicates-explorer
pip3 install -r requirements.txt
python3 server.py
```

Then open http://127.0.0.1:8766/ and use the **Setup** tab to choose your photo folder.

## Features

- **Setup** — choose any photo library folder (macOS folder picker or paste a path)
- **Browse** — year, album/top folder, subfolder, and filename search
- **Duplicates** — hash same-size candidates; review groups; keep one copy
- **Open files** — QuickTime for `.mov`/`.mp4`/`.m4v`, Preview for `.tif`/`.tiff`, Finder reveal
- **Delete** — move to `.explorer-trash/` inside the library (default) or permanent delete
- **Empty trash** — clear `.explorer-trash/` after typing `EMPTY`
- **Batch cleanup** — select duplicate groups and **Delete selected Duplicate Groups**

Index and thumbnails stay on this computer under `data/`. The photo folder is only changed when you delete or empty trash.

## Quick start

1. Make sure the photo folder is available on this computer (for a NAS: mount the share in Finder first).
2. Install the thumbnail dependency once:

```bash
pip3 install pillow
```

3. Start the app:

```bash
cd photo-duplicates-explorer   # or ~/Sites/duplicates-explorer
python3 server.py
```

4. Open http://127.0.0.1:8766/  
   - First run opens the **Setup** tab if no folder is configured yet.  
   - Choose a folder (macOS **Choose folder…**, or paste a path and **Save folder**).  
   - Click **Build / refresh index**.  
   - When indexing finishes, the Setup status shows the indexed file count; Browse works on partial results while it runs.

Keep `server.py` running while you use the UI. Stop it with Ctrl+C in that terminal.

### Command-line setup (optional)

```bash
cd photo-duplicates-explorer
python3 server.py --root /path/to/your/photos --port 8766
# in another terminal, if you prefer not to use the Setup button:
python3 scripts/index_photos.py
python3 scripts/find_duplicates.py
```

You can also copy `config.example.json` to `data/config.json` and edit `photos_root`.

`--root` writes `photos_root` into `data/config.json`. Later you can run plain `python3 server.py` and it will reuse that config.

## Requirements

- Python 3.10+ recommended
- [Pillow](https://pypi.org/project/Pillow/) for JPEG/PNG thumbnails (`pip3 install pillow`)
- macOS extras (optional but used when present):
  - `sips` for HEIC thumbnails
  - QuickTime Player / Preview for native open
  - Finder reveal and folder picker via `osascript`

On other systems, browse/delete/duplicates still work; native openers and the folder picker are macOS-oriented.

## Browse

- Filter by year, top folder, subfolder, keyword
- Images / videos / both
- Thumbnails + lightbox for common image types
- Select photos and delete (see below)

## Delete

- **Default:** move into `<library>/.explorer-trash/<timestamp>/…` (recoverable in Finder)
- **Permanent:** type `DELETE` to confirm
- **Empty trash…:** type `EMPTY` to permanently clear `.explorer-trash/` only
- Log: `data/delete_log.jsonl`

## Duplicates

1. Open the **Duplicates** tab  
2. **Find / continue duplicates** (hashes files that share a byte size)  
3. One copy is marked **keep** (prefers non–`Photos Library*` paths)  
4. **View** / **Folder** / per-group delete, or check groups and use **Delete selected Duplicate Groups**

Fingerprint = size + first 1MB + last 64KB (fast over SMB; not a full-file cryptographic hash).

```bash
python3 scripts/find_duplicates.py
python3 scripts/find_duplicates.py --limit 1000
```

## Configuration

Stored in `data/config.json`:

```json
{
  "photos_root": "/path/to/your/photos",
  "port": 8766
}
```

## Project layout

| Path | Purpose |
|------|---------|
| `server.py` | Local web UI + APIs |
| `config.py` | Shared config helpers |
| `scripts/index_photos.py` | Build/refresh `data/index.db` |
| `scripts/find_duplicates.py` | Duplicate fingerprint scan |
| `static/` | Web UI |
| `data/config.json` | Chosen library path + port |
| `data/index.db` | Local media index |
| `data/thumbs/` | Cached thumbnails |
| `data/*_progress.json` | Index / duplicate scan status |

## Notes for large libraries

- First index over SMB can take a long time; the UI works on partial results
- Duplicate scanning only hashes size-collision candidates, but that set can still be huge
- Prefer deleting extras from `Photos Library*` dumps before touching dated original folders
- Keep a backup before bulk permanent deletes
