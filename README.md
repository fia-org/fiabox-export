# FIAbox → Box migration toolkit

Two small, dependency-free Python scripts that inventory and export a FIAbox
(Numsync/Visiativ) file-sharing instance via its REST API, for migration to Box.

- **`enumerate.py`** — read-only metadata crawl: every account's files, folders,
  shares and public links → CSVs in `exports/`.
- **`download.py`** — downloads the actual file contents, resumable and retrying.

No third-party packages — Python 3.8+ standard library only.

## 1. Setup

```bash
cp .env.example .env
# edit .env and fill in a superAdmin login + password
```

`.env` (read by both scripts; never committed):

```
FIABOX_BASE=https://fiabox.fia.com
FIABOX_LOGIN=you@example.com
FIABOX_PASSWORD=your-password
```

The login must be a **superAdmin** account — the scripts mint a per-user token via
`POST /ws/api/accounts/authBySuperAdmin/{email}` to read each account.

Provide the list of accounts to process in **`emails.txt`** (one email per line;
see `emails.example.txt`). The user API has no "list all accounts" endpoint, so this
list comes from outside — e.g. an admin-console user export.

## 2. Inventory (metadata only, read-only)

```bash
python3 -I enumerate.py                 # full run over emails.txt -> exports/*.csv
python3 -I enumerate.py --probe a@b.com # single account, print to stdout
```

Writes to `exports/`: `accounts_summary.csv`, `files.csv`, `folders.csv`, `shares.csv`,
`public_links.csv`. See **`SCHEMA.md`** for the column-by-column schema, the
join model, and migration caveats.

## 3. Download file contents

```bash
python3 -I download.py you@example.com          # one account
python3 -I download.py --all                     # every account in emails.txt
python3 -I download.py you@example.com --list    # list files, download nothing
python3 -I download.py you@example.com --fix-nonascii   # recover non-ASCII-named files
```

Options: `--dest DIR` (default `downloads/`), `--retries N` (default 4),
`--match STR` (only paths containing STR), `--keep-renamed` (with `--fix-nonascii`).

- **Resumable:** re-running skips any file already on disk with a matching size.
- **Layout:** `downloads/<email>/<cloud_folder_name>/<path>`.
- **Non-ASCII names:** files whose path contains a non-ASCII char return HTTP 520 on
  download (an app-side serving bug). A normal run parks them; `--fix-nonascii`
  recovers them via a rename round-trip. See caveat 1 in `SCHEMA.md`.

## How it works (API notes)

- Auth: send the token as an HTTP header `token: <tok>` (not `Authorization`).
- A browser `User-Agent` is required (Cloudflare blocks the default urllib UA).
- Files are addressed by `GET /ws/api/files/{idCloudFolder}/{url-encoded path}` —
  a numeric cloud-folder id + the file's path relative to that folder's root.
- `openapi.json` is the full API spec for reference.

## Files in this repo

| Path | What it is |
|---|---|
| `enumerate.py`, `download.py` | the two scripts |
| `.env.example` | credential template (copy to `.env`) |
| `emails.example.txt` | format for the account list (copy to `emails.txt`) |
| `openapi.json` | FIAbox REST API spec |
| `SCHEMA.md` | schema + join model + caveats for the exported CSVs |

Not committed (see `.gitignore`): `.env`, `emails.txt`, downloaded content,
exported CSVs, and any admin exports (`Users.csv`, `allEvents.csv`) — these hold
credentials or personal data and are shared separately, not via the repo.
