#!/usr/bin/env python3
"""Build the static progress site for the Sports 3D recreation projects.

Scans <ROOT>/{BBall,Soccer,Volleyball,Realism} 3D/outputs/<clip>/, transcodes media into
web-friendly H.264 MP4 + JPG posters, copies the self-contained three.js viewers, and writes
docs/index.html (timeline by date), docs/examples/<id>.html (one page per clip x project) and
docs/data/outputs.json (manifest).

Re-runnable and incremental: every generated media file is keyed on its source path, mtime,
size and the encode settings (docs/data/build_cache.json); unchanged sources are skipped.
Source folders are only ever read. Stdlib only + the bundled ffmpeg binary.

    python3 build_site.py            # incremental build
    python3 build_site.py --force    # re-encode everything
    python3 build_site.py --dry-run  # scan + report, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # /Users/alex/Desktop/Projects/Internal/Sports
DOCS = HERE / "docs"
FFMPEG = ROOT / "Realism 3D/.venv/lib/python3.11/site-packages/imageio_ffmpeg/binaries/ffmpeg-macos-aarch64-v7.1"

MAX_FILE = 50 * 1024 * 1024        # hard GitHub-friendly cap per file
VIDEO_TARGET = 45 * 1024 * 1024    # re-encode harder if a transcode exceeds this
VIDEO_W = 1280                     # "720p wide": max width, keep aspect
CRF = 26
POSTER_W = 640
QA_W = 1400
QA_MAX = 24                        # QA images per example
ENC_VERSION = 3                    # bump to invalidate the transcode cache

# key, folder, sport label, short label
PROJECTS = [
    ("basketball", "BBall 3D", "Basketball"),
    ("soccer", "Soccer 3D", "Soccer"),
    ("volleyball", "Volleyball 3D", "Volleyball"),
    ("realism", "Realism 3D", "Realism"),
]
SKIP_DIRS = {"dummy", "qa", "qa_vid"}  # plus anything starting with "_" / "." / "-"

VIDEO_EXT = {".mp4", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}

# suffix after "<clip>" -> (kind, label, order). Longest suffix wins.
KINDS = {
    "": ("original", "Original clip", 0),
    "_compare": ("compare", "Side-by-side: source vs 3D", 10),
    "_real_compare": ("compare", "Side-by-side: source vs realism", 10),
    "_3d_broadcast": ("broadcast", "3D overlaid on the broadcast", 20),
    "_real_broadcast": ("broadcast", "Realism, broadcast camera", 20),
    "_3d": ("3d", "3D recreation", 30),
    "_real_orbit": ("orbit", "Realism, orbit camera", 35),
    "_tactical": ("tactical", "Tactical top-down", 40),
    "_browser_tour": ("tour", "Interactive viewer tour", 50),
    "_twitter": ("showcase", "Featured cut: broadcast → 3D → replay", 15),
    "_showcase": ("showcase", "Showcase", 60),
    "_reel": ("reel", "Reel", 61),
    "_reel30": ("reel", "Reel (30 fps)", 62),
    "_reel60": ("reel", "Reel (60 fps)", 63),
    "_angles": ("angles", "Multi-angle", 64),
    "_shootercam": ("shootercam", "Shooter cam", 65),
    "_viewer": ("viewer", "Interactive 3D viewer", 5),
    "_real_viewer": ("viewer", "Interactive realism viewer", 5),
}
THUMB_PREF = ["broadcast", "3d", "orbit", "compare", "showcase", "original"]

SOURCE_URLS = {
    "mex_per": {
        "channel": "Selección Nacional de México",
        "title": "MÉXICO VS PERÚ | REVIVE TODA LA ACCIÓN | PARTIDO COMPLETO",
        "url": "https://www.youtube.com/watch?v=ITOlViqq34k",
        "licence": "Creative Commons Attribution (as labelled on YouTube)",
    },
    "avc_rally": {
        "channel": "Asian Volleyball Confederation",
        "title": "JPN VS IND I F 3-4 I 2ND ASIAN MEN'S U16",
        "url": "https://www.youtube.com/watch?v=mKjf1Or4wlI",
        "licence": "Creative Commons Attribution (as labelled on YouTube)",
    },
}
NBA_CLIPS = {"Warriors1", "Warriors2", "Steph", "StephLuca"}
NBA_NOTE = ("Source footage is NBA broadcast video; all rights belong to the NBA and its broadcast "
            "partners. Shown here only as reference for a non-commercial research demo.")


def log(*a):
    print(*a, flush=True)


def esc(s) -> str:
    return html.escape(str(s), quote=True)


def fmt_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def local_dt(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts)


def fmt_dt(ts: float) -> str:
    d = local_dt(ts)
    return d.strftime("%b %-d, %Y %H:%M")


# --------------------------------------------------------------------------- scanning

def classify(clip: str, path: Path, clip_dir: Path):
    """Return (kind, label, order, copy) for one file in a clip folder."""
    rel = path.relative_to(clip_dir)
    name, ext = path.stem, path.suffix.lower()
    if rel.parts[0] == "assets":
        return ("asset", "Viewer bake asset (embedded in viewer, not copied)", 99, False)
    if rel.parts[0] == "qa":
        if len(rel.parts) == 2 and ext in IMAGE_EXT:
            return ("qa", "QA sheet", 90, True)
        return ("qa_debug", "QA debug file", 99, False)
    if ext == ".pkl":
        return ("play_data", "Pickled PlayData", 99, False)
    if ext == ".json":
        return ("data", "QC / metrics JSON", 98, False)
    if ext == ".md":
        return ("note", "Note", 98, False)
    if name.endswith("_poster") and ext in IMAGE_EXT:
        return ("poster_src", "Source poster", 97, False)
    if name == clip or name.startswith(clip + "_"):
        suffix = name[len(clip):]
        if suffix in KINDS:
            kind, label, order = KINDS[suffix]
            if kind == "viewer" and ext != ".html":
                return None
            if kind != "viewer" and ext not in VIDEO_EXT:
                return None
            return (kind, label, order, True)
        if ext in VIDEO_EXT:  # unknown new render type: still publish it
            pretty = suffix.strip("_").replace("_", " ").title()
            return ("other", pretty, 70, True)
        if ext == ".html":
            return ("viewer", "Interactive viewer (" + suffix.strip("_") + ")", 6, True)
    return None


def find_original(proj_dir: Path, clip: str, clip_dir: Path):
    for d in (clip_dir, proj_dir / "inputs"):
        for ext in (".mp4", ".MOV", ".mov", ".m4v"):
            p = d / (clip + ext)
            if p.is_file():
                return p
    return None


def scan():
    examples = {}
    for key, folder, sport in PROJECTS:
        out = ROOT / folder / "outputs"
        if not out.is_dir():
            continue
        for clip_dir in sorted(out.iterdir()):
            clip = clip_dir.name
            if not clip_dir.is_dir() or clip[0] in "_.-" or clip in SKIP_DIRS:
                continue
            files = []
            for p in sorted(clip_dir.rglob("*")):
                if not p.is_file() or p.name.startswith("."):
                    continue
                c = classify(clip, p, clip_dir)
                if c is None:
                    continue
                files.append((p, c))
            publishable = [f for f in files if f[1][3] and f[1][0] not in ("original", "qa")]
            if not publishable:
                continue  # empty / smoke / synthetic folders
            if not any(c[0] == "original" for _, c in files) and key != "realism":
                orig = find_original(ROOT / folder, clip, clip_dir)
                if orig:
                    files.append((orig, KINDS[""] + (True,)))
            ex_id = f"{key}-{clip}".lower()
            examples[ex_id] = {
                "id": ex_id, "project": folder, "project_key": key, "sport": sport, "clip": clip,
                "files": files,
            }
    # realism: find base project + original clip
    for ex in examples.values():
        if ex["project_key"] != "realism":
            ex["base_sport"] = ex["sport"]
            continue
        base = next((e for e in examples.values() if e["project_key"] != "realism" and e["clip"] == ex["clip"]), None)
        ex["base_id"] = base["id"] if base else None
        ex["base_sport"] = base["sport"] if base else "Unknown"
        if base and not any(c[0] == "original" for _, c in ex["files"]):
            orig = next(((p, c) for p, c in base["files"] if c[0] == "original"), None)
            if orig:
                ex["files"].append(orig)
    return examples


# --------------------------------------------------------------------------- media

class Cache:
    def __init__(self, path: Path):
        self.path = path
        try:
            self.data = json.loads(path.read_text())
        except Exception:
            self.data = {}

    @staticmethod
    def key_of(src: Path, params: str):
        st = src.stat()
        return {"src": str(src), "mtime": st.st_mtime, "size": st.st_size, "params": params, "v": ENC_VERSION}

    def fresh(self, dest: Path, src: Path, params: str) -> bool:
        rec = self.data.get(str(dest.relative_to(DOCS)))
        if not rec or not dest.exists():
            return False
        k = self.key_of(src, params)
        return all(rec.get(x) == k[x] for x in k)

    def put(self, dest: Path, src: Path, params: str, **extra):
        rec = self.key_of(src, params)
        rec.update(extra)
        self.data[str(dest.relative_to(DOCS))] = rec

    def get(self, dest: Path):
        return self.data.get(str(dest.relative_to(DOCS)), {})

    def save(self, keep: set):
        if keep is not None:
            self.data = {k: v for k, v in self.data.items() if k in keep}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.replace(self.path)


def probe(src: Path):
    r = subprocess.run([str(FFMPEG), "-hide_banner", "-i", str(src)], capture_output=True, text=True)
    err = r.stderr
    dur = 0.0
    m = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    if m:
        dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    has_audio = bool(re.search(r"Stream #\S+.*Audio: (aac|mp3|opus|ac3|pcm)", err))
    m = re.search(r"Video: .*?, (\d{2,5})x(\d{2,5})", err)
    w, h = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
    return dur, has_audio, w, h


def run_ff(args):
    r = subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y"] + args,
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-800:])


def transcode(src: Path, dest: Path, keep_audio: bool, cache: Cache, force: bool):
    params = f"w{VIDEO_W}-crf{CRF}-a{int(keep_audio)}"
    if not force and cache.fresh(dest, src, params):
        return cache.get(dest)
    dur, has_audio, w, h = probe(src)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part.mp4")
    crf = CRF
    while True:
        args = ["-i", str(src), "-map", "0:v:0"]
        if keep_audio and has_audio:
            args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "96k", "-ac", "2"]
        else:
            args += ["-an"]
        args += ["-vf", f"scale='min({VIDEO_W},iw)':-2:flags=lanczos,format=yuv420p",
                 "-c:v", "libx264", "-preset", "slow", "-crf", str(crf), "-profile:v", "high",
                 "-movflags", "+faststart", "-map_metadata", "-1", "-sn", "-dn", str(tmp)]
        log(f"    encode {src.name} -> {dest.relative_to(DOCS)} (crf {crf})")
        run_ff(args)
        if tmp.stat().st_size <= VIDEO_TARGET or crf >= 40:
            break
        crf += 4
    if tmp.stat().st_size > MAX_FILE:
        tmp.unlink()
        raise RuntimeError(f"{src} still > 50 MB after re-encode")
    tmp.replace(dest)
    _, _, ow, oh = probe(dest)
    cache.put(dest, src, params, duration=round(dur, 2), width=ow, height=oh,
              audio=bool(keep_audio and has_audio), crf=crf)
    return cache.get(dest)


def make_poster(src: Path, dest: Path, cache: Cache, force: bool, duration: float):
    params = f"poster{POSTER_W}"
    if not force and cache.fresh(dest, src, params):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part.jpg")
    t = min(1.5, duration / 3) if duration else 0
    for ss in (t, 0):
        try:
            run_ff(["-ss", f"{ss:.2f}", "-i", str(src), "-frames:v", "1",
                    "-vf", f"scale='min({POSTER_W},iw)':-2:flags=lanczos", "-q:v", "4", str(tmp)])
            if tmp.exists() and tmp.stat().st_size > 0:
                break
        except RuntimeError:
            continue
    tmp.replace(dest)
    cache.put(dest, src, params)


def make_image(src: Path, dest: Path, cache: Cache, force: bool):
    params = f"img{QA_W}"
    if not force and cache.fresh(dest, src, params):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.stem + ".part.jpg")
    run_ff(["-i", str(src), "-frames:v", "1", "-vf", f"scale='min({QA_W},iw)':-2:flags=lanczos",
            "-q:v", "5", str(tmp)])
    tmp.replace(dest)
    cache.put(dest, src, params)


def copy_file(src: Path, dest: Path, cache: Cache, force: bool):
    params = "copy"
    if not force and cache.fresh(dest, src, params):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    shutil.copy2(src, tmp)
    tmp.replace(dest)
    cache.put(dest, src, params)


VIEWER_REF_RE = re.compile(r"""(?:src|href)\s*=\s*["']([^"'#]+)["']|fetch\(\s*["'`]([^"'`]+)["'`]|import\s*\(\s*["'`]([^"'`]+)""")


def viewer_local_refs(path: Path):
    """Relative files a viewer would request (excluding data:, http(s):, blob:)."""
    text = path.read_text(errors="ignore")
    refs = set()
    for m in VIEWER_REF_RE.finditer(text):
        ref = next(g for g in m.groups() if g)
        if re.match(r"^(data:|https?:|blob:|mailto:|javascript:|//)", ref) or "${" in ref:
            continue
        refs.add(ref.split("?")[0])
    cdn = sorted(set(re.findall(r"https?://[A-Za-z0-9.\-]+/[^\s\"'`)]*", text[:20000])))
    return sorted(refs), cdn


# --------------------------------------------------------------------------- build

def build(force=False, dry=False):
    if not FFMPEG.exists():
        sys.exit(f"ffmpeg not found: {FFMPEG}")
    examples = scan()
    log(f"found {len(examples)} examples: {', '.join(sorted(examples))}")
    if dry:
        for ex in examples.values():
            log(f"  {ex['id']}: " + ", ".join(f"{p.name}[{c[0]}]" for p, c in ex["files"] if c[3]))
        return

    DOCS.mkdir(parents=True, exist_ok=True)
    cache = Cache(DOCS / "data" / "build_cache.json")
    old_manifest = {}
    try:
        old_manifest = json.loads((DOCS / "data" / "outputs.json").read_text())
    except Exception:
        pass
    old_posted = {e["id"]: e.get("posted") for e in old_manifest.get("examples", [])}
    old_posted.update({o["id"]: o.get("posted") for o in old_manifest.get("outputs", [])})
    try:  # optional hand-maintained overrides: {"<example or output id>": {...}}
        old_posted.update(json.loads((HERE / "posted.json").read_text()))
    except Exception:
        pass

    produced = set()      # docs-relative paths written this run
    warnings = []
    manifest_ex, manifest_out = [], []

    for ex_id in sorted(examples, key=lambda i: ([p[0] for p in PROJECTS].index(examples[i]["project_key"]), i)):
        ex = examples[ex_id]
        log(f"[{ex_id}]")
        mdir = DOCS / "media" / ex_id
        outs = []
        qa_count = 0
        for src, (kind, label, order, copy) in sorted(ex["files"], key=lambda f: (f[1][2], f[0].name)):
            st = src.stat()
            rel_src = str(src.relative_to(ROOT))
            out_id = f"{ex_id}/{src.relative_to(src.parents[1] if 'qa' in src.parts[-2:-1] else src.parent)}"
            o = {
                "id": out_id.lower(), "example": ex_id, "project": ex["project"], "clip": ex["clip"],
                "file": src.name, "source_path": rel_src, "kind": kind, "label": label,
                "date": local_dt(st.st_mtime).strftime("%Y-%m-%d"),
                "mtime": local_dt(st.st_mtime).isoformat(timespec="seconds"),
                "size": st.st_size, "site_path": None, "site_size": None, "poster": None,
                "posted": old_posted.get(out_id.lower()), "_order": order, "_ts": st.st_mtime,
            }
            try:
                if not copy:
                    pass
                elif kind == "qa":
                    if qa_count >= QA_MAX:
                        o["note"] = "not published (QA image cap)"
                    else:
                        qa_count += 1
                        dest = mdir / "qa" / (src.stem + ".jpg")
                        make_image(src, dest, cache, force)
                        o["site_path"] = str(dest.relative_to(DOCS))
                elif kind == "viewer":
                    if st.st_size > MAX_FILE:
                        o["note"] = f"viewer is {fmt_size(st.st_size)} (> 50 MB), not copied"
                        warnings.append(f"{rel_src}: {o['note']}")
                    else:
                        dest = DOCS / "viewers" / ex_id / src.name
                        copy_file(src, dest, cache, force)
                        o["site_path"] = str(dest.relative_to(DOCS))
                        refs, cdn = viewer_local_refs(src)
                        o["viewer_local_refs"] = refs
                        o["viewer_cdn"] = cdn
                        missing = [r for r in refs if not (src.parent / r).exists()]
                        for r in refs:
                            if (src.parent / r).exists():
                                copy_file(src.parent / r, dest.parent / r, cache, force)
                                produced.add(str((dest.parent / r).relative_to(DOCS)))
                        if missing:
                            warnings.append(f"{rel_src}: missing referenced files {missing}")
                        if st.st_size > 25 * 1024 * 1024:
                            warnings.append(f"{rel_src}: large viewer ({fmt_size(st.st_size)}, data embedded as base64)")
                else:  # video
                    vdir = mdir
                    if kind == "original" and ex.get("base_id"):  # reuse the base project's transcode
                        vdir = DOCS / "media" / ex["base_id"]
                        o["shared_with"] = ex["base_id"]
                    dest = vdir / (src.stem + ".mp4")
                    info = transcode(src, dest, keep_audio=(kind == "original"), cache=cache, force=force)
                    poster = vdir / (src.stem + ".jpg")
                    make_poster(dest, poster, cache, force, info.get("duration", 0))
                    o["site_path"] = str(dest.relative_to(DOCS))
                    o["poster"] = str(poster.relative_to(DOCS))
                    o["duration"] = info.get("duration")
                    o["width"], o["height"] = info.get("width"), info.get("height")
                    produced.add(o["poster"])
            except Exception as e:  # keep going; one broken file must not kill the scheduled run
                warnings.append(f"{rel_src}: {e}")
                o["note"] = f"build error: {e}"
                o["site_path"] = None
            if o["site_path"]:
                produced.add(o["site_path"])
                o["site_size"] = (DOCS / o["site_path"]).stat().st_size
            outs.append(o)

        clip = ex["clip"]
        attribution = SOURCE_URLS.get(clip)
        copyright_note = NBA_NOTE if (clip in NBA_CLIPS and ex["base_sport"] == "Basketball") else None
        related = []
        if ex["project_key"] == "realism" and ex.get("base_id"):
            related.append(ex["base_id"])
        for other in examples.values():
            if other["project_key"] == "realism" and other.get("base_id") == ex_id:
                related.append(other["id"])
        pub = [o for o in outs if o["site_path"] and o["kind"] not in ("qa",)]
        thumb = None
        for k in THUMB_PREF:
            cand = [o for o in pub if o["kind"] == k and o["poster"]]
            if cand:
                thumb = cand[0]["poster"]
                break
        dated = [o for o in outs if o["kind"] not in ("original", "asset", "play_data", "data", "note", "qa_debug", "poster_src")]
        title_sport = "Realism" if ex["project_key"] == "realism" else ex["sport"]
        manifest_ex.append({
            "id": ex_id, "title": f"{title_sport} – {clip}", "project": ex["project"],
            "sport": title_sport, "base_sport": ex["base_sport"], "clip": clip,
            "page": f"examples/{ex_id}.html", "thumbnail": thumb, "related": related,
            "first_output": min((o["mtime"] for o in dated), default=None),
            "last_output": max((o["mtime"] for o in dated), default=None),
            "attribution": attribution, "copyright_note": copyright_note,
            "posted": old_posted.get(ex_id),
        })
        manifest_out.extend(outs)
        cache.save(None)  # checkpoint so an interrupted run keeps finished transcodes

    # ---------------------------------------------------------------- pages
    ex_by_id = {e["id"]: e for e in manifest_ex}
    outs_by_ex = {}
    for o in manifest_out:
        outs_by_ex.setdefault(o["example"], []).append(o)

    write_static_assets()
    produced.update({"assets/style.css", "assets/site.js", ".nojekyll"})
    for e in manifest_ex:
        page = DOCS / e["page"]
        page.parent.mkdir(parents=True, exist_ok=True)
        page.write_text(render_example(e, outs_by_ex[e["id"]], ex_by_id))
        produced.add(e["page"])
    (DOCS / "index.html").write_text(render_index(manifest_ex, manifest_out))
    produced.add("index.html")

    # ---------------------------------------------------------------- manifest + cleanup
    def clean(o):
        return {k: v for k, v in o.items() if not k.startswith("_")}
    manifest = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "root": str(ROOT),
        "posted_schema": "null, or {\"platform\": \"x\", \"url\": \"...\", \"date\": \"YYYY-MM-DD\"}; "
                         "kept across rebuilds (also settable in site/posted.json by id)",
        "examples": manifest_ex,
        "outputs": [clean(o) for o in sorted(manifest_out, key=lambda o: (o["example"], o["_order"], o["file"]))],
        "warnings": warnings,
    }
    mpath = DOCS / "data" / "outputs.json"
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(manifest, indent=1, ensure_ascii=False))
    produced.update({"data/outputs.json", "data/build_cache.json"})
    cache.save(produced)

    removed = 0
    for sub in ("media", "viewers", "examples"):
        base = DOCS / sub
        if not base.exists():
            continue
        for p in sorted(base.rglob("*"), reverse=True):
            rel = str(p.relative_to(DOCS))
            if p.is_file() and rel not in produced:
                p.unlink()
                removed += 1
            elif p.is_dir() and not any(p.iterdir()):
                p.rmdir()

    total = sum(p.stat().st_size for p in DOCS.rglob("*") if p.is_file())
    largest = max((p for p in DOCS.rglob("*") if p.is_file()), key=lambda p: p.stat().st_size)
    log(f"\ndone: {len(manifest_ex)} example pages, {sum(1 for o in manifest_out if o['site_path'])} published files, "
        f"{removed} stale files removed")
    log(f"docs/ total {fmt_size(total)}; largest {largest.relative_to(DOCS)} ({fmt_size(largest.stat().st_size)})")
    for w in warnings:
        log("  warning:", w)


