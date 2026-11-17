#!/usr/bin/env python3
"""
fiabox migration enumerator (metadata-only, read-only).

Reads superAdmin creds from .env, reads a list of user emails
from emails.txt (one per line), and for each account:
  - mints a token via authBySuperAdmin
  - walks cloud folders recursively -> file count + total bytes
  - pulls shares and public links
Writes three CSVs into out/. Does NOT download file contents.

Usage:
  python3 -I enumerate.py            # full run over emails.txt
  python3 -I enumerate.py --probe a@b.com   # single account, print to stdout
"""
import os, sys, json, csv, time, urllib.request, urllib.parse, urllib.error

BASE = None
LOGIN = None
PASSWORD = None

def load_env():
    global BASE, LOGIN, PASSWORD
    env = {}
    if os.path.exists(".env"):  # read ONLY .env (never .env.example, which is a template)
        for line in open(".env"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    BASE = env.get("FIABOX_BASE", "https://fiabox.fia.com").rstrip("/")
    LOGIN = env.get("FIABOX_LOGIN")
    PASSWORD = env.get("FIABOX_PASSWORD")
    if not (LOGIN and PASSWORD):
        sys.exit("Missing FIABOX_LOGIN / FIABOX_PASSWORD in .env "
                 "(copy .env.example to .env and fill in your values)")

def ms_to_utc(ms):
    """Epoch milliseconds -> 'YYYY-MM-DD HH:MM:SS' UTC; '' for missing/zero."""
    try:
        ms = int(ms)
    except (TypeError, ValueError):
        return ""
    if not ms:
        return ""
    import datetime
    return datetime.datetime.fromtimestamp(
        ms / 1000, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def req(method, path, token=None, body=None, timeout=30):
    url = BASE + path
    data = None
    headers = {"Accept": "application/json",
               "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/156.0.0.0 Safari/537.36"}
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["token"] = token
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            ct = resp.headers.get("Content-Type", "")
            if "json" in ct:
                return resp.status, json.loads(raw) if raw else None
            return resp.status, raw.decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")

def super_token(email):
    """Mint a token for `email` using superAdmin creds."""
    status, body = req("POST", "/ws/api/accounts/authBySuperAdmin/" + urllib.parse.quote(email),
                        body={"login": LOGIN, "password": PASSWORD})
    if status == 200 and isinstance(body, dict) and body.get("token"):
        return body["token"], body
    return None, body

def list_folder(token, id_dc, subpath):
    """List one folder. Returns list of FsItem-like dicts, or []"""
    p = "/ws/api/folders/%d/%s" % (id_dc, urllib.parse.quote(subpath, safe=""))
    status, body = req("GET", p, token=token)
    if status == 200 and isinstance(body, list):
        return body
    if status == 200 and isinstance(body, dict):
        # some deployments wrap children under a key
        for k in ("children", "items", "content", "navigationFsItemList"):
            if isinstance(body.get(k), list):
                return body[k]
    return []

def walk_account(token):
    """Walk every cloud folder. Returns (files_list, folders_list, error).

    Each file row: dict(cloud_folder_id, cloud_folder_name, path, size, lastModified).
    Each folder row: dict(cloud_folder_id, cloud_folder_name, path, is_root,
    lastModified). The cloud-folder root is emitted as a folder with path '.'
    (matching the share convention) so empty cloud folders are still captured.
    Folder listing items use keys: type ('File' or a directory type), path
    (full sub-path from the cloud-folder root), size, lastModified, idCloudFolder.
    """
    status, cfs = req("GET", "/ws/api/cloudFolders", token=token)
    files, folders = [], []
    if status != 200 or not isinstance(cfs, list):
        return files, folders, "cloudFolders status=%s" % status
    for cf in cfs:
        id_dc = cf.get("id")
        cf_name = cf.get("name")
        if id_dc is None:
            continue
        # Emit the cloud-folder root itself so empty cloud folders appear and
        # root-level shares (path '.') have something to join to.
        folders.append({
            "cloud_folder_id": id_dc, "cloud_folder_name": cf_name,
            "path": ".", "is_root": True,
            "lastModified": cf.get("lastModified"),
        })
        stack = [""]
        while stack:
            sub = stack.pop()
            for it in list_folder(token, id_dc, sub):
                is_file = it.get("type") == "File"
                path = it.get("path", "")
                if is_file:
                    files.append({
                        "cloud_folder_id": id_dc,
                        "cloud_folder_name": cf_name,
                        "path": path,
                        "size": it.get("size") or 0,
                        "lastModified": it.get("lastModified"),
                    })
                else:
                    folders.append({
                        "cloud_folder_id": id_dc,
                        "cloud_folder_name": cf_name,
                        "path": path, "is_root": False,
                        "lastModified": it.get("lastModified"),
                    })
                    stack.append(path)  # path is already the full sub-path
    return files, folders, ""

def main():
    load_env()
    os.makedirs("exports", exist_ok=True)

    if len(sys.argv) >= 3 and sys.argv[1] == "--probe":
        email = sys.argv[2]
        tok, info = super_token(email)
        if not tok:
            print("NO TOKEN for", email, "->", str(info)[:200]); return
        files, fo, err = walk_account(tok)
        print(json.dumps({"email": email, "id": info.get("id"),
                          "files": len(files), "bytes": sum(f["size"] for f in files),
                          "folders": len(fo), "err": err}, indent=2))
        for f in files[:40]:
            print("  %10d  %s/%s" % (f["size"], f["cloud_folder_name"], f["path"]))
        return

    emails = [l.strip() for l in open("emails.txt") if l.strip() and not l.startswith("#")] \
        if os.path.exists("emails.txt") else []
    if not emails:
        sys.exit("Create emails.txt (one email per line) first.")

    _summ_f = open("exports/accounts_summary.csv", "w", newline="")
    summ = csv.writer(_summ_f)
    summ.writerow(["email", "account_id", "files", "size_MB", "size_GB", "folders",
                   "newest_file_utc", "error"])
    flw = csv.writer(open("exports/files.csv", "w", newline=""))
    flw.writerow(["owner_email", "account_id", "cloud_folder_id", "cloud_folder_name",
                  "path", "size", "lastModified_utc"])
    fdw = csv.writer(open("exports/folders.csv", "w", newline=""))
    fdw.writerow(["owner_email", "account_id", "cloud_folder_id", "cloud_folder_name",
                  "folder_path", "is_root", "lastModified_utc"])
    _shw_f = open("exports/shares.csv", "w", newline="")
    shw = csv.writer(_shw_f)
    # One row per share edge: grantor_email owns the folder, grantee_email has access.
    # direction = 'owned'  -> discovered from the owner's account (recipients inlined)
    #           = 'received' -> discovered from the recipient's account (owner inlined)
    shw.writerow(["grantor_email", "grantee_email", "shareType", "active",
                  "share_id", "idCloudFolder", "folder_name", "path",
                  "direction", "queried_account"])
    lkw = csv.writer(open("exports/public_links.csv", "w", newline=""))
    lkw.writerow(["owner_email", "owner_account_id", "link_id", "name", "url", "path",
                  "idCloudFolder", "validity_days", "uploadAllowed", "hasPassword", "created_utc"])

    pw_links = 0  # count of password-protected public links (value never stored)
    for i, email in enumerate(emails, 1):
        tok, info = super_token(email)
        if not tok:
            summ.writerow([email, "", "", "", "", "", "", "NO_TOKEN: " + str(info)[:120]])
            print("[%d/%d] %-35s no account/token" % (i, len(emails), email))
            continue
        acct_id = info.get("id") if isinstance(info, dict) else ""
        files, fo, err = walk_account(tok)
        nbytes = sum(f["size"] for f in files)
        newest = max((ms_to_utc(f["lastModified"]) for f in files), default="")
        summ.writerow([email, acct_id, len(files), round(nbytes / 1e6, 1),
                       round(nbytes / 1e9, 2), len(fo), newest, err])
        for f in files:
            flw.writerow([email, acct_id, f["cloud_folder_id"], f["cloud_folder_name"],
                          f["path"], f["size"], ms_to_utc(f["lastModified"])])
        for d in fo:
            fdw.writerow([email, acct_id, d["cloud_folder_id"], d["cloud_folder_name"],
                          d["path"], d["is_root"], ms_to_utc(d["lastModified"])])

        st, shares = req("GET", "/ws/api/shares", token=tok)
        if st == 200 and isinstance(shares, list):
            for s in shares:
                owner = (s.get("owner") or {}).get("login")
                sid, idc = s.get("id"), s.get("idCloudFolder")
                fname, path = s.get("displayedName"), s.get("path")
                recips = s.get("sharesByAccount") or []
                if s.get("mine") and recips:
                    # This account owns the share; each recipient is a grantee.
                    for a in recips:
                        acct = a.get("account") or {}
                        grantee = a.get("email") or acct.get("login")
                        stype = a.get("shareType") or a.get("typePartage") or s.get("shareType")
                        shw.writerow([owner or email, grantee, stype, a.get("active"),
                                      sid, idc, fname, path, "owned", email])
                elif s.get("mine"):
                    # Owns it, but recipients not inlined (e.g. working-group or public-link share).
                    shw.writerow([owner or email, "", s.get("shareType"), "",
                                  sid, idc, fname, path, "owned", email])
                else:
                    # Shared TO this account: owner -> this account, at shareType.
                    shw.writerow([owner, email, s.get("shareType"), s.get("active"),
                                  sid, idc, fname, path, "received", email])
        st, links = req("GET", "/ws/api/publicLinks", token=tok)
        if st == 200 and isinstance(links, list):
            for l in links:
                has_pw = bool(l.get("password"))
                if has_pw:
                    pw_links += 1
                lkw.writerow([email, acct_id, l.get("id"), l.get("name"), l.get("url"),
                              l.get("path"), l.get("idCloudFolder"), l.get("validity"),
                              l.get("uploadAllowed"), has_pw, ms_to_utc(l.get("creationDate"))])
        print("[%d/%d] %-35s files=%d bytes=%d folders=%d %s"
              % (i, len(emails), email, len(files), sum(f["size"] for f in files), len(fo), err))
        time.sleep(0.1)  # be gentle on the server

    _summ_f.close()
    _shw_f.close()
    add_shared_with_me()   # final pass: mark accounts that have folders shared with them

    print("\nDone. CSVs in out/: accounts_summary.csv, files.csv, folders.csv, "
          "shares.csv, public_links.csv")
    if pw_links:
        print("NOTE: %d public link(s) are password-protected (flagged hasPassword=True; "
              "the password values are NOT stored in the CSVs)." % pw_links)


def add_shared_with_me():
    """Rewrite accounts_summary.csv with a 'shared_with_me' column (yes/no):
    an account has folders shared with it if it appears as a grantee in shares.csv."""
    grantees = set()
    for r in csv.DictReader(open("exports/shares.csv", newline="")):
        g = (r.get("grantee_email") or "").strip().lower()
        if g:
            grantees.add(g)
    rows = list(csv.reader(open("exports/accounts_summary.csv", newline="")))
    hdr = rows[0]
    if "shared_with_me" in hdr:
        return
    ei = hdr.index("error")
    hdr.insert(ei, "shared_with_me")
    out = [hdr]
    for r in rows[1:]:
        r.insert(ei, "yes" if r[0].strip().lower() in grantees else "no")
        out.append(r)
    csv.writer(open("exports/accounts_summary.csv", "w", newline="")).writerows(out)

if __name__ == "__main__":
    main()
