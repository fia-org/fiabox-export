#!/usr/bin/env python3
"""
fiabox bulk file downloader -- resumable, retrying, portable (stdlib only).

Downloads file CONTENTS via superAdmin impersonation, preserving structure:
  <dest>/<email>/<cloud_folder_name>/<path>

Resume:  re-running skips any file already on disk whose size matches the API.
Retry:   transient failures (520/5xx, network drops) are retried with backoff.
Skip:    non-ASCII-named files are skipped (app can't serve them; see
         exports/non_ascii_files.csv) and logged, not retried.

Usage:
  python3 -I download.py <email>              # one account
  python3 -I download.py --all                # every account in emails.txt
  python3 -I download.py <email> --list       # list files, download nothing
  python3 -I download.py <email> --match STR  # only paths containing STR
  python3 -I download.py <email> --fix-nonascii   # recover the parked non-ASCII files
Options:
  --dest DIR      output root (default: downloads)
  --retries N     attempts per file (default: 4)
  --include-nonascii   attempt non-ASCII names too (will likely 520)
  --fix-nonascii  for non-ASCII files: try as-is/normalized first (catches nbsp &
                  Unicode-normalization false positives); for the genuinely broken
                  (520) ones, rename->download->rename back (non-destructive).
  --keep-renamed  with --fix-nonascii: leave files renamed to ASCII (don't revert).
"""
import sys, os, time, csv, json, unicodedata, urllib.parse, urllib.request, urllib.error

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import enumerate as E


def has_nonascii(s):
    return any(ord(c) > 127 for c in s)


def with_retry(fn, retries, label):
    """Call fn(); retry on 5xx/network errors with exponential backoff.
    Returns (result, None) or (None, error_string)."""
    delay = 2.0
    last = None
    for attempt in range(1, retries + 1):
        try:
            return fn(), None
        except urllib.error.HTTPError as e:
            last = "HTTP %s" % e.code
            if e.code < 500:            # 4xx won't fix on retry
                return None, last
        except Exception as e:
            last = type(e).__name__ + ": " + str(e)[:60]
        if attempt < retries:
            time.sleep(delay)
            delay = min(delay * 2, 30)
    return None, last + " (after %d attempts)" % retries


def download_one(token, id_dc, path, dest):
    url = E.BASE + "/ws/api/files/%d/%s" % (id_dc, urllib.parse.quote(path, safe=""))
    req = urllib.request.Request(url, headers={"token": token, "User-Agent": UA})
    tmp = dest + ".part"
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with urllib.request.urlopen(req, timeout=300) as resp, open(tmp, "wb") as fh:
        n = 0
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            fh.write(chunk); n += len(chunk)
    os.replace(tmp, dest)       # atomic: a .part left behind means it was interrupted
    return n


# ---------------------------------------------------------------------------
# non-ASCII recovery: the download endpoint returns 520 for accented/special
# chars (an app-side serving bug), but the metadata/rename endpoint
# (PUT /ws/api/files) accepts the same path fine. So: try the file as-is and
# with normalized paths first (that alone recovers the non-breaking-space /
# Unicode-normalization false positives); only if the file is GENUINELY broken
# do we round-trip rename -> download -> rename back, leaving the name untouched.
# ---------------------------------------------------------------------------
_TMP = [0]  # run-unique temp counter


def _variants(path):
    """Candidate paths to try before any rename (no mutation)."""
    out = [path]
    for form in ("NFC", "NFD"):
        n = unicodedata.normalize(form, path)
        if n not in out:
            out.append(n)
    sp = path.replace("\xa0", " ")   # non-breaking space -> normal space
    if sp not in out:
        out.append(sp)
    return out


def _asciify(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))   # drop accents
    s = s.replace("\xa0", " ")
    s = "".join(c if 32 <= ord(c) < 127 else "_" for c in s)
    return s.strip() or "file"


def _parent_has_nonascii(path):
    parent = path.rpartition("/")[0]
    return has_nonascii(parent)


def _download_once(token, cf, path, dest):
    try:
        return download_one(token, cf, path, dest), None
    except urllib.error.HTTPError as e:
        return None, "HTTP %s" % e.code
    except Exception as e:
        return None, type(e).__name__