# --------------------------------------------------------------------------- HTML

SPORT_CLASS = {"Basketball": "bb", "Soccer": "sc", "Volleyball": "vb", "Realism": "re"}


def head(title, prefix, desc=""):
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<meta name="description" content="{esc(desc or title)}">
<meta name="color-scheme" content="dark">
<link rel="stylesheet" href="{prefix}assets/style.css">
</head>
<body>
"""


def posted_badge(p):
    if not p:
        return ""
    if isinstance(p, dict):
        url, plat = p.get("url"), p.get("platform", "X")
    else:
        url, plat = (p if str(p).startswith("http") else None), "X"
    label = f"Posted on {esc(plat if plat.lower() != 'x' else 'X')}"
    if url:
        return f'<a class="badge posted" href="{esc(url)}" target="_blank" rel="noopener">{label}</a>'
    return f'<span class="badge posted">{label}</span>'


def credit_block(e):
    parts = []
    a = e.get("attribution")
    if a:
        parts.append(
            f'<div class="credit"><strong>Source footage:</strong> <a href="{esc(a["url"])}" target="_blank" rel="noopener">'
            f'{esc(a["channel"])} on YouTube</a> — “{esc(a["title"])}”. {esc(a["licence"])}. '
            f'The 3D recreations are derived from this clip; credit to the channel.</div>')
    if e.get("copyright_note"):
        parts.append(f'<div class="credit warn">{esc(e["copyright_note"])}</div>')
    return "".join(parts)


def video_block(o, prefix, big=False):
    dur = f" · {o['duration']:.0f}s" if o.get("duration") else ""
    res = f" · {o['width']}×{o['height']}" if o.get("width") else ""
    return f"""<figure class="vid{' big' if big else ''}">
  <video controls playsinline preload="none" {'muted loop ' if o['kind'] != 'original' else ''}poster="{prefix}{esc(o['poster'])}" src="{prefix}{esc(o['site_path'])}"></video>
  <figcaption><span class="lbl">{esc(o['label'])}</span>{posted_badge(o.get('posted'))}
    <span class="meta">Made {esc(fmt_dt(o['_ts']))}{dur}{res} · {esc(fmt_size(o['site_size']))} · <code>{esc(o['file'])}</code></span></figcaption>
