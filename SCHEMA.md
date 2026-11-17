# FIAbox inventory — migration export

Snapshot of FIAbox (Numsync) via the REST API, for the Box migration.
Metadata (files, folders, shares, links) re-walked **2026-11-10**; file **contents** snapshot **2026-11-09**.
Covers **252 accounts** from the user export; **236** reachable, **181** hold files.

## Totals
- **97,808 files**, **902.2 GB**
- **13,923 folders** (1,774 cloud-folder roots + sub-folders; **3,119 are empty**)
- **627 share edges** (folder permissions)
- **1,431 public links** (239 password-protected)

## Files
| File | What's in it |
|---|---|
| `accounts_summary.csv` | One row per account: `account_id`, file count, `size_MB`, `size_GB`, folder count, `newest_file_utc`, `shared_with_me` (yes if any folder is shared *with* this account), `last_login` (UTC; see note). Sorted largest first. |
| `files.csv` | One row per file: `owner_email`, `account_id`, `cloud_folder_id`, `cloud_folder_name`, `path`, `size` (bytes), `lastModified_utc`. |
| `folders.csv` | One row per folder, **including empty ones** and each cloud-folder root (`is_root=True`, `folder_path='.'`). Lets the vendor recreate the full tree and join folder-level shares/links to a real folder. Columns: `owner_email`, `account_id`, `cloud_folder_id`, `cloud_folder_name`, `folder_path`, `is_root`, `lastModified_utc`. |
| `shares.csv` | Permission map. One row per share edge: `grantor_email` (owner) → `grantee_email`, `shareType` (READ/WRITE), `idCloudFolder` + `path` (the shared folder). `direction` = owned/received (which side it was seen from). **Sharing is folder-level only** — FIAbox has no per-file ACL (a file is shared only by living in a shared folder, or via a public link). |
| `public_links.csv` | One row per public link: `owner_email`, `owner_account_id`, `link_id`, URL, `idCloudFolder` + `path` (target folder or file), `validity_days`, `uploadAllowed`, `hasPassword`, `created_utc`. |
| `nested_access_exceptions.csv` | The 61 folders whose sharing does **not** simply inherit from the parent: 17 sub-folders shared differently than a shared ancestor (added recipient or READ↔WRITE change) and 44 sub-folders shared while the parent is not shared. These need explicit per-folder collaborations in Box rather than inheritance. |
| `non_ascii_files.csv` | The 2,218 files that can't be exported by the app (see caveat 1). |

All timestamps are **UTC**. Link passwords exist (239) but are **not** in these files; they can be exported separately for the cut-over.

## How the exports join (for the vendor)
Everything keys off **`cloud_folder_id`** (the same value is called `idCloudFolder` in `shares.csv`
and `public_links.csv`) plus a **`path`**. `path` is relative to the cloud-folder root; the root
itself is `.`. The API exposes **no per-file ID**, so a file's identity is the composite
(`cloud_folder_id`, `path`) — stable and unique within the export.

| To recreate… | Use | Join key |
|---|---|---|
| Users / accounts | `accounts_summary.csv` (+ `Users.csv` for entity, quota, activated, creation date) | `account_id` or `email` |
| Folder tree (incl. empty) | `folders.csv` | (`cloud_folder_id`, `folder_path`) |
| Files | `files.csv` | file = (`cloud_folder_id`, `path`); owner → accounts via `account_id`/`owner_email`; parent folder = (`cloud_folder_id`, dirname of `path`) → `folders.csv` |
| Folder-level shares | `shares.csv` | (`idCloudFolder`, `path`) → `folders.csv`; `grantor_email`/`grantee_email` → accounts (a grantee may be an external email with no account row) |
| Public links | `public_links.csv` | (`idCloudFolder`, `path`) → `folders.csv` **or** `files.csv` (a link can target a file); `owner_account_id`/`owner_email` → accounts |

**Join-integrity check (this snapshot):**
- Files → parent folder: every file's cloud folder is present in `folders.csv`.
- Shares → folder: **616 / 627** resolve. The 11 that don't are **dangling** — the target folder was renamed or deleted but the permission record remains. Ignore or confirm.
- Links → folder/file: **1,381 / 1,431** resolve. Of the 50 that don't: 4 are non-ASCII paths (caveat 1); the rest point into the few accounts whose folders can't be listed (caveat 2) or to since-deleted folders.

## Not captured here — available only with admin-API access
This inventory used superAdmin **impersonation** on the user API. The separate admin API
(`/ws/admin/api/*`) is locked (needs the admin-console credential, held on the VM). It has
**no file listing or download** — so the 900 GB of file content can *only* come via the user
API, as done here. Full breakdown of what the admin endpoints offer:

