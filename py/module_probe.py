#!/usr/bin/env python3
"""Read-only compatibility probe: how a kernel release string interacts with module loading.

It inspects a cloned kernel source tree and its .config and reports what the tree itself says about module versioning,
module signing and the release string. It answers one question before the release string is changed: can the proposed
string break module loading, and what can only be settled outside CI?

Usage:
    module_probe.py --src DIR [--config FILE] [--kernel-name NAME --run-num STR --sha7 SHA] [--brand NAME]
                    [--modules DIR] [--json FILE] [--markdown FILE]
    module_probe.py --modules DIR [--json FILE] [--markdown FILE]       (stock module files only, no kernel tree)

READ-ONLY. The probe never writes into the source tree and never changes LOCALVERSION or any other file the build uses:
  * the only files it writes are the --json and --markdown reports, and it refuses a path inside --src;
  * git is used only through `git --no-optional-locks rev-parse` and `git --no-optional-locks log`;
  * there is no commit, push, checkout, config or patch step.

Every finding carries a status, so what was seen is never mixed up with what was assumed:
  VERIFIED       read directly from a file of the tree or from the .config (the evidence names the file and line)
  COMPUTED       arithmetic on verified inputs (for example the length of the release string)
  UNDETERMINED   the tree was inspected but the thing was not found, or the inputs were not given
  NOT VERIFIABLE needs something CI does not have (stock vendor modules, a device, a signing key, a boot)

Results are text-pattern analysis of the C sources, not execution. They say what the code says, not what a built kernel
does. A boot on the device is the final proof, and the report says so.
"""

import argparse
import json
import re
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERIFIED = "VERIFIED"
COMPUTED = "COMPUTED"
UNDETERMINED = "UNDETERMINED"
NOT_VERIFIABLE = "NOT VERIFIABLE"

OK, RISK, UNKNOWN, INFO = "ok", "risk", "unknown", "info"

SIGNATURE_MARKER = b"~Module signature appended~\n"
MODVERSION_ENTRY = 64                # sizeof(struct modversion_info): crc + name, 64 bytes on arm64
SAFE_RELEASE = re.compile(r"^[A-Za-z0-9._~+-]+$")
GIT_READ_ONLY = ("rev-parse", "log")


class ProbeError(Exception):
    pass


# ---------------------------------------------------------------------------------------------------------------
# Safe access: nothing here can modify the tree