</figure>"""


def render_example(e, outs, ex_by_id):
    p = "../"
    sc = SPORT_CLASS.get(e["sport"], "")
    pub = [o for o in outs if o["site_path"]]
    orig = [o for o in pub if o["kind"] == "original"]
    viewers = [o for o in pub if o["kind"] == "viewer"]
    compares = [o for o in pub if o["kind"] == "compare"]
    others = [o for o in pub if o["kind"] not in ("original", "viewer", "compare", "qa")]
    qa = [o for o in pub if o["kind"] == "qa"]
    unpublished = [o for o in outs if not o["site_path"]]

    rel_links = "".join(
        f'<a class="chip {SPORT_CLASS.get(ex_by_id[r]["sport"], "")}" href="{esc(r)}.html">'
        f'{"See the footage-textured Realism version" if ex_by_id[r]["sport"] == "Realism" else "See the base " + esc(ex_by_id[r]["sport"]) + " recreation"} →</a>'
        for r in e["related"] if r in ex_by_id)
    first, last = e["first_output"], e["last_output"]
    span = ""
    if first:
        f = dt.datetime.fromisoformat(first).strftime("%b %-d")
        l = dt.datetime.fromisoformat(last).strftime("%b %-d, %Y")
        span = f"Outputs made {f} – {l}" if f != dt.datetime.fromisoformat(last).strftime("%b %-d") else f"Outputs made {l}"

    s = [head(f"{e['title']} · Sports 3D progress", p, f"3D recreation outputs for {e['clip']}")]
    s.append(f"""<header class="top"><a class="back" href="{p}index.html">← All progress</a></header>