def _put_rename(token, cf, src, dst, attempts=3):
    """Rename file `src` -> `dst` within cloud folder `cf`. Returns HTTP status,
    or None on network error/timeout (which the server may still have executed,
    so callers verify by downloading/listing rather than trusting this)."""
    url = E.BASE + "/ws/api/files/%d/%s" % (cf, urllib.parse.quote(src, safe=""))
    data = json.dumps({"idCloudFolder": cf, "path": dst, "type": "File"}).encode()
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, method="PUT",
                                     headers={"token": token, "User-Agent": UA,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:
            if i < attempts - 1:
                time.sleep(2)
    return None


def _exists(token, cf, path):
    """Is `path` present in cloud folder `cf` right now?"""
    parent = path.rpartition("/")[0]
    try:
        items = E.list_folder(token, cf, parent) or []
    except Exception:
        return None
    return any(it.get("path") == path for it in items)


def _recover_one(token, cf, path, dest, retries, keep_renamed):
    """Get one non-ASCII file. Returns (status_label, bytes).
    status: ok_direct | ok_normalized | recovered_rename | recovered_rename_KEPT
            | needs_folder_rename | FAIL:<reason> | FAIL_rename:<code>"""
    # 1) try as-is + normalized forms (no mutation) -- catches nbsp / NFC-NFD
    for i, cand in enumerate(_variants(path)):
        n, err = _download_once(token, cf, cand, dest)
        if err is None:
            return ("ok_direct" if i == 0 else "ok_normalized"), n
    # 2) genuinely broken. Can only rename the leaf; a non-ASCII PARENT folder
    #    would need a folder rename (more disruptive) -- flag, don't touch.
    if _parent_has_nonascii(path):
        return "needs_folder_rename", 0
    parent, _, leaf = path.rpartition("/")
    _TMP[0] += 1
    tmp_leaf = "_fiatmp%d_%s" % (_TMP[0], _asciify(leaf))
    tmp = (parent + "/" + tmp_leaf) if parent else tmp_leaf
    # The rename PUT can return 404/timeout yet still execute server-side, so we
    # don't trust its status -- we verify by downloading the temp name.
    st = _put_rename(token, cf, path, tmp)
    n, derr = with_retry(lambda: download_one(token, cf, tmp, dest), retries, path)
    if derr:
        # temp not reachable -> rename likely didn't happen; make sure we didn't
        # strand a temp file, then report failure.
        _put_rename(token, cf, tmp, path)
        return "FAIL:%s(rename_status=%s)" % (derr, st), 0
    if keep_renamed:
        return "recovered_rename_KEPT", n
    rb = _put_rename(token, cf, tmp, path)          # rename back to original
    if rb not in (200, 204):                        # status flaky -> verify by listing
        back = _exists(token, cf, path)
        rb = 204 if back else (rb if rb is not None else "timeout")
    return ("recovered_rename" if rb in (200, 204) else "recovered_rename_NOREVERT:%s" % rb), n


def process_account_fix(email, dest_root, retries, logw, keep_renamed):
    """Recover the non-ASCII (parked) files for one account."""
    tok, info = with_retry(lambda: _token(email), retries, "token")
    if tok is None:
        print("  %-35s NO TOKEN (%s)" % (email, info)); return (0, 0, 0, 0)
    files, folders, err = E.walk_account(tok)
    na = [f for f in files if has_nonascii(f["path"])]
    base = os.path.join(dest_root, email.replace("/", "_"))
    direct = renamed = failed = folder = 0
    got = 0
    for f in na:
        path = f["path"]
        dest = os.path.join(base, f["cloud_folder_name"].replace("/", "_"), path)
        if os.path.exists(dest) and os.path.getsize(dest) == f["size"]:
            logw.writerow([email, f["cloud_folder_name"], path, f["size"], "skip_exists", 0]); continue
        label, n = _recover_one(tok, f["cloud_folder_id"], path, dest, retries, keep_renamed)
        logw.writerow([email, f["cloud_folder_name"], path, f["size"], label, n])
        if label.startswith("ok_"):
            direct += 1; got += n
        elif label.startswith("recovered_rename"):
            renamed += 1; got += n
        elif label == "needs_folder_rename":
            folder += 1
        else:
            failed += 1
            print("    FAIL %s/%s -> %s" % (f["cloud_folder_name"], path[:45], label))
    print("  %-35s non-ascii=%d  got_directly=%d  recovered_by_rename=%d  folder_blocked=%d  fail=%d  (%.1f MB)"
          % (email, len(na), direct, renamed, folder, failed, got / 1e6))
    return (direct, renamed, failed, folder)


def process_account(email, dest_root, retries, match, list_only, include_nonascii, logw):
    tok, info = with_retry(lambda: _token(email), retries, "token")
    if tok is None:
        print("  %-35s NO TOKEN (%s)" % (email, info)); return (0, 0, 0, 0)
    files, folders, err = E.walk_account(tok)
    if match:
        files = [f for f in files if match in f["path"]]
    if list_only:
        for f in files:
            flag = " [non-ascii]" if has_nonascii(f["path"]) else ""
            print("  %12d  %s/%s%s" % (f["size"], f["cloud_folder_name"], f["path"], flag))
        return (0, 0, 0, 0)

    base = os.path.join(dest_root, email.replace("/", "_"))
    ok = skipped = failed = parked = 0
    got = 0
    for f in files:
        path = f["path"]
        if has_nonascii(path) and not include_nonascii:
            parked += 1
            logw.writerow([email, f["cloud_folder_name"], path, f["size"], "parked_nonascii", 0])
            continue
        dest = os.path.join(base, f["cloud_folder_name"].replace("/", "_"), path)
        if os.path.exists(dest) and os.path.getsize(dest) == f["size"]:
            skipped += 1
            continue
        n, derr = with_retry(lambda: download_one(tok, f["cloud_folder_id"], path, dest),
                             retries, path)
        if derr:
            failed += 1
            logw.writerow([email, f["cloud_folder_name"], path, f["size"], "FAIL:" + derr, 0])
            print("    FAIL %s/%s -> %s" % (f["cloud_folder_name"], path[:50], derr))
        else:
            ok += 1; got += n
            logw.writerow([email, f["cloud_folder_name"], path, f["size"], "ok", n])
    print("  %-35s ok=%d skip=%d fail=%d parked=%d  (%.1f MB)"
          % (email, ok, skipped, failed, parked, got / 1e6))
    return (ok, skipped, failed, parked)


def _token(email):
    tok, info = E.super_token(email)
    if not tok:
        raise RuntimeError("no token: %s" % str(info)[:80])
    return tok


def main():
    E.load_env()
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    opt = lambda name, default=None: (args[args.index(name) + 1] if name in args else default)
    dest_root = opt("--dest", "downloads")
    retries = int(opt("--retries", "4"))
    match = opt("--match")
    list_only = "--list" in args
    include_nonascii = "--include-nonascii" in args
    all_mode = "--all" in args
    fix_mode = "--fix-nonascii" in args
    keep_renamed = "--keep-renamed" in args

    if all_mode:
        emails = [l.strip() for l in open("emails.txt") if l.strip() and not l.startswith("#")]
    else:
        emails = [a for a in args if "@" in a]
    if not emails:
        sys.exit("Give an email, or --all with emails.txt present.")

    os.makedirs(dest_root, exist_ok=True)
    logpath = os.path.join(dest_root, "_download_log.csv")
    new = not os.path.exists(logpath)
    logf = open(logpath, "a", newline="")
    logw = csv.writer(logf)
    if new:
        logw.writerow(["email", "cloud_folder", "path", "size", "status", "bytes"])

    t0 = time.time()
    tot = [0, 0, 0, 0]
    for i, email in enumerate(emails, 1):
        if len(emails) > 1:
            print("[%d/%d]" % (i, len(emails)), end=" ")
        if fix_mode:
            r = process_account_fix(email, dest_root, retries, logw, keep_renamed)
        else:
            r = process_account(email, dest_root, retries, match, list_only, include_nonascii, logw)
        logf.flush()
        for j in range(4):
            tot[j] += r[j]
    dt = time.time() - t0
    if fix_mode:
        print("\nTOTAL got_directly=%d recovered_by_rename=%d failed=%d folder_blocked=%d in %.0fs"
              % (tot[0], tot[1], tot[2], tot[3], dt))
        print("  got_directly  = false positives (nbsp / normalization) -- were never really broken")
        print("  recovered_by_rename = genuinely broken (520), fixed via rename round-trip")
        print("Log: %s" % logpath)
    elif not list_only:
        print("\nTOTAL ok=%d skipped=%d failed=%d parked_nonascii=%d in %.0fs"
              % (tot[0], tot[1], tot[2], tot[3], dt))
        print("Log: %s   (re-run to resume; matched files are skipped)" % logpath)
        if tot[2]:
            print("NOTE: %d file(s) failed after retries -- re-run to retry just those." % tot[2])


if __name__ == "__main__":
    main()