def run_git(src: Path, *args: str) -> str:
    """git, restricted to commands that cannot change the repository (and that take no index lock)."""
    if not args or args[0] not in GIT_READ_ONLY:
        raise ProbeError(f"refusing to run 'git {' '.join(args)}': the probe only uses {', '.join(GIT_READ_ONLY)}")
    result = subprocess.run(["git", "--no-optional-locks", "-C", str(src), *args], capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        raise ProbeError(result.stderr.strip() or f"git {args[0]} failed")
    return result.stdout.strip()


def output_path(path: str, src: Optional[Path]) -> Path:
    """A report path, refused when it lies inside the source tree."""
    target = Path(path).expanduser().resolve()
    root = src.resolve() if src else None
    if root and (target == root or root in target.parents):
        raise ProbeError(f"refusing to write {target}: it is inside the source tree {root}. The probe is read-only")
    return target


def read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


# ---------------------------------------------------------------------------------------------------------------
# .config and C source helpers

def parse_config(text: Optional[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^CONFIG_([A-Za-z0-9_]+)=(.*)$", line)
        if m:
            values[m.group(1)] = m.group(2).strip().strip('"')
            continue
        m = re.match(r"^# CONFIG_([A-Za-z0-9_]+) is not set$", line)
        if m:
            values[m.group(1)] = "not set"
    return values


def extract_function(text: str, name: str) -> Optional[Tuple[str, int]]:
    """The body of a C function definition (not a call), with the line it starts on."""
    pattern = re.compile(rf"^[ \t]*(?:static\s+|inline\s+|noinline\s+)*[\w \t\*]+?\b{re.escape(name)}\s*\([^;{{}}]*\)\s*\{{", re.M)
    m = pattern.search(text)
    if not m:
        return None
    start, depth, i = m.start(), 0, m.end() - 1
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1], line_of(text, start)
        i += 1
    return None


def help_text(root: Path, symbol: str) -> Optional[Tuple[str, str]]:
    """The Kconfig help of a symbol, from the tree's own Kconfig files."""
    for sub in ("init", "kernel", "certs", "arch/arm64", "security", "android"):
        base = root / sub
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("Kconfig*")):
            text = read(path)
            if not text:
                continue
            m = re.search(rf"^config {re.escape(symbol)}\b(.*?)(?=^(?:config|menuconfig|menu|endmenu|choice|endchoice|source|if|endif)\b|\Z)",
                          text, re.M | re.S)
            if m:
                help_m = re.search(r"^\s*help\s*\n(.*)", m.group(1), re.M | re.S)
                body = " ".join(part.strip() for part in (help_m.group(1) if help_m else "").splitlines() if part.strip())
                return str(path.relative_to(root)), body[:400]
    return None


class Report:
    def __init__(self) -> None:
        self.findings: List[Dict[str, Any]] = []

    def add(self, area: str, title: str, status: str, result: str, detail: str = "", evidence: Optional[List[str]] = None) -> None:
        self.findings.append({"area": area, "title": title, "status": status, "result": result,
                              "detail": detail, "evidence": evidence or []})


# ---------------------------------------------------------------------------------------------------------------
# The checks

def check_source(src: Path, report: Report, use_git: bool) -> Dict[str, str]:
    info = {"version": "", "commit": ""}
    makefile = read(src / "Makefile")
    if makefile is None:
        report.add("Source", "Linux version", UNDETERMINED, UNKNOWN, f"{src}/Makefile could not be read")
        return info
    parts = {}
    for key in ("VERSION", "PATCHLEVEL", "SUBLEVEL"):
        m = re.search(rf"^{key}\s*=\s*(\d+)", makefile, re.M)
        parts[key] = m.group(1) if m else ""
    if all(parts.values()):
        info["version"] = f"{parts['VERSION']}.{parts['PATCHLEVEL']}.{parts['SUBLEVEL']}"
        report.add("Source", "Linux version", VERIFIED, INFO, f"Linux {info['version']}", ["Makefile: VERSION, PATCHLEVEL, SUBLEVEL"])
    else:
        report.add("Source", "Linux version", UNDETERMINED, UNKNOWN, "VERSION, PATCHLEVEL and SUBLEVEL were not all found in the Makefile")
    if use_git:
        try:
            info["commit"] = run_git(src, "rev-parse", "HEAD")
            report.add("Source", "Commit inspected", VERIFIED, INFO, info["commit"], ["git rev-parse HEAD"])
        except (ProbeError, OSError, subprocess.TimeoutExpired):
            report.add("Source", "Commit inspected", UNDETERMINED, INFO, "the tree is not a git checkout, or git is unavailable")
    return info


def check_config_values(config: Dict[str, str], have_config: bool, report: Report) -> None:
    names = ["MODULES", "MODVERSIONS", "MODULE_SRCVERSION_ALL", "MODULE_FORCE_LOAD", "MODULE_UNLOAD", "MODULE_FORCE_UNLOAD",
             "TRIM_UNUSED_KSYMS", "UNUSED_KSYMS_WHITELIST", "RANDSTRUCT"]
    if not have_config:
        report.add("Module versioning", ".config values", UNDETERMINED, UNKNOWN, "no .config was given, so the module settings were not read")
        return
    for name in names:
        value = config.get(name, "absent")
        report.add("Module versioning", f"CONFIG_{name}", VERIFIED, INFO, value, [".config"])
    if config.get("MODULES") != "y":
        report.add("Module versioning", "Loadable modules", VERIFIED, RISK, "CONFIG_MODULES is not enabled, so there is no module loading to protect", evidence=[".config"])
    if config.get("MODVERSIONS") != "y":
        report.add("Module versioning", "Symbol version CRCs", VERIFIED, RISK,
                   "CONFIG_MODVERSIONS is not enabled: modules carry no CRCs, so the whole version string including the release has to match",
                   [".config"])
    else:
        report.add("Module versioning", "Symbol version CRCs", VERIFIED, OK,
                   "CONFIG_MODVERSIONS=y: modules are built with per-symbol CRCs", [".config"])


def find_module_core(src: Path) -> Tuple[Optional[Path], Optional[str]]:
    for rel in ("kernel/module/main.c", "kernel/module.c"):
        text = read(src / rel)
        if text is not None:
            return src / rel, text
    return None, None


def check_module_core(src: Path, report: Report) -> Dict[str, Any]:
    result: Dict[str, Any] = {"release_ignored_with_crcs": None, "core_file": ""}
    path, text = find_module_core(src)
    if text is None:
        report.add("Module versioning", "Module loader source", UNDETERMINED, UNKNOWN, "neither kernel/module/main.c nor kernel/module.c was found")
        for title in ("Version magic comparison (same_magic)", "Where the comparison is applied (check_modinfo)", "Per-symbol CRC checks"):
            report.add("Module versioning", title, UNDETERMINED, UNKNOWN, "not checked: it needs the module loader source, which was not found")
        return result
    rel = str(path.relative_to(src))
    result["core_file"] = rel
    report.add("Module versioning", "Module loader source", VERIFIED, INFO, rel)

    # The comparison of version magic strings
    same = extract_function(text, "same_magic")
    if same is None:
        report.add("Module versioning", "Version magic comparison (same_magic)", UNDETERMINED, UNKNOWN,
                   "no definition of same_magic() was found in the module loader, so the comparison rule was not established", [rel])
    else:
        body, line = same
        skips = bool(re.search(r"strcspn\s*\(\s*\w+\s*,\s*\" \"\s*\)|strchr\s*\(\s*\w+\s*,\s*' '\s*\)", body))
        conditional = bool(re.search(r"if\s*\(\s*(?:has_crcs|crcs|modversions)\s*\)", body))
        compares = "strcmp" in body or "strncmp" in body
        evidence = [f"{rel}:{line}"] + [l.strip() for l in body.splitlines()[:14]]
        if skips and conditional and compares:
            result["release_ignored_with_crcs"] = True
            report.add("Module versioning", "Version magic comparison (same_magic)", VERIFIED, OK,
                       "When a module carries symbol CRCs, the first word of the version magic (the kernel release) is skipped before the "
                       "comparison. A module without CRCs is compared as a whole string, release included.", evidence=evidence)
        elif compares:
            result["release_ignored_with_crcs"] = False
            report.add("Module versioning", "Version magic comparison (same_magic)", VERIFIED, RISK,
                       "The version magic is compared as a whole in this tree, release included, with no CRC exception. "
                       "Any change to the release string would stop every module built against the old string from loading.", evidence=evidence)
        else:
            report.add("Module versioning", "Version magic comparison (same_magic)", UNDETERMINED, UNKNOWN,
                       "same_magic() was found but its comparison could not be recognised", evidence=evidence)

    # Where it is applied, and the forced-load escape hatches
    modinfo = extract_function(text, "check_modinfo")
    if modinfo is None:
        report.add("Module versioning", "Where the comparison is applied (check_modinfo)", UNDETERMINED, UNKNOWN,
                   "check_modinfo() was not found", [rel])
    else:
        body, line = modinfo
        uses_vers = bool(re.search(r"same_magic\s*\([^)]*(?:index\.vers|has_crcs|crcs)", body))
        force = "MODULE_INIT_IGNORE_VERMAGIC" in body
        message = re.search(r"version magic '%s' should be '%s'", body) is not None
        report.add("Module versioning", "Where the comparison is applied (check_modinfo)", VERIFIED, INFO,
                   "same_magic() is called with the module's CRC presence: " + ("yes" if uses_vers else "not recognised")
                   + "; forced loading with MODULE_INIT_IGNORE_VERMAGIC: " + ("handled" if force else "not found")
                   + "; kernel log text 'version magic ... should be ...': " + ("present" if message else "not found"),
                   [f"{rel}:{line}"])
        if message:
            report.add("Module versioning", "Log text to search for on a device", VERIFIED, INFO,
                       "A rejected module logs: version magic '<module>' should be '<kernel>' (dmesg)", [f"{rel}:{line}"])

    # Per-symbol CRC checks, which do not involve the release string
    version_text = read(src / "kernel/module/version.c") or text
    version_rel = "kernel/module/version.c" if (src / "kernel/module/version.c").exists() else rel
    crc_funcs = [name for name in ("check_version", "check_modstruct_version") if extract_function(version_text, name)]
    disagree = "disagrees about version of symbol" in version_text
    if crc_funcs:
        report.add("Module versioning", "Per-symbol CRC checks", VERIFIED, INFO,
                   f"{', '.join(crc_funcs)} compare symbol CRCs; log text 'disagrees about version of symbol': {'present' if disagree else 'not found'}. "
                   "These checks read CRCs, not the release string (reasoned from the function names, not proven by the probe).",
                   [version_rel])
    else:
        report.add("Module versioning", "Per-symbol CRC checks", UNDETERMINED, UNKNOWN, "check_version() was not found", [version_rel])
    return result


def check_vermagic_header(src: Path, report: Report) -> None:
    header = read(src / "include/linux/vermagic.h")
    if header is None:
        report.add("Module versioning", "Layout of the version magic", UNDETERMINED, UNKNOWN, "include/linux/vermagic.h was not found")
        return
    m = re.search(r"#define\s+VERMAGIC_STRING\b((?:[^\n]*\\\n)*[^\n]*)", header)
    if not m:
        report.add("Module versioning", "Layout of the version magic", UNDETERMINED, UNKNOWN, "VERMAGIC_STRING was not found", ["include/linux/vermagic.h"])
        return
    tokens = re.sub(r"\\\n", " ", m.group(1)).split()
    first = tokens[0] if tokens else ""
    line = line_of(header, m.start())
    if first == "UTS_RELEASE":
        report.add("Module versioning", "Layout of the version magic", VERIFIED, OK,
                   "VERMAGIC_STRING starts with UTS_RELEASE, so the kernel release is the first word of the version magic, "
                   "the word that same_magic() skips for modules with CRCs", [f"include/linux/vermagic.h:{line}"])
    else:
        report.add("Module versioning", "Layout of the version magic", VERIFIED, RISK,
                   f"VERMAGIC_STRING does not start with UTS_RELEASE (it starts with '{first}'), so skipping the first word "
                   "does not skip the release", [f"include/linux/vermagic.h:{line}"])


def check_gki_protection(src: Path, report: Report) -> None:
    gki_files = sorted(str(p.relative_to(src)) for p in (src / "kernel/module").glob("gki_module*")) if (src / "kernel/module").is_dir() else []
    protected = sorted(str(p.relative_to(src)) for p in (src / "android").glob("abi_gki_protected_exports*")) if (src / "android").is_dir() else []
    report.add("Module signing", "GKI module protection files", VERIFIED, INFO,
               ("protected-export handling: " + ", ".join(gki_files) if gki_files else "no kernel/module/gki_module* file in this tree")
               + "; " + (("protected export lists: " + ", ".join(protected)) if protected else "no android/abi_gki_protected_exports* list in this tree"),
               gki_files + protected)


def check_signing(src: Path, config: Dict[str, str], have_config: bool, report: Report) -> None:
    check_gki_protection(src, report)
    if not have_config:
        report.add("Module signing", "Signing configuration", UNDETERMINED, UNKNOWN, "no .config was given")
        return
    names = ["MODULE_SIG", "MODULE_SIG_FORCE", "MODULE_SIG_ALL", "MODULE_SIG_PROTECT", "MODULE_SIG_HASH",
             "MODULE_SIG_KEY", "MODULE_SIG_KEY_TYPE_RSA", "SYSTEM_TRUSTED_KEYS", "MODULE_COMPRESS_NONE"]
    for name in names:
        report.add("Module signing", f"CONFIG_{name}", VERIFIED, INFO, config.get(name, "absent"), [".config"])
    for name in ("MODULE_SIG_FORCE", "MODULE_SIG_PROTECT"):
        found = help_text(src, name)
        if found:
            rel, body = found
            report.add("Module signing", f"What the tree says CONFIG_{name} means", VERIFIED, INFO, body or "(no help text)", [rel])
        else:
            report.add("Module signing", f"What the tree says CONFIG_{name} means", UNDETERMINED, INFO, "no Kconfig help text was found for it")
    key = src / "certs" / "signing_key.pem"
    report.add("Module signing", "Signing key in the tree", VERIFIED, INFO,
               "certs/signing_key.pem is " + ("present" if key.exists() else "absent: the build generates a key"),
               ["certs/"])
    if config.get("MODULE_SIG_FORCE") == "y":
        report.add("Module signing", "Enforced signatures", VERIFIED, INFO,
                   "CONFIG_MODULE_SIG_FORCE=y: see the tree's help text above for what happens to modules whose signature this build does not trust. "
                   "This does not depend on the release string.", [".config"])


def uts_len(src: Path) -> Tuple[Optional[int], str]:
    for rel in ("include/uapi/linux/utsname.h", "include/linux/utsname.h"):
        text = read(src / rel)
        if text:
            m = re.search(r"#define\s+__NEW_UTS_LEN\s+(\d+)", text)
            if m:
                return int(m.group(1)), f"{rel}:{line_of(text, m.start())}"
    return None, ""


def compose_release(version: str, kernel_name: str, run_num: str, sha7: str) -> str:
    """The release string exactly as build.sh forms it: <kernelversion>-<KERNEL_NAME><RUN_NUM>-<sha7>."""
    return f"{version}-{kernel_name}{run_num}-{sha7}"


def check_release(src: Path, config: Dict[str, str], have_config: bool, info: Dict[str, str], args: argparse.Namespace, report: Report) -> Dict[str, Any]:
    result: Dict[str, Any] = {"current": "", "proposed": "", "limit": None}
    version = info["version"]
    makefile = read(src / "Makefile") or ""
    evidence = [f"Makefile:{line_of(makefile, m.start())}: {m.group(0).strip()}"
                for m in re.finditer(r"^.*(?:kernel\.release|KERNELRELEASE|setlocalversion).*$", makefile, re.M)][:6]
    report.add("Release string", "How the release is composed", VERIFIED if evidence else UNDETERMINED, INFO if evidence else UNKNOWN,
               "The Makefile builds kernel.release from the kernel version plus the output of scripts/setlocalversion "
               "(and CONFIG_LOCALVERSION)." if evidence else "no kernel.release rule was recognised in the Makefile", evidence)

    setlocal = read(src / "scripts/setlocalversion")
    if setlocal is None:
        report.add("Release string", "scripts/setlocalversion", UNDETERMINED, UNKNOWN, "scripts/setlocalversion was not found")
    else:
        active = len(re.findall(r'^\s*echo\s+"\+"', setlocal, re.M))
        commented = len(re.findall(r'^\s*#\s*echo\s+"\+"', setlocal, re.M))
        report.add("Release string", "scripts/setlocalversion state", VERIFIED, INFO,
                   f'`echo "+"` lines: {active} active, {commented} commented out. build.sh comments them out in kernel mode, '
                   "so a dirty tree does not add a '+' to the release.", ["scripts/setlocalversion"])

    if have_config:
        report.add("Release string", "CONFIG_LOCALVERSION in .config", VERIFIED, INFO,
                   repr(config.get("LOCALVERSION", "absent")) + ("; LOCALVERSION_AUTO=" + config.get("LOCALVERSION_AUTO", "absent")),
                   [".config"])

    limit, limit_evidence = uts_len(src)
    result["limit"] = limit
    if limit is None:
        report.add("Release string", "Release length limit", UNDETERMINED, UNKNOWN, "__NEW_UTS_LEN was not found in the utsname headers")
    else:
        report.add("Release string", "Release length limit", VERIFIED, INFO,
                   f"__NEW_UTS_LEN is {limit}: the release field of uname holds at most {limit} characters", [limit_evidence])
    init_text = "".join(read(p) or "" for p in sorted((src / "init").glob("version*.c"))) if (src / "init").is_dir() else ""
    if re.search(r"\.release\s*=\s*UTS_RELEASE", init_text):
        report.add("Release string", "Where the release enters uname", VERIFIED, INFO,
                   "init_uts_ns takes .release from UTS_RELEASE", ["init/version*.c"])

    if not version:
        report.add("Release string", "Proposed release string", UNDETERMINED, UNKNOWN, "the Linux version was not found, so no release string was composed")
        return result
    sha7 = args.sha7 or info["commit"][:7]
    if not args.kernel_name and not args.brand:
        return result
    brand = args.brand or "HyperCore"
    run_num = args.run_num or ""
    current = compose_release(version, args.kernel_name, run_num, sha7) if args.kernel_name else ""
    proposed = compose_release(version, brand, run_num, sha7)
    result["current"], result["proposed"] = current, proposed

    if current:
        report.add("Release string", "Release string this build forms today", COMPUTED, INFO,
                   f"{current} ({len(current)} characters)", ["build.sh: CONFIG_LOCALVERSION=-${KERNEL_NAME}${RUN_NUM}-${sha7}"])
        if have_config and config.get("LOCALVERSION") not in (None, "absent"):
            expected = f"-{args.kernel_name}{run_num}-{sha7}"
            same = config.get("LOCALVERSION") == expected
            report.add("Release string", "Computed string matches .config", VERIFIED, OK if same else INFO,
                       f".config has {config.get('LOCALVERSION')!r}, the computed suffix is {expected!r}" + ("" if same else " (they differ)"),
                       [".config"])
    report.add("Release string", "Proposed release string", COMPUTED, INFO, f"{proposed} ({len(proposed)} characters)",
               [f"same formula with KERNEL_NAME={brand}"])

    major_minor = ".".join(version.split(".")[:2])
    worst = compose_release(f"{major_minor}.999", brand, "-r999999999", "0" * 12)
    result["worst_case"] = worst
    if limit is not None:
        for label, text in (("Proposed", proposed), ("Worst case (3-digit sublevel, 9-digit run number, 12-character hash)", worst)):
            fits = len(text) <= limit
            report.add("Release string", f"{label}: length against the limit", COMPUTED, OK if fits else RISK,
                       f"{len(text)} of {limit} characters, {limit - len(text)} to spare" if fits
                       else f"{len(text)} characters exceeds the {limit} character limit of uname's release field",
                       [text])
    safe = bool(SAFE_RELEASE.match(proposed))
    report.add("Release string", "Characters in the proposed release", COMPUTED, OK if safe else RISK,
               "only letters, digits and . _ ~ + -: no spaces or slashes, safe as one word in /proc/version and in module paths"
               if safe else "contains characters outside letters, digits and . _ ~ + - (spaces and slashes break parsers and module paths)",
               [proposed])
    report.add("Release string", "Upstream version stays first", COMPUTED, OK if proposed.startswith(version + "-") else RISK,
               f"the release begins with the Linux version {version}", [proposed])
    return result


NOT_VERIFIABLE_ITEMS = [
    ("Stock vendor modules carry symbol CRCs",
     "needs the .ko files of the stock ROM (vendor_dlkm / vendor_boot). Run this probe with --modules on a PC with those files, or run "
     "`modinfo -F vermagic` and check for a __versions section."),
    ("Version magic of the stock vendor modules",
     "the first word of each module's vermagic is the release it was built for. Needs the module files or `modinfo` on the device."),
    ("Signature and signer of the stock modules",
     "needs the module files and the key this build trusts. The probe can only report whether a signature is appended."),
    ("Whether this build's key matches the key that signed the stock modules",
     "needs the stock signing key or a build with the same SYSTEM_TRUSTED_KEYS. Not available in CI."),
    ("What Settings shows on the device's ROM",
     "needs the device: Settings > About phone > Kernel version, and `uname -r` / `cat /proc/version`."),
    ("Whether modules load at runtime",
     "needs a boot on the device: `dmesg | grep -iE 'version magic|disagrees about version|module verification failed|Unknown symbol'`."),
    ("Userspace that parses the kernel release",
     "vendor HALs, VINTF compatibility and apps that read `uname -r` cannot be listed from the kernel tree."),
    ("Behaviour of the compiled kernel",
     "the probe reads source text and configuration. It does not compile or run anything."),
]


# ---------------------------------------------------------------------------------------------------------------
# Optional: look at real module files (a stock ROM's vendor modules), on a PC

def elf_sections(data: bytes) -> Dict[str, bytes]:
    if data[:4] != b"\x7fELF" or len(data) < 64 or data[4] != 2 or data[5] != 1:
        raise ProbeError("not a little-endian 64-bit ELF file")
    (_, _, _, _, _, _, shoff, _, _, _, _, shentsize, shnum, shstrndx) = struct.unpack("<16sHHIQQQIHHHHHH", data[:64])
    if shoff == 0 or shnum == 0:
        return {}
    headers = []
    for i in range(shnum):
        off = shoff + i * shentsize
        headers.append(struct.unpack("<IIQQQQIIQQ", data[off:off + 64]))
    strtab_off, strtab_size = headers[shstrndx][4], headers[shstrndx][5]
    names = data[strtab_off:strtab_off + strtab_size]
    sections: Dict[str, bytes] = {}
    for sh_name, _, _, _, offset, size, *_ in headers:
        end = names.find(b"\0", sh_name)
        name = names[sh_name:end if end >= 0 else None].decode("ascii", "replace")
        sections[name] = data[offset:offset + size]
    return sections


def inspect_module(path: Path) -> Dict[str, Any]:
    data = path.read_bytes()
    sections = elf_sections(data)
    info: Dict[str, str] = {}
    for item in sections.get(".modinfo", b"").split(b"\0"):
        if b"=" in item:
            key, _, value = item.decode("utf-8", "replace").partition("=")
            info.setdefault(key, value)
    vermagic = info.get("vermagic", "")
    return {
        "file": path.name, "name": info.get("name", path.stem), "vermagic": vermagic,
        "release": vermagic.split(" ", 1)[0] if vermagic else "",
        "crc_entries": len(sections.get("__versions", b"")) // MODVERSION_ENTRY,
        "has_crcs": len(sections.get("__versions", b"")) >= MODVERSION_ENTRY,
        "signed": data.endswith(SIGNATURE_MARKER),
    }


def check_modules(directory: Path, rule: Optional[bool], results: Dict[str, Any], report: Report) -> List[Dict[str, Any]]:
    files = sorted(directory.rglob("*.ko"))
    if not files:
        report.add("Module files", "Module files given", UNDETERMINED, UNKNOWN, f"no .ko files were found under {directory}")
        return []
    modules, errors = [], []
    for path in files:
        try:
            modules.append(inspect_module(path))
        except (ProbeError, OSError, struct.error, IndexError) as error:
            errors.append(f"{path.name}: {error}")
    with_crcs = [m for m in modules if m["has_crcs"]]
    without = [m for m in modules if not m["has_crcs"]]
    releases = sorted({m["release"] for m in modules if m["release"]})
    report.add("Module files", "Modules inspected", VERIFIED, INFO,
               f"{len(modules)} modules read ({len(errors)} could not be read); {len(with_crcs)} carry symbol CRCs, {len(without)} do not; "
               f"{sum(1 for m in modules if m['signed'])} have a signature appended", [str(directory)])
    report.add("Module files", "Release words found in the modules' version magic", VERIFIED, INFO, ", ".join(releases) or "none", [str(directory)])
    for error in errors[:5]:
        report.add("Module files", "Unreadable module", UNDETERMINED, UNKNOWN, error)
    if without:
        names = ", ".join(m["name"] for m in without[:12]) + (" ..." if len(without) > 12 else "")
        report.add("Module files", "Modules without symbol CRCs", VERIFIED, RISK,
                   f"{len(without)} modules have no __versions section, so their whole version magic, release included, has to match "
                   f"the kernel's exactly. Changing the release would stop them from loading: {names}", [str(directory)])
    elif modules:
        report.add("Module files", "Modules without symbol CRCs", VERIFIED, OK, "every module carries symbol CRCs", [str(directory)])
    if rule is True and with_crcs:
        report.add("Module files", "Effect of a different release on modules with CRCs", VERIFIED, OK,
                   f"{len(with_crcs)} modules have CRCs: the loader of this tree skips the release word for them, so a different release does not "
                   "change the outcome of the version magic check (the CRC checks and the rest of the version magic still apply).", [str(directory)])
    elif rule is False and with_crcs:
        report.add("Module files", "Effect of a different release on modules with CRCs", VERIFIED, RISK,
                   "this tree compares the whole version magic, so a different release would reject these modules too", [str(directory)])
    elif with_crcs:
        report.add("Module files", "Effect of a different release on modules with CRCs", UNDETERMINED, UNKNOWN,
                   "the loader rule was not established (give --src), so the effect on these modules is not known", [str(directory)])
    return modules


# ---------------------------------------------------------------------------------------------------------------

def run(args: argparse.Namespace) -> Dict[str, Any]:
    src = Path(args.src).resolve() if args.src else None
    if src is None and not args.modules:
        raise ProbeError("give --src (a kernel source tree), --modules (a directory of .ko files), or both")
    if src is not None and not src.is_dir():
        raise ProbeError(f"--src {src} is not a directory")
    json_path = output_path(args.json, src) if args.json else None
    md_path = output_path(args.markdown, src) if args.markdown else None

    report = Report()
    config_text = read(Path(args.config)) if args.config else None
    have_config = config_text is not None
    if args.config and not have_config:
        report.add("Source", ".config", UNDETERMINED, UNKNOWN, f"{args.config} could not be read")
    config = parse_config(config_text)

    info = {"version": "", "commit": ""}
    rule: Dict[str, Any] = {"release_ignored_with_crcs": None, "core_file": ""}
    release: Dict[str, Any] = {}
    if src is not None:
        info = check_source(src, report, use_git=not args.no_git)
        check_config_values(config, have_config, report)
        rule = check_module_core(src, report)
        check_vermagic_header(src, report)
        check_signing(src, config, have_config, report)
        release = check_release(src, config, have_config, info, args, report)
    modules = check_modules(Path(args.modules), rule["release_ignored_with_crcs"], rule, report) if args.modules else []

    counts: Dict[str, int] = {}
    for finding in report.findings:
        counts[finding["result"]] = counts.get(finding["result"], 0) + 1
    result = {
        "tool": "module_probe", "read_only": True,
        "source": {"path": str(src) if src else "", "linux_version": info["version"], "commit": info["commit"], "config_read": have_config},
        "release": release, "module_loader": rule, "modules": modules,
        "counts": counts, "findings": report.findings,
        "not_verifiable": [{"item": item, "why": why} for item, why in NOT_VERIFIABLE_ITEMS],
    }
    if json_path:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(result, indent=1), encoding="utf-8")
    markdown = render_markdown(result)
    if md_path:
        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(markdown, encoding="utf-8")
    result["markdown"] = markdown
    return result