<main class="wrap">
<section class="hero">
  <div class="kicker"><span class="tag {sc}">{esc(e['sport'])}</span>{'<span class="tag ghost">' + esc(e['base_sport']) + ' clip</span>' if e['sport'] == 'Realism' else ''}{posted_badge(e.get('posted'))}</div>
  <h1>{esc(e['title'])}</h1>
  <p class="sub">{esc(e['project'])} · clip <code>{esc(e['clip'])}</code> · {esc(span)}</p>
  {'<div class="related">' + rel_links + '</div>' if rel_links else ''}
  {credit_block(e)}
</section>""")

    if orig or compares:
        s.append('<section><h2>Source vs recreation</h2><div class="grid two">')
        for o in orig:
            s.append(video_block(o, p))
        for o in compares:
            s.append(video_block(o, p))
        s.append("</div></section>")

    for v in viewers:
        thumb = e.get("thumbnail")
        s.append(f"""<section><h2>{esc(v['label'])}</h2>
<div class="viewer" data-src="{p}{esc(v['site_path'])}">
  <div class="viewer-ph" style="{f'background-image:url({p}{esc(thumb)})' if thumb else ''}">
    <button class="load">Load interactive viewer <small>{esc(fmt_size(v['site_size']))} · three.js</small></button>
  </div>
