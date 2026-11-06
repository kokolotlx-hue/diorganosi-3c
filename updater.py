"""Ενημερώσεις εφαρμογών από GitHub Releases — χωρίς δικό σου server.

Μπορεί να μπει **σε όλες τις εφαρμογές** όπως είναι: αρκεί δίπλα στο exe να
υπάρχει ένα `update.json`:

    {
      "app": "Διοργάνωση 3C",
      "repo": "gkologiannis/diorganosi-3c",
      "asset": "Διοργάνωση3C-v2.exe",
      "version": "2.0.0",
      "channel": "latest"
    }

Η ροή:
1. `check_for_update()` διαβάζει το τελευταίο release από το GitHub API.
2. Συγκρίνει την έκδοση (semver) με τη δική μας.
3. `download_update()` κατεβάζει το asset σε προσωρινό φάκελο και ελέγχει SHA-256.
4. `install_update()` γράφει ένα .cmd που περιμένει να κλείσει η εφαρμογή,
   αντικαθιστά το exe, το ξανανοίγει και σβήνεται.

Χωρίς δίκτυο/ρυθμίσεις δεν κάνει τίποτα και δεν πετάει σφάλματα.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

API = "https://api.github.com/repos/{repo}/releases/{channel}"
TIMEOUT = 8
USER_AGENT = "koko-updater/1.0"


class UpdateError(Exception):
    pass


# --- ρυθμίσεις -----------------------------------------------------------
def config_path(app_dir=None):
    base = app_dir or os.path.dirname(os.path.abspath(
        sys.executable if getattr(sys, "frozen", False) else __file__))
    return os.path.join(base, "update.json")


def load_config(app_dir=None):
    path = config_path(app_dir)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def save_config(data, app_dir=None):
    path = config_path(app_dir)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return path


def parse_version(text):
    """«2.0.0» / «v2.1.3» / «2.0» → tuple για σύγκριση."""
    parts = []
    for chunk in str(text or "").strip().lstrip("vV").replace("-", ".").split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if digits == "":
            break
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts[:4])


def is_newer(remote, local):
    return parse_version(remote) > parse_version(local)


# --- έλεγχος -------------------------------------------------------------
def _get_json(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


def check_for_update(config=None):
    """Επιστρέφει dict με πληροφορίες ή None αν δεν υπάρχει νεότερη έκδοση."""
    config = config or load_config()
    repo = (config.get("repo") or "").strip()
    if not repo or "/" not in repo:
        raise UpdateError("Δεν έχει οριστεί αποθετήριο (repo) στις ρυθμίσεις.")
    channel = config.get("channel") or "latest"
    data = _get_json(API.format(repo=repo, channel=channel))
    tag = data.get("tag_name") or data.get("name") or ""
    remote_version = tag.lstrip("vV")
    local_version = config.get("version") or "0.0.0"
    asset_name = config.get("asset") or ""
    asset = None
    for item in data.get("assets") or []:
        if not asset_name or item.get("name") == asset_name:
            asset = item
            break
    digest = ""
    if asset:
        digest = (asset.get("digest") or "").replace("sha256:", "")
    return {
        "remote_version": remote_version,
        "local_version": local_version,
        "newer": is_newer(remote_version, local_version),
        "notes": data.get("body") or "",
        "published": data.get("published_at") or "",
        "page": data.get("html_url") or "",
        "asset_name": asset.get("name") if asset else "",
        "asset_url": asset.get("browser_download_url") if asset else "",
        "asset_size": asset.get("size") if asset else 0,
        "sha256": digest,
    }


# --- λήψη ----------------------------------------------------------------
def download_update(info, target_dir=None):
    url = info.get("asset_url")
    if not url:
        raise UpdateError("Το release δεν έχει αρχείο για κατέβασμα.")
    target_dir = target_dir or tempfile.mkdtemp(prefix="update_")
    filename = info.get("asset_name") or os.path.basename(url)
    path = os.path.join(target_dir, filename)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response, \
            open(path, "wb") as handle:
        while True:
            chunk = response.read(65536)
            if not chunk:
                break
            handle.write(chunk)
    sha = hashlib.sha256(open(path, "rb").read()).hexdigest()
    expected = (info.get("sha256") or "").lower()
    if expected and sha != expected:
        raise UpdateError("Το αρχείο δεν ταιριάζει με το SHA-256 του release.")
    info["local_file"] = path
    info["sha256_local"] = sha
    return path


# --- εγκατάσταση ---------------------------------------------------------
def install_update(new_file, config=None):
    """Αντικαθιστά το τρέχον exe με το νέο, μέσω .cmd που περιμένει να κλείσουμε."""
    if not getattr(sys, "frozen", False):
        raise UpdateError("Η αυτόματη εγκατάσταση δουλεύει μόνο από το .exe.")
    current = os.path.abspath(sys.executable)
    config = config or load_config()
    script = os.path.join(tempfile.gettempdir(), "install_update.cmd")
    lines = [
        "@echo off",
        "chcp 65001 >nul",
        "timeout /t 3 /nobreak >nul",
        f':wait',
        f'tasklist /fi "imagename eq {os.path.basename(current)}" | find /i '
        f'"{os.path.basename(current)}" >nul && (timeout /t 1 /nobreak >nul & goto wait)',
        f'copy /y "{new_file}" "{current}" >nul',
        f'start "" "{current}"',
        "del \"%~f0\"",
    ]
    with open(script, "w", encoding="utf-8", newline="\r\n") as handle:
        handle.write("\n".join(lines) + "\n")
    subprocess.Popen(["cmd", "/c", script], close_fds=True,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))
    return script


def update_config_version(new_version, config=None, app_dir=None):
    config = dict(config or load_config(app_dir))
    config["version"] = str(new_version).lstrip("vV")
    save_config(config, app_dir)
    return config
