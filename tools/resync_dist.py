"""Re-sync the app CODE into an already-built dist/Cheddarbowl and re-zip it -- WITHOUT rebuilding the
embeddable Python runtime. Uses build_dist's constants + a fresh secret-scan gate so the app's runtime/
and secrets/ trees (and any stray credentials) can never leak into the artifact.

    python tools/resync_dist.py

Requires a prior `python tools/build_dist.py` (needs OUT/python/ present). Safe to run repeatedly.
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import zipfile

import build_dist as B          # same dir (tools/)

ROOT = B.ROOT
OUT = B.OUT                      # dist/Cheddarbowl
ZIP = os.path.join(ROOT, "dist", "Cheddarbowl.zip")

# Never allow these into the zip: credentials, per-user session, runtime state, logs, profiles.
SECRET = re.compile(
    r"(^|/)(secrets|runtime)/|espn_auth|host_token|nfl_cookies|active_league|draft_state|"
    r"ws_capture\.jsonl|scraper_status|\.lock$|\.log$|pick_overrides\.json|manual_draft\.json|"
    r"browser_profile|edge_cdp|pw_profile|cookies",
    re.I,
)


def _rel(path: str) -> str:
    return os.path.relpath(path, os.path.dirname(OUT)).replace("\\", "/")   # Cheddarbowl/...


def resync_code() -> None:
    """Overwrite shipped code with current source (python/ interpreter untouched)."""
    for sub in ("cheddarbowl", "apps"):
        dst = os.path.join(OUT, sub)
        if os.path.isdir(dst):
            shutil.rmtree(dst)
        shutil.copytree(os.path.join(ROOT, sub), dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.rmtree(os.path.join(OUT, "cheddarbowl", "ingest", "legacy"), ignore_errors=True)
    for f in ("launch.py", "requirements-app.txt"):
        shutil.copy2(os.path.join(ROOT, f), os.path.join(OUT, f))
    print("synced: cheddarbowl/ + apps/ + launch.py + requirements-app.txt")


def _members() -> list[tuple[str, str]]:
    """(abs_path, arcname) allowlist. Everything under OUT except runtime/, secrets/, __pycache__,
    *.pyc, and anything matching SECRET. data/ keeps only reference + projections."""
    members: list[tuple[str, str]] = []
    for base, dirs, files in os.walk(OUT):
        dirs[:] = [d for d in dirs if d != "__pycache__" and d not in ("runtime", "secrets")]
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            ap = os.path.join(base, fn)
            arc = _rel(ap)
            if SECRET.search(arc):
                continue
            members.append((ap, arc))
    return members


def build_zip() -> None:
    members = _members()
    leaks = [arc for _, arc in members if SECRET.search(arc)]
    if leaks:
        print("ABORT -- secret/runtime files would be shipped:", *leaks[:20], sep="\n  ")
        sys.exit(1)
    if os.path.exists(ZIP):
        os.remove(ZIP)
    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for ap, arc in members:
            z.write(ap, arc)
    print(f"wrote {ZIP}  ({os.path.getsize(ZIP)/1e6:.0f} MB, {len(members)} entries)")


def verify_zip() -> None:
    with zipfile.ZipFile(ZIP) as z:
        names = z.namelist()
        leaks = [n for n in names if SECRET.search(n)]
        assert not leaks, f"SECRET in zip: {leaks[:20]}"
        checks = {
            "Cheddarbowl/cheddarbowl/ingest/espn_live.py": ["ping_interval=20", "CB_FRESH_DRAFT"],
            "Cheddarbowl/cheddarbowl/ingest/runner.py": ["_lock_held"],
            "Cheddarbowl/apps/draft.py": ["from cheddarbowl import config"],
        }
        for name, needles in checks.items():
            txt = z.read(name).decode("utf-8", "replace")
            for nd in needles:
                assert nd in txt, f"{name} missing {nd!r} -- stale copy shipped"
            print(f"OK {name}")
    print("VERIFIED: no secrets; live-draft fixes present in shipped code.")


if __name__ == "__main__":
    resync_code()
    build_zip()
    verify_zip()