</div>
<p class="meta">Made {esc(fmt_dt(v['_ts']))} · <a href="{p}{esc(v['site_path'])}" target="_blank" rel="noopener">Open full screen in a new tab ↗</a>
 · drag to orbit, scroll to zoom, space to play/pause</p>
</section>""")

    if others:
        s.append('<section><h2>Renders</h2><div class="grid">')
        for o in others:
            s.append(video_block(o, p))
        s.append("</div></section>")

    if qa:
        s.append(f'<section><details><summary>QA contact sheets ({len(qa)})</summary><div class="qa">')
        for o in qa:
            s.append(f'<a href="{p}{esc(o["site_path"])}" target="_blank"><img loading="lazy" src="{p}{esc(o["site_path"])}" alt="{esc(o["file"])}"><span>{esc(o["file"])}</span></a>')
        s.append("</div></details></section>")

    if unpublished:
        s.append(f'<section><details><summary>Other files in the output folder ({len(unpublished)}, not published)</summary><table class="files">')
        for o in unpublished:
            s.append(f'<tr><td><code>{esc(o["source_path"].split("/outputs/")[-1])}</code></td><td>{esc(o["label"])}</td>'
                     f'<td>{esc(fmt_size(o["size"]))}</td><td>{esc(fmt_dt(o["_ts"]))}</td></tr>')
        s.append("</table></details></section>")

    s.append(f'</main><footer class="foot">Generated {esc(dt.datetime.now().strftime("%b %-d, %Y %H:%M"))} · '
             f'<a href="{p}data/outputs.json">manifest</a></footer>\n<script src="{p}assets/site.js"></script>\n</body></html>\n')
    return "\n".join(s)


TIMELINE_SKIP = {"asset", "play_data", "data", "note", "qa_debug", "poster_src"}


def render_index(examples, outs):
    ex_by_id = {e["id"]: e for e in examples}
    # timeline: date -> example -> outputs made that day
    days = {}
    for o in outs:
        if o["kind"] in TIMELINE_SKIP or o.get("shared_with"):
            continue
        days.setdefault(o["date"], {}).setdefault(o["example"], []).append(o)

    n_renders = sum(1 for o in outs if o["site_path"] and o["kind"] not in ("original", "qa", "viewer"))
    n_viewers = sum(1 for o in outs if o["site_path"] and o["kind"] == "viewer")
    latest = max((o["mtime"] for o in outs if o["kind"] not in TIMELINE_SKIP | {"original"}), default="")
    sports = [s for s in ("Basketball", "Soccer", "Volleyball", "Realism") if any(e["sport"] == s for e in examples)]

    s = [head("Sports 3D · progress", "", "Timeline of 3D sports-play recreations from single broadcast clips")]
    s.append(f"""<main class="wrap">
