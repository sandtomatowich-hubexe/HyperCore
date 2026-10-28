#!/usr/bin/env python3
"""Post finished builds to Telegram.

Each kernel zip becomes one rich message (Bot API 10.3): a table with the kernel details, a table with the
feature switches, link buttons for the root solution and the CI run, and the zip itself as a document block.
If Telegram rejects the rich message, the same information is sent as a normal document with an HTML caption.

Environment (set by the workflow):
  BOT_TOKEN, CHAT_ID           required; TOPIC_ID optional (forum topics only)
  BUILD_RESULT                 result of the build matrix: success / failure / cancelled
  RELEASE_URL, RUN_URL, COMMIT_URL, RUN_NUMBER
  ARTIFACT_DIR                 default: release-artifacts (zips plus the <zip>.meta.json files written by build.sh)

Bot uploads are limited to 50 MB per file; bigger zips are skipped with a note.
The token is passed to curl through stdin so it never shows up in a process list.
"""
import glob
import html
import json
import os
import re
import subprocess
import sys
import time

LIMIT = 49 * 1024 * 1024
E = os.environ.get
TOKEN, CHAT, TOPIC = E("BOT_TOKEN", ""), E("CHAT_ID", ""), E("TOPIC_ID", "")
ART = E("ARTIFACT_DIR", "release-artifacts")
ZIP = re.compile(r"^([^-]+)-(.+)-(v\d{2}\.\d{2}\.\d{2}(?:-r\d+)?)-(\d+\.\d+\.\d+)\.zip$")


def call(method, fields, upload=None):
    """POST to the Bot API. Returns (ok, description)."""
    cmd = ["curl", "-sS", "--max-time", "900", "-K", "-"]
    for k, v in fields.items():
        cmd += ["--form-string", f"{k}={v}"]
    if upload:
        cmd += ["-F", f"{upload[0]}=@{upload[1]}"]
    cfg = f'url = "https://api.telegram.org/bot{TOKEN}/{method}"\n'
    out = subprocess.run(cmd, input=cfg, capture_output=True, text=True)
    try:
        res = json.loads(out.stdout)
    except ValueError:
        return False, f"no JSON reply ({out.stderr.strip()[:200]})"
    return bool(res.get("ok")), res.get("description", "")


def common(extra=None):
    f = {"chat_id": CHAT}
    if TOPIC:
        f["message_thread_id"] = TOPIC
    f.update(extra or {})
    return f


def load_meta(zip_path):
    base = os.path.basename(zip_path)
    meta_path = zip_path[:-4] + ".meta.json"
    if os.path.exists(meta_path):
        with open(meta_path) as fh:
            return json.load(fh)
    m = ZIP.match(base)
    return {"zip": base, "variant": m.group(2) if m else base, "linux": m.group(4) if m else "",
            "root": {}, "features": {}}


# ---- rich message pieces -------------------------------------------------------------------------------
def cell(text):
    return {"text": text, "align": "left", "valign": "middle"}


def chip(label, url=None):
    """A button shown inline in a table cell: a link chip, or a greyed-out value chip."""
    b = {"text": label}
    if url:
        b["url"] = url
    else:
        b["disabled"] = {}
    return {"type": "button", "button": b}


def onoff(v):
    return chip("Enable" if v else "Disable")


def toolchain(raw):
    t = (raw or "").strip()
    t = re.sub(r"git\b", "", t).strip()
    return re.sub(r"(?i)clang", "Clang", t) or "Unknown"


def lto_label(raw):
    return {"thinLTO": "ThinLTO", "fullLTO": "FullLTO", "noneLTO": "NoLTO"}.get(raw, raw or "Unknown")


def root_label(meta):
    r = meta.get("root") or {}
    if not r.get("name"):
        return None, None
    ver = r.get("version") or ""
    cnt = r.get("count") or ""
    label = r["name"] + (f" {ver}" if ver else "") + (f" ({cnt})" if cnt else "")
    if meta.get("susfs"):
        label += f" \u00b7 SuSFS {meta['susfs']}"
    return label, f"https://github.com/{r['repo']}" if r.get("repo") else None


def source_urls(repo, branch):
    """Links for the kernel source: GitHub owner/repo, or a full URL such as android.googlesource.com."""
    if repo.startswith("http"):
        base = repo.rstrip("/")
        return base, f"{base}/+/refs/heads/{branch}"
    return f"https://github.com/{repo}", f"https://github.com/{repo}/tree/{branch}"