| Admin endpoint group | What it gives | Above what we have? |
|---|---|---|
| `accounts` (list/get) | All accounts with role/entity, filters, pagination | **No** — we have this from `Users.csv` + our inventory |
| `shares`, `shares/{id}/accounts` | Every share + recipients, org-wide in one pass | **Mostly no** — we reconstructed 627 edges by cross-referencing. But cleaner, and covers the 16 accounts we couldn't reach |
| `publiclinks` | All public links | **No** — we have 1,431 (incl. password flags) |
| `entities` | Org/tenant structure + quota policy: `quotaMaxInGo`, user caps, whitelists, `sharingBypassWhiteList`, `exceedingQuotaPolicy`, mail settings | **YES — new** |
| `workingGroups`, `/accounts` | Group definitions + membership | **YES — new**, and likely fills a gap (see below) |
| `agents`, `synchros` | Desktop sync-agent registrations + sync configs — which folders sync to which machines | **YES — new** |
| `events/web`, `events/synchro` | Full activity log — downloads, uploads, sharing actions, sync events | **YES — richer** than the login-only CSV |
| `configurations` | System-wide config keys | New, but not migration-critical |
| `version` | App version | Trivial |

**Genuinely additive with admin access:**
- **Working groups** — likely fills a gap: some rows in `shares.csv` have a `shareType` but blank `grantee_email`; those are probably group shares the user API can't expand.
- **Entities** — tenant/quota structure (whitelists, sharing policies) we have no visibility into.
- **Sync topology** (`agents`/`synchros`) — which users sync which folders to which desktops.
- **True activity** (`events/web`) — downloads/uploads per file, not just logins — the definitive "is this account actually used" signal.
- **The 16 deactivated accounts** — admin lists include them; impersonation cannot reach them.

For files, shares, links and last-login, this export already has what's needed; the admin
credential is only worth pursuing for group permissions, quota structure, sync usage, or true
download/upload activity.

## Caveats
1. **1,891 files (18.8 GB, 87 accounts — 1.9% of files, 2.1% of size) can't be downloaded as-is (HTTP 520), but there's a working fix, no VM access needed.** Any file whose download **path** contains a non-ASCII character (accents, en/em dashes, `°`, curly quotes, etc.) errors (HTTP 520) when downloaded, in both the API and the web UI. The bug is only in the file-*serving* path: the rename endpoint (`PUT /ws/api/files`) accepts the same path fine. So `download.py --fix-nonascii` recovers them by **rename → download → rename back** (non-destructive — the file ends with its original name; see note below on links). It first tries the file as-is and with normalized paths, which recovers the non-breaking-space cases with no rename at all.
   - **1,881 files (18.7 GB, 86 accounts) genuinely 520** (accents 1,340; other symbols 281; dashes 94; curly-quote/mixed/other 149) and need the rename round-trip.
   - **10 files are non-breaking-space** — a normalized request fetches them, no rename.
   - (`non_ascii_files.csv` lists 2,218 rows — the extra 327 have the non-ASCII character only in the *folder name*, not the download path, so they download normally and are **not** in the 1,891.)

   Tested on one account so far. Link safety: renaming only touches the one file during its download window; only **40 public links** across all accounts point directly at a non-ASCII file (the 178 folder-level links are untouched, since only files are renamed, not folders). The other ~95,900 files (98%) download normally.
2. **A few accounts can't be enumerated (content exists but is invisible to the API).** For ~3 accounts — notably `aljaber@fia.com` (REGIONAL DEVELOPMENT PROGRAMME 2025, FIA INNOVATION CHALLENGE 2024) — the folder-listing endpoint returns 404/500 for *every* path, so their files don't appear in `files.csv`/`folders.csv` even though the content exists (its public links still work). Unlike caveat 1 there's no API workaround — these need VM-level recovery. They surface as zero-file accounts that nonetheless own public links.
3. **Dangling shares/links.** 11 shares and some links point to folders that were since renamed or deleted; the permission/link record outlived the folder. These won't join to `folders.csv` — expected, not a data error.
4. **16 accounts not inventoried.** The deactivated accounts in the user export; they return no token. Assumed out of scope — confirm none hold needed data.
5. **~82 accounts are empty or stale.** 71 have zero files; 11 more have nothing modified since before 2021 (back to 2016). Candidates to archive rather than migrate live — filter on `files` and `newest_file_utc` in `accounts_summary.csv`.
6. **`last_login` source & method.** Derived from the admin event log (`allEvents.csv`, exported from the console; login events back to May 2021). It is the most recent genuine `ACCOUNT_LOGGED_IN`, **excluding automated bulk sweeps** — superadmin impersonation runs (migration-prep PowerShell scripts and this inventory's own API calls) that logged into 20+ accounts from one IP within minutes. Those are tool activity, not user logins, and would otherwise make every account look active. Blank = no interactive login found.