<section class="hero home">
  <h1>Sports 3D <span class="dim">progress log</span></h1>
  <p class="sub">One broadcast camera in, a full 3D recreation of the play out: players rebuilt as meshes, court and ball fitted in 3D,
  camera recovered per frame. This page tracks every output by the day it was made.</p>
  <div class="stats">
    <div><b>{len(examples)}</b><span>examples</span></div>
    <div><b>{n_renders}</b><span>renders</span></div>
    <div><b>{n_viewers}</b><span>3D viewers</span></div>
    <div><b>{esc(dt.datetime.fromisoformat(latest).strftime('%b %-d') if latest else '–')}</b><span>latest output</span></div>
  </div>
  <div class="filters" role="group" aria-label="Filter by sport">
    <button class="f on" data-f="all">All</button>
    {''.join(f'<button class="f {SPORT_CLASS[x]}" data-f="{x}">{x}</button>' for x in sports)}
  </div>
</section>

<section><h2>Examples</h2><div class="cards">""")
    for e in sorted(examples, key=lambda e: e["last_output"] or "", reverse=True):
        thumb = f'<img loading="lazy" src="{esc(e["thumbnail"])}" alt="">' if e["thumbnail"] else '<div class="noimg"></div>'
        last = dt.datetime.fromisoformat(e["last_output"]).strftime("%b %-d") if e["last_output"] else ""
        s.append(f"""<a class="card" data-sport="{esc(e['sport'])}" href="{esc(e['page'])}">
  <div class="thumb">{thumb}<span class="tag {SPORT_CLASS.get(e['sport'], '')}">{esc(e['sport'])}</span>{posted_badge(e.get('posted'))}</div>
  <div class="body"><b>{esc(e['clip'])}</b><span>{esc(e['project'])} · updated {esc(last)}</span></div>
