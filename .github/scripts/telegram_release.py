#!/usr/bin/env python3
"""Post a finished build to Telegram: one summary message, then every kernel zip as a document.

Environment (set by the workflow):
  BOT_TOKEN, CHAT_ID           required; TOPIC_ID optional (forum topics only)
  BUILD_RESULT                 result of the build matrix: success / failure / cancelled
  RELEASE_NAME, RELEASE_URL, RUN_URL, COMMIT_URL, RUN_NUMBER
  KERNEL_VERSION(S), ANDROID_RELEASE   optional
  ARTIFACT_DIR                 default: release-artifacts

The bot API limits uploads to 50 MB per file; bigger zips are listed but not uploaded.
The token is passed to curl through stdin so it never shows up in a process list.
"""
import glob
import hashlib
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
ZIP = re.compile(r"^([^-]+)-(.+)-(v\d{2}\.\d{2}\.\d{2}(?:-r\d+)?)-(\d+\.\d+)\.\d+\.zip$")


def api(method, fields, doc=None):
    cmd = ["curl", "-sS", "--max-time", "900", "-K", "-"]
    for k, v in fields.items():
        cmd += ["--form-string", f"{k}={v}"]
    if doc:
        cmd += ["-F", f"document=@{doc}"]
    cfg = f'url = "https://api.telegram.org/bot{TOKEN}/{method}"\n'
    out = subprocess.run(cmd, input=cfg, capture_output=True, text=True)
    try:
        res = json.loads(out.stdout)
    except ValueError:
        print(f"::error::Telegram {method}: no JSON reply ({out.stderr.strip()[:200]})")
        return False
    if not res.get("ok"):
        print(f"::error::Telegram {method} failed: {res.get('description', res)}")
        return False
    return True


def base(extra=None):
    f = {"chat_id": CHAT, "parse_mode": "HTML"}
    if TOPIC:
        f["message_thread_id"] = TOPIC
    f.update(extra or {})
    return f


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    if not TOKEN or not CHAT:
        print("::error::BOT_TOKEN and CHAT_ID secrets are required")
        return 1

    zips = sorted(glob.glob(os.path.join(ART, "*.zip")))
    result = E("BUILD_RESULT", "success")
    name = E("RELEASE_NAME") or f"CI #{E('RUN_NUMBER', '?')}"
    lines = [f"<b>{html.escape(name)}</b>"]

    if not zips:
        lines += ["", "❌ <b>No kernels were produced.</b>"]
    elif result == "success":
        lines += ["", "✅ <b>Build finished</b>"]
    else:
        lines += ["", f"⚠️ <b>Build finished with problems</b> ({html.escape(result)}); only the successful kernels are attached"]

    kv = E("KERNEL_VERSIONS") or E("KERNEL_VERSION")
    if kv:
        android = f"Android {E('ANDROID_RELEASE')} · " if E("ANDROID_RELEASE") and "," not in kv else ""
        lines += ["", f"<b>Kernel:</b> {android}Linux {html.escape(kv)}"]

    if zips:
        lines += ["", "<b>Builds</b>"]
        for z in zips:
            m = ZIP.match(os.path.basename(z))
            label = m.group(2) if m else os.path.basename(z)
            size = os.path.getsize(z) / 1048576
            note = "" if os.path.getsize(z) <= LIMIT else " (too big for the bot, use the release)"
            lines.append(f"• {html.escape(label)} · {size:.1f} MB{note}")

    links = []
    if E("RELEASE_URL") and zips:
        links.append(f'<a href="{html.escape(E("RELEASE_URL"))}">Release</a>')
    if E("RUN_URL"):
        links.append(f'<a href="{html.escape(E("RUN_URL"))}">CI #{html.escape(E("RUN_NUMBER", ""))}</a>')
    if E("COMMIT_URL"):
        links.append(f'<a href="{html.escape(E("COMMIT_URL"))}">Commit</a>')
    if links:
        lines += ["", " · ".join(links)]

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:3990] + "…"
    ok = api("sendMessage", base({"text": text, "disable_web_page_preview": "true"}))

    for z in zips:
        if os.path.getsize(z) > LIMIT:
            continue
        time.sleep(1)  # stay under the per-chat rate limit
        cap = f"<code>{html.escape(os.path.basename(z))}</code>\nsha256: <code>{sha256(z)[:16]}…</code>"
        ok &= api("sendDocument", base({"caption": cap, "disable_notification": "true"}), doc=z)

    sums = os.path.join(ART, "checksums.txt")
    if zips and os.path.exists(sums):
        time.sleep(1)
        ok &= api("sendDocument", base({"caption": "SHA-256 checksums", "disable_notification": "true"}), doc=sums)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