def build_blocks(meta, zip_path, attach):
    ft = meta.get("features") or {}
    repo, branch = meta.get("source_repo", ""), meta.get("source_branch", "")
    t1 = [
        [cell("Kernel"), cell(f"Linux {meta.get('linux', '')}")],
    ]
    if repo:
        repo_url, branch_url = source_urls(repo, branch)
        t1.append([cell("Source"), cell(chip(repo.rstrip("/").split("/")[-1], repo_url))])
        if branch:
            t1.append([cell("Branch"), cell(chip(branch, branch_url))])
    t1.append([cell("Toolchain"), cell(toolchain(meta.get("compiler")))])
    t1.append([cell("LTO"), cell(chip(lto_label(meta.get("lto"))))])

    t2 = [
        [cell("Mountless Engine"), cell(chip("NoMount" if ft.get("nomount") else "None"))],
        [cell("Droidspaces"), cell(onoff(ft.get("droidspaces")))],
        [cell("Re:Kernel"), cell(onoff(ft.get("rekernel")))],
        [cell("NTSync"), cell(onoff(True))],
        [cell("Baseband Guard"), cell(onoff(True))],
        [cell("LZ4+ZSTD"), cell(onoff(ft.get("zstd")))],
        [cell("LZ4KD"), cell(onoff(ft.get("lz4kd")))],
        [cell("MGLRU"), cell(onoff(True))],
    ]
    blocks = [
        {"type": "table", "cells": t1, "is_bordered": True, "is_compact": True},
        {"type": "table", "cells": t2, "is_bordered": True, "is_compact": True},
    ]
    label, url = root_label(meta)
    if label and url:
        blocks.append({"type": "buttons", "buttons": [{"text": label, "url": url}], "align": "center"})
    links = []
    if E("RELEASE_URL"):
        links.append({"text": "Release", "url": E("RELEASE_URL")})
    if E("RUN_URL"):
        links.append({"text": f"CI #{E('RUN_NUMBER', '')}".strip(), "url": E("RUN_URL")})
    if E("COMMIT_URL"):
        links.append({"text": "Commit", "url": E("COMMIT_URL")})
    if links:
        blocks.append({"type": "buttons", "buttons": links, "align": "center"})
    blocks.append({"type": "document", "document": {"type": "document", "media": f"attach://{attach}"}})
    return blocks


def send_rich(meta, zip_path, silent):
    blocks = build_blocks(meta, zip_path, "kernelzip")
    fields = common({"rich_message": json.dumps({"blocks": blocks, "skip_entity_detection": True}),
                     "disable_notification": "true" if silent else "false"})
    return call("sendRichMessage", fields, ("kernelzip", zip_path))


def fallback_caption(meta):
    ft = meta.get("features") or {}
    onoff_s = lambda v: "on" if v else "off"
    lines = [f"<b>{html.escape(meta.get('variant', ''))}</b>",
             f"Kernel: Linux {html.escape(meta.get('linux', ''))}",
             f"Toolchain: {html.escape(toolchain(meta.get('compiler')))}",
             f"LTO: {html.escape(lto_label(meta.get('lto')))}"]
    label, url = root_label(meta)
    if label:
        lines.append(f'Root: <a href="{html.escape(url or "")}">{html.escape(label)}</a>' if url else f"Root: {html.escape(label)}")
    lines.append("Droidspaces {} | Re:Kernel {} | NoMount {} | LZ4+ZSTD {} | LZ4KD {}".format(
        onoff_s(ft.get("droidspaces")), onoff_s(ft.get("rekernel")), onoff_s(ft.get("nomount")),
        onoff_s(ft.get("zstd")), onoff_s(ft.get("lz4kd"))))
    links = []
    for name, key in (("Release", "RELEASE_URL"), ("CI", "RUN_URL"), ("Commit", "COMMIT_URL")):
        if E(key):
            links.append(f'<a href="{html.escape(E(key))}">{name}</a>')
    if links:
        lines.append(" | ".join(links))
    return "\n".join(lines)[:1000]


def send_fallback(meta, zip_path, silent):
    fields = common({"parse_mode": "HTML", "caption": fallback_caption(meta),
                     "disable_notification": "true" if silent else "false"})
    return call("sendDocument", fields, ("document", zip_path))


def main():
    if not TOKEN or not CHAT:
        print("::error::BOT_TOKEN and CHAT_ID secrets are required")
        return 1

    zips = sorted(glob.glob(os.path.join(ART, "*.zip")))
    result = E("BUILD_RESULT", "success")
    failed = False
    rich_ok = True

    for i, z in enumerate(zips):
        name = os.path.basename(z)
        if os.path.getsize(z) > LIMIT:
            print(f"::warning::{name} is over the 50 MB bot limit; not uploaded")
            continue
        if i:
            time.sleep(2)  # stay under the per-chat rate limit
        meta = load_meta(z)
        silent = i > 0
        if rich_ok:
            ok, desc = send_rich(meta, z, silent)
            if not ok:
                print(f"::warning::sendRichMessage failed ({desc}); falling back to plain documents")
                rich_ok = False
        if not rich_ok:
            ok, desc = send_fallback(meta, z, silent)
        if not ok:
            print(f"::error::Telegram upload of {name} failed: {desc}")
            failed = True

    if not zips or result != "success":
        text = "<b>No kernels were produced.</b>" if not zips else \
               f"<b>Build finished with problems</b> ({html.escape(result)}). Only the successful kernels were posted."
        if E("RUN_URL"):
            text += f'\n<a href="{html.escape(E("RUN_URL"))}">CI #{html.escape(E("RUN_NUMBER", ""))}</a>'
        ok, desc = call("sendMessage", common({"text": text, "parse_mode": "HTML"}))
        failed = failed or not ok
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