</a>""")
    s.append("</div></section>\n<section><h2>Timeline</h2><ol class=\"timeline\">")

    for day in sorted(days, reverse=True):
        d = dt.date.fromisoformat(day)
        s.append(f'<li class="day"><div class="date"><b>{d.strftime("%b %-d")}</b><span>{d.strftime("%A, %Y")}</span></div><div class="entries">')
        for ex_id, items in sorted(days[day].items(), key=lambda kv: -max(o["_ts"] for o in kv[1])):
            e = ex_by_id[ex_id]
            items.sort(key=lambda o: (o["_order"], o["file"]))
            vids = [o for o in items if o.get("poster")]
            pref = sorted(vids, key=lambda o: THUMB_PREF.index(o["kind"]) if o["kind"] in THUMB_PREF else 50)
            thumb = pref[0]["poster"] if pref else e["thumbnail"]
            only_source = all(o["kind"] == "original" for o in items)
            kinds = []
            for o in items:
                if o["kind"] == "qa":
                    continue
                kinds.append(f'<span class="pill">{esc(o["label"])}</span>')
            nqa = sum(1 for o in items if o["kind"] == "qa")
            if nqa:
                kinds.append(f'<span class="pill ghost">{nqa} QA sheet{"s" if nqa > 1 else ""}</span>')
            when = max(o["_ts"] for o in items)
            s.append(f"""<a class="entry" data-sport="{esc(e['sport'])}" href="{esc(e['page'])}">
  {'<img loading="lazy" src="' + esc(thumb) + '" alt="">' if thumb else '<div class="noimg"></div>'}
  <div><div class="et"><span class="tag {SPORT_CLASS.get(e['sport'], '')}">{esc(e['sport'])}</span><b>{esc(e['clip'])}</b>
  <span class="dim">{'source clip added' if only_source else str(len(items)) + ' output' + ('s' if len(items) > 1 else '')} · {esc(local_dt(when).strftime('%H:%M'))}</span></div>
  <div class="pills">{''.join(kinds)}</div></div>
</a>""")
        s.append("</div></li>")
    s.append(f"""</ol></section>