def cell(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_markdown(result: Dict[str, Any]) -> str:
    src = result["source"]
    lines = ["## HyperCore compatibility probe (read-only)", "",
             (f"Source: Linux {src['linux_version'] or 'unknown'}" + (f" at `{src['commit'][:12]}`" if src["commit"] else "")
              + f"; .config {'read' if src['config_read'] else 'not given'}." if src["path"] else "Source: no kernel tree was given, only module files."), "",
             "Nothing was changed: no file of the source, no LOCALVERSION, no commit. "
             "VERIFIED = read from the tree or config, COMPUTED = arithmetic on verified values, "
             "UNDETERMINED = looked for and not found, NOT VERIFIABLE = needs something CI does not have.", ""]
    risks = [f for f in result["findings"] if f["result"] == RISK]
    unknown = [f for f in result["findings"] if f["result"] == UNKNOWN]
    lines.append(f"**Risks found: {len(risks)}. Undetermined: {len(unknown)}.**" + ("" if risks else " No finding of the tree or config says a release change would break module loading."))
    lines.append("")
    for f in risks:
        lines.append(f"- RISK: {f['title']}: {f['detail'] or f['result']}")
    if risks:
        lines.append("")
    area = None
    for f in result["findings"]:
        if f["area"] != area:
            area = f["area"]
            lines += ["", f"### {area}", "", "| Status | Finding | Result | Evidence |", "|---|---|---|---|"]
        text = f["detail"] or f["result"]
        evidence = "; ".join(f["evidence"][:3])
        lines.append(f"| {f['status']} | {cell(f['title'])} | {cell(text)} | {cell(evidence)} |")
    lines += ["", "### Cannot be verified in CI", "", "| Item | Why, and how to close it |", "|---|---|"]
    for item in result["not_verifiable"]:
        lines.append(f"| {cell(item['item'])} | {cell(item['why'])} |")
    return "\n".join(lines) + "\n"


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="module_probe.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--src", help="the cloned kernel source tree (read only)")
    parser.add_argument("--config", help="the generated .config (read only)")
    parser.add_argument("--kernel-name", default="", help="KERNEL_NAME of this build, to compute the release it forms today")
    parser.add_argument("--run-num", default="", help="RUN_NUM exactly as build.sh has it, which includes the dash, for example -r12")
    parser.add_argument("--sha7", default="", help="the short commit hash build.sh puts in the release")
    parser.add_argument("--brand", default="", help="the brand to propose in place of KERNEL_NAME, for example HyperCore")
    parser.add_argument("--modules", help="a directory of stock .ko files to inspect (optional, usually on a PC)")
    parser.add_argument("--json", help="write the report as JSON to this file (must be outside --src)")
    parser.add_argument("--markdown", help="write the report as Markdown to this file (must be outside --src)")
    parser.add_argument("--no-git", action="store_true", help="do not call git at all")
    parser.add_argument("--strict", action="store_true", help="exit with status 1 when a risk was found (the build hook never uses this)")
    # RUN_NUM is written like -r12, which argparse would take for an option. Accept '--run-num -r12' as '--run-num=-r12'.
    argv = list(argv)
    for i, item in enumerate(argv):
        if item == "--run-num" and i + 1 < len(argv):
            argv[i:i + 2] = [f"--run-num={argv[i + 1]}"]
            break
    args = parser.parse_args(argv[1:])
    try:
        result = run(args)
    except ProbeError as error:
        print(f"[-] {error}", file=sys.stderr)
        return 2
    print(result["markdown"])
    return 1 if args.strict and result["counts"].get(RISK) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
