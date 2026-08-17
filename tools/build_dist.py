"""Build a portable, embedded-Python distribution of Cheddarbowl for Windows.

Produces  dist/Cheddarbowl/  -- a self-contained folder the league can unzip and run with NO Python
install. Ships its own Python (official embeddable) + all deps, and uses the member's installed
Edge/Chrome (no bundled browser). Auth/league/team are auto-detected per user on first run.

    python tools/build_dist.py

Steps: download embeddable Python -> put the app dir + site-packages on its path -> bootstrap pip ->
install requirements-app.txt -> copy the cheddarbowl package + apps + current-season reference data ->
write a windowless launcher. Network is required (downloads the embeddable + wheels).

Layout of the output:
    dist/Cheddarbowl/
      python/            embeddable interpreter + Lib/site-packages   (renamed from the old 'runtime/'
                         to avoid colliding with the app's own runtime/ state tree)
      cheddarbowl/       the package (ingest/legacy is NOT shipped)
      apps/              draft.py, draft_results.py, weekly.py, wrapup.py
      launch.py  requirements-app.txt  .streamlit/  Cheddarbowl.bat  README.txt
      data/seasons/<year>/{reference,projections}/   shared inputs the app reads
      runtime/  secrets/  are created lazily at first run (never shipped with content)
"""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # tools/ -> repo root
sys.path.insert(0, ROOT)
from cheddarbowl import config  # noqa: E402  -- for SEASON + season path helpers

PYVER = "%d.%d.%d" % sys.version_info[:3]
OUT = os.path.join(ROOT, "dist", "Cheddarbowl")
PYRT = os.path.join(OUT, "python")               # embeddable interpreter (was 'runtime')
EMBED_URL = f"https://www.python.org/ftp/python/{PYVER}/python-{PYVER}-embed-amd64.zip"
GETPIP_URL = "https://bootstrap.pypa.io/get-pip.py"

# Shared, non-sensitive reference data the running app needs (per current season).
REFERENCE_KEEP = ["player_crosswalk.csv", "crosswalk_manual.csv", "headshots.csv", "teams.csv", "adp.csv"]


def _sh(*args: str) -> None:
    print("   $", " ".join(os.path.basename(a) if os.path.sep in a else a for a in args), flush=True)
    subprocess.check_call(list(args))


def _fetch(url: str, dest: str) -> None:
    print(f"   -> {url}", flush=True)
    urllib.request.urlretrieve(url, dest)


def main() -> None:
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(PYRT)

    # 1) embeddable Python
    print(f"[1/6] embeddable Python {PYVER}", flush=True)
    zpath = os.path.join(OUT, "_embed.zip")
    _fetch(EMBED_URL, zpath)
    with zipfile.ZipFile(zpath) as z:
        z.extractall(PYRT)
    os.remove(zpath)

    # 2) put the app dir (..) + site-packages on the embeddable path, and enable site (for pip/.pth).
    #    '..' resolves to OUT, so `import cheddarbowl` (OUT/cheddarbowl) works from any app entry point.
    print("[2/6] configure interpreter path (._pth)", flush=True)
    pth = glob.glob(os.path.join(PYRT, "python*._pth"))[0]
    with open(pth, encoding="utf-8") as f:
        lines = [ln.rstrip("\n") for ln in f]
    keep = [ln for ln in lines if ln.strip() and not ln.strip().startswith("#")
            and ln.strip() != "import site"]
    for extra in ("..", "Lib\\site-packages"):
        if extra not in keep:
            keep.append(extra)
    keep.append("import site")
    with open(pth, "w", encoding="utf-8") as f:
        f.write("\n".join(keep) + "\n")

    py = os.path.join(PYRT, "python.exe")

    # 3) bootstrap pip
    print("[3/6] bootstrap pip", flush=True)
    gp = os.path.join(PYRT, "get-pip.py")
    _fetch(GETPIP_URL, gp)
    _sh(py, gp, "--no-warn-script-location")
    os.remove(gp)

    # 4) install runtime deps
    print("[4/6] install dependencies (this pulls ~200-300 MB of wheels)", flush=True)
    _sh(py, "-m", "pip", "install", "--no-warn-script-location",
        "-r", os.path.join(ROOT, "requirements-app.txt"))

    # 5) copy app code + shared data (no secrets, no runtime state, no dev-only bits)
    print("[5/6] copy package + apps + data", flush=True)
    shutil.copytree(os.path.join(ROOT, "cheddarbowl"), os.path.join(OUT, "cheddarbowl"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    # The retired NFL.com scrapers (ingest/legacy) need bs4/lxml, which aren't in requirements-app;
    # nothing at runtime imports them, so drop them from the shipped package.
    shutil.rmtree(os.path.join(OUT, "cheddarbowl", "ingest", "legacy"), ignore_errors=True)
    shutil.copytree(os.path.join(ROOT, "apps"), os.path.join(OUT, "apps"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for f in ("launch.py", "requirements-app.txt"):
        shutil.copy2(os.path.join(ROOT, f), os.path.join(OUT, f))
    if os.path.isdir(os.path.join(ROOT, ".streamlit")):
        shutil.copytree(os.path.join(ROOT, ".streamlit"), os.path.join(OUT, ".streamlit"),
                        ignore=shutil.ignore_patterns("*.toml.bak"))
    # current-season shared inputs -> data/seasons/<year>/{reference,projections}
    ref_out = os.path.join(OUT, "data", "seasons", str(config.SEASON), "reference")
    proj_out = os.path.join(OUT, "data", "seasons", str(config.SEASON), "projections")
    os.makedirs(ref_out); os.makedirs(proj_out)
    for fn in REFERENCE_KEEP:
        src = os.path.join(config.reference_dir(), fn)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(ref_out, fn))
    if os.path.exists(config.PROJECTIONS_CSV):
        shutil.copy2(config.PROJECTIONS_CSV, os.path.join(proj_out, "preseason.csv"))

    # 6) launcher (windowless) + readme
    print("[6/6] launcher + readme", flush=True)
    with open(os.path.join(OUT, "Cheddarbowl.bat"), "w", newline="\r\n", encoding="utf-8") as f:
        f.write('@echo off\r\ncd /d "%~dp0"\r\nstart "" "python\\pythonw.exe" launch.py\r\n')
    with open(os.path.join(OUT, "README.txt"), "w", newline="\r\n", encoding="utf-8") as f:
        f.write(
            "Cheddarbowl Draft Assistant\r\n===========================\r\n\r\n"
            "1. Double-click Cheddarbowl.bat.\r\n"
            "2. A browser window opens on ESPN Fantasy. Log in the first time (it's saved after).\r\n"
            "3. Open your draft. A second, chromeless window shows the assistant alongside it.\r\n"
            "4. Close the browser when the draft is done.\r\n\r\n"
            "Requirements: Windows + Microsoft Edge (pre-installed) or Google Chrome.\r\n"
            "Everything runs locally on your PC; your ESPN login stays in this folder.\r\n")

    size = sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(OUT) for f in fs)
    print(f"\nOK built {OUT}  ({size/1e6:.0f} MB)", flush=True)


if __name__ == "__main__":
    main()