</main>
<footer class="foot">Generated {esc(dt.datetime.now().strftime("%b %-d, %Y %H:%M"))} · dates are file modification times ·
<a href="data/outputs.json">manifest (JSON)</a></footer>
<script src="assets/site.js"></script>
</body></html>
""")
    return "\n".join(s)


CSS = r"""
:root{--bg:#0b0d12;--bg2:#12151d;--card:#161a24;--line:#232838;--fg:#e8ebf2;--mut:#98a1b3;--acc:#ffb347;
--bb:#ff7a3d;--sc:#3ddc84;--vb:#4cc3ff;--re:#c58bff;--r:14px}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Roboto,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--acc);text-decoration:none}a:hover{text-decoration:underline}
code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mut)}
.wrap{max-width:1180px;margin:0 auto;padding:0 16px 48px}
.top{position:sticky;top:0;z-index:5;background:rgba(11,13,18,.85);backdrop-filter:blur(10px);border-bottom:1px solid var(--line);padding:10px 16px}
.back{color:var(--fg);font-weight:600}
.hero{padding:36px 0 12px}.hero.home{padding-top:48px}
h1{font-size:clamp(28px,5vw,44px);line-height:1.1;margin:8px 0 10px;letter-spacing:-.02em}
h2{font-size:18px;margin:36px 0 14px;letter-spacing:-.01em}
.dim{color:var(--mut);font-weight:400}.sub{color:var(--mut);max-width:760px;margin:0}
.kicker{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tag{display:inline-block;font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:3px 8px;border-radius:999px;background:#2a2f3d;color:var(--fg)}
.tag.bb{background:color-mix(in srgb,var(--bb) 22%,transparent);color:var(--bb)}
.tag.sc{background:color-mix(in srgb,var(--sc) 22%,transparent);color:var(--sc)}
.tag.vb{background:color-mix(in srgb,var(--vb) 22%,transparent);color:var(--vb)}
.tag.re{background:color-mix(in srgb,var(--re) 22%,transparent);color:var(--re)}
.tag.ghost{background:transparent;border:1px solid var(--line);color:var(--mut)}
.badge.posted{font-size:11px;font-weight:700;padding:3px 8px;border-radius:999px;background:#fff;color:#000}
.stats{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:22px 0 18px;max-width:640px}
.stats div{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:12px 14px}
.stats b{display:block;font-size:24px}.stats span{color:var(--mut);font-size:12px}
.filters{display:flex;gap:8px;flex-wrap:wrap}
.f{font:inherit;font-size:13px;font-weight:600;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:999px;padding:6px 14px;cursor:pointer}
.f.on{background:var(--fg);color:var(--bg);border-color:var(--fg)}
.f.bb.on{background:var(--bb);border-color:var(--bb)}.f.sc.on{background:var(--sc);border-color:var(--sc)}
.f.vb.on{background:var(--vb);border-color:var(--vb)}.f.re.on{background:var(--re);border-color:var(--re)}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden;color:var(--fg);transition:transform .15s,border-color .15s}
.card:hover{transform:translateY(-2px);border-color:#3a4155;text-decoration:none}
.thumb{position:relative;aspect-ratio:16/9;background:#000}.thumb img,.noimg{width:100%;height:100%;object-fit:cover;display:block}
.thumb .tag{position:absolute;left:8px;top:8px;backdrop-filter:blur(6px);background:rgba(0,0,0,.6)}
.thumb .badge{position:absolute;right:8px;top:8px}
.card .body{padding:10px 12px}.card .body b{display:block}.card .body span{color:var(--mut);font-size:12px}
.timeline{list-style:none;margin:0;padding:0}
.day{display:grid;grid-template-columns:120px 1fr;gap:18px;padding:16px 0;border-top:1px solid var(--line)}
.day .date b{display:block;font-size:20px}.day .date span{color:var(--mut);font-size:12px}
.entries{display:grid;gap:10px}
.entry{display:grid;grid-template-columns:160px 1fr;gap:14px;align-items:center;background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:8px;color:var(--fg)}
.entry:hover{border-color:#3a4155;text-decoration:none}
.entry img,.entry .noimg{width:160px;aspect-ratio:16/9;object-fit:cover;border-radius:9px;background:#000}
.et{display:flex;gap:8px;align-items:center;flex-wrap:wrap}.et .dim{font-size:12px}
.pills{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
.pill{font-size:12px;padding:2px 8px;border-radius:6px;background:#202534;color:#cdd3df}.pill.ghost{background:transparent;border:1px dashed var(--line);color:var(--mut)}
.hidden{display:none!important}.day.empty{display:none}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:16px}.grid.two{grid-template-columns:repeat(auto-fit,minmax(340px,1fr))}
.vid{margin:0;background:var(--card);border:1px solid var(--line);border-radius:var(--r);overflow:hidden}
.vid video{display:block;width:100%;background:#000;max-height:70vh}
.vid figcaption{padding:10px 12px;display:flex;flex-wrap:wrap;gap:4px 10px;align-items:center}
.vid .lbl{font-weight:600}.meta{color:var(--mut);font-size:12px;width:100%}.meta code{font-size:11px}
p.meta{margin:8px 0 0}
.viewer{position:relative;aspect-ratio:16/9;max-height:78vh;width:100%;border-radius:var(--r);overflow:hidden;border:1px solid var(--line);background:#000}
.viewer iframe{position:absolute;inset:0;width:100%;height:100%;border:0}
.viewer-ph{position:absolute;inset:0;background:#000 center/cover no-repeat;display:flex;align-items:center;justify-content:center}
.viewer-ph::before{content:"";position:absolute;inset:0;background:rgba(0,0,0,.45)}
.load{position:relative;font:inherit;font-weight:700;font-size:16px;color:#000;background:var(--acc);border:0;border-radius:999px;padding:12px 22px;cursor:pointer;display:flex;flex-direction:column;align-items:center}
.load small{font-weight:500;font-size:11px;opacity:.75}
.related{display:flex;gap:8px;flex-wrap:wrap;margin-top:14px}
.chip{border:1px solid var(--line);background:var(--card);border-radius:999px;padding:6px 12px;font-weight:600;font-size:13px}
.chip.re{color:var(--re)}.chip.bb{color:var(--bb)}.chip.sc{color:var(--sc)}.chip.vb{color:var(--vb)}
.credit{margin-top:14px;padding:10px 14px;border-radius:10px;background:#141a14;border:1px solid #284028;font-size:13px;color:#cfe3cf;max-width:860px}
.credit.warn{background:#1c1712;border-color:#43361f;color:#e6d5b8}
details{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:10px 14px;margin-top:16px}
summary{cursor:pointer;font-weight:600}
.qa{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px;margin-top:12px}
.qa a{display:block;color:var(--mut);font-size:11px}.qa img{width:100%;border-radius:8px;display:block;background:#000}
.files{width:100%;border-collapse:collapse;margin-top:10px;font-size:12px;color:var(--mut)}
.files td{padding:5px 6px;border-top:1px solid var(--line);vertical-align:top}
.foot{color:var(--mut);font-size:12px;text-align:center;padding:24px 16px 40px;border-top:1px solid var(--line)}
@media (max-width:700px){
 .stats{grid-template-columns:repeat(2,minmax(0,1fr))}
 .day{grid-template-columns:1fr;gap:8px}
 .entry{grid-template-columns:110px 1fr;gap:10px}.entry img,.entry .noimg{width:110px}
 .grid,.grid.two{grid-template-columns:1fr}
 .viewer{aspect-ratio:4/5}
}
"""

JS = r"""
(function(){
  var btns=document.querySelectorAll('.f');
  function apply(f){
    btns.forEach(function(b){b.classList.toggle('on',b.dataset.f===f)});
    document.querySelectorAll('[data-sport]').forEach(function(el){el.classList.toggle('hidden',f!=='all'&&el.dataset.sport!==f)});
    document.querySelectorAll('.day').forEach(function(d){d.classList.toggle('empty',!d.querySelector('.entry:not(.hidden)'))});
    try{ if(f==='all') history.replaceState(null,'',location.pathname); else history.replaceState(null,'','#'+encodeURIComponent(f)); }catch(e){}
  }
  btns.forEach(function(b){b.addEventListener('click',function(){apply(b.dataset.f)})});
  if(btns.length){var h=decodeURIComponent(location.hash.slice(1));apply([].some.call(btns,function(b){return b.dataset.f===h})?h:'all');}
  document.querySelectorAll('.viewer').forEach(function(v){
    var b=v.querySelector('.load'); if(!b) return;
    b.addEventListener('click',function(){
      var f=document.createElement('iframe'); f.src=v.dataset.src; f.allow='fullscreen'; f.setAttribute('allowfullscreen','');
      f.title='Interactive 3D viewer'; v.innerHTML=''; v.appendChild(f);
    });
  });
  // play muted renders when scrolled into view? keep it light: only on hover/tap via controls.
})();
"""


def write_static_assets():
    a = DOCS / "assets"
    a.mkdir(parents=True, exist_ok=True)
    for name, body in (("style.css", CSS), ("site.js", JS)):
        p = a / name
        if not p.exists() or p.read_text() != body:
            p.write_text(body)
    (DOCS / ".nojekyll").touch()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="re-encode / re-copy everything")
    ap.add_argument("--dry-run", action="store_true", help="scan and list, write nothing")
    args = ap.parse_args()
    DOCS.mkdir(parents=True, exist_ok=True)
    lock_path = HERE / ".build.lock"
    with open(lock_path, "w") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit("another build_site.py run is in progress; exiting")
        t0 = time.time()
        build(force=args.force, dry=args.dry_run)
        log(f"elapsed {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
