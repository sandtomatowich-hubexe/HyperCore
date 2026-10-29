#!/usr/bin/env python3
"""HyperCore kernel source registry: load, validate and query sources.json.

Usage:
    sources.py [--registry FILE] validate
    sources.py [--registry FILE] lines
    sources.py [--registry FILE] legacy-override-line
    sources.py [--registry FILE] line-field <line> <android_release|kmi|susfs_branch|defconfig>
    sources.py [--registry FILE] default-device
    sources.py [--registry FILE] sources [<line>]
    sources.py [--registry FILE] select <line> [--source ID] [--flag NAME=value ...]
    sources.py [--registry FILE] status <source_id> [<device>]
    sources.py [--registry FILE] gate --kernel-version <line|All> [--source ID] [--device ID]
               [--allow-unverified true|false] [--custom-lines "6.1 5.10"] [--gha]

`select` prints the built-in default source for a kernel line as one pipe-separated record:
    repo|branch|kmi|source_id|repo_label|branch_label|variant_flag
variant_flag is the flag that picked a branch variant (for example NH), or empty when the plain branch is used.
This is the lowest layer of source selection. The KERNEL_REPO / KERNEL_BRANCH* repository variables are
applied on top of it by resolve_kernel_source_config in functions-extra.sh.

Every command validates the registry first. A broken registry exits with status 1 and one line per problem,
each naming the exact place in the file, so it can be fixed without reading this script.
"""

import argparse
import difflib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1
DEFAULT_REGISTRY = Path(__file__).resolve().parent.parent / "sources.json"

LINE_RE = re.compile(r"^\d+\.\d+$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
# Same character rules that resolve_kernel_source_config enforces on branches
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/+-]+$")
KMI_RE = re.compile(r"^android\d+-\d+\.\d+$")
DEFCONFIG_RE = re.compile(r"^[A-Za-z0-9._-]*defconfig$")
FLAG_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")

COMPATIBILITY = ("confirmed", "needs-verification", "unsupported")
VERIFICATION_STATES = ("unverified", "reported", "verified")
BASIS = ("tested", "documented", "reasoned")
LINE_FIELDS = ("android_release", "kmi", "susfs_branch", "defconfig")

TOP_KEYS = {"schema_version", "legacy_override_line", "default_device", "devices", "lines", "sources"}
DEVICE_KEYS = {"name", "soc", "kernel_line", "android_release", "references"}
LINE_KEYS = {"android_release", "kmi", "susfs_branch", "defconfig", "toolchain"}
TOOLCHAIN_KEYS = {"name", "notes"}
SOURCE_KEYS = {"line", "repo", "branch", "label", "default_for_line", "branch_variants", "devices"}
VARIANT_KEYS = {"when_flag", "branch", "label"}
ASSESSMENT_KEYS = {
    "compatibility", "basis", "reason", "build_verification", "boot_verification", "notes",
}
VERIFICATION_KEYS = {"state", "evidence"}


class RegistryError(Exception):
    """The registry cannot be used. `problems` holds one actionable line per issue."""

    def __init__(self, problems: List[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def _no_duplicates(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError(f"duplicate key '{key}'")
        seen[key] = value
    return seen


def read_registry(path: Path) -> Dict[str, Any]:
    name = path.name
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise RegistryError([f"{name}: file not found at {path}"])
    except OSError as error:
        raise RegistryError([f"{name}: cannot be read: {error}"])
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except json.JSONDecodeError as error:
        raise RegistryError([f"{name}: invalid JSON at line {error.lineno}, column {error.colno}: {error.msg}"])
    except ValueError as error:
        raise RegistryError([f"{name}: {error}"])
    if not isinstance(data, dict):
        raise RegistryError([f"{name}: the top level must be a JSON object"])
    return data


class _Checker:
    def __init__(self, name: str):
        self.name = name
        self.problems: List[str] = []

    def add(self, where: str, message: str) -> None:
        self.problems.append(f"{self.name}: {where}: {message}")

    def obj(self, value: Any, where: str, allowed: set, required: set) -> bool:
        """Check that value is an object with only allowed keys and all required keys."""
        if not isinstance(value, dict):
            self.add(where, "must be an object")
            return False
        for key in value:
            if key not in allowed:
                close = difflib.get_close_matches(key, allowed, n=1)
                hint = f" (did you mean '{close[0]}'?)" if close else ""
                self.add(f"{where}.{key}", f"unknown key{hint}. Allowed: {', '.join(sorted(allowed))}")
        for key in sorted(required):
            if key not in value:
                self.add(where, f"missing required key '{key}'")
        return True

    def string(self, value: Any, where: str, pattern: Optional["re.Pattern"] = None, what: str = "") -> bool:
        if not isinstance(value, str) or not value.strip():
            self.add(where, "must be a non-empty string")
            return False
        if pattern is not None and not pattern.match(value):
            self.add(where, f"'{value}' is not valid{(' ' + what) if what else ''}")
            return False
        return True

    def choice(self, value: Any, where: str, options: tuple) -> bool:
        if value not in options:
            self.add(where, f"'{value}' is not one of: {', '.join(options)}")
            return False
        return True


def _check_repo(c: _Checker, value: Any, where: str) -> None:
    if not c.string(value, where):
        return
    if not value.startswith("https://") or re.search(r"\s", value) or len(value) <= len("https://"):
        c.add(where, f"'{value}' must be an https:// URL without spaces")


def _check_verification(c: _Checker, value: Any, where: str) -> Optional[str]:
    if not c.obj(value, where, VERIFICATION_KEYS, {"state"}):
        return None
    state = value.get("state")
    if not c.choice(state, f"{where}.state", VERIFICATION_STATES):
        return None
    evidence = value.get("evidence")
    if state != "unverified" and not (isinstance(evidence, str) and evidence.strip()):
        c.add(where, f"state '{state}' needs a non-empty 'evidence' that says what was seen and where")
    return state


def check_registry(data: Dict[str, Any], name: str = "sources.json") -> List[str]:
    c = _Checker(name)
    if not c.obj(data, "(top level)", TOP_KEYS, {"schema_version", "legacy_override_line", "default_device", "devices", "lines", "sources"}):
        return c.problems

    if data.get("schema_version") != SCHEMA_VERSION:
        c.add("schema_version", f"is {data.get('schema_version')!r} but this tool reads version {SCHEMA_VERSION}")

    devices = data.get("devices") if isinstance(data.get("devices"), dict) else {}
    lines = data.get("lines") if isinstance(data.get("lines"), dict) else {}
    sources = data.get("sources") if isinstance(data.get("sources"), dict) else {}
    for key, value in (("devices", data.get("devices")), ("lines", data.get("lines")), ("sources", data.get("sources"))):
        if not isinstance(value, dict) or not value:
            c.add(key, "must be a non-empty object")

    # devices
    for dev_id, dev in devices.items():
        where = f"devices.{dev_id}"
        c.string(dev_id, where, ID_RE, "as a device id (lowercase letters, digits, . _ -)")
        if not c.obj(dev, where, DEVICE_KEYS, {"name", "kernel_line", "android_release"}):
            continue
        c.string(dev.get("name"), f"{where}.name")
        c.string(dev.get("kernel_line"), f"{where}.kernel_line", LINE_RE, "as a kernel line like 6.1")
        c.string(dev.get("android_release"), f"{where}.android_release", re.compile(r"^\d+$"), "as an Android release number")
        if "soc" in dev:
            c.string(dev["soc"], f"{where}.soc")
        refs = dev.get("references", [])
        if not isinstance(refs, list) or not all(isinstance(r, str) and r.strip() for r in refs):
            c.add(f"{where}.references", "must be a list of non-empty strings")
        if isinstance(dev.get("kernel_line"), str) and dev["kernel_line"] not in lines:
            c.add(f"{where}.kernel_line", f"'{dev['kernel_line']}' is not a configured line. Configured: {', '.join(lines) or 'none'}")

    # lines
    for line_id, line in lines.items():
        where = f"lines.{line_id}"
        c.string(line_id, where, LINE_RE, "as a kernel line like 6.1")
        if not c.obj(line, where, LINE_KEYS, set(LINE_FIELDS)):
            continue
        c.string(line.get("android_release"), f"{where}.android_release", re.compile(r"^\d+$"), "as an Android release number")
        c.string(line.get("kmi"), f"{where}.kmi", KMI_RE, "as a KMI branch like android14-6.1")
        c.string(line.get("susfs_branch"), f"{where}.susfs_branch", BRANCH_RE, "as a branch name")
        c.string(line.get("defconfig"), f"{where}.defconfig", DEFCONFIG_RE, "as a defconfig file name")
        if "toolchain" in line and c.obj(line["toolchain"], f"{where}.toolchain", TOOLCHAIN_KEYS, {"name"}):
            c.string(line["toolchain"].get("name"), f"{where}.toolchain.name")

    default_device = data.get("default_device")
    if isinstance(default_device, str) and default_device not in devices:
        c.add("default_device", f"'{default_device}' is not a configured device. Configured: {', '.join(devices) or 'none'}")

    legacy = data.get("legacy_override_line")
    if isinstance(legacy, str) and legacy not in lines:
        c.add("legacy_override_line", f"'{legacy}' is not a configured line. Configured: {', '.join(lines) or 'none'}")

    # sources
    defaults: Dict[str, List[str]] = {}
    for src_id, src in sources.items():
        where = f"sources.{src_id}"
        c.string(src_id, where, ID_RE, "as a source id (lowercase letters, digits, . _ -)")
        if not c.obj(src, where, SOURCE_KEYS, {"line", "repo", "branch", "label"}):
            continue
        line = src.get("line")
        if c.string(line, f"{where}.line", LINE_RE, "as a kernel line like 6.1") and line not in lines:
            c.add(f"{where}.line", f"'{line}' is not a configured line. Add it under 'lines' first. Configured: {', '.join(lines) or 'none'}")
        _check_repo(c, src.get("repo"), f"{where}.repo")
        c.string(src.get("branch"), f"{where}.branch", BRANCH_RE, "as a branch name (letters, digits and . _ / + -)")
        c.string(src.get("label"), f"{where}.label")
        if "default_for_line" in src:
            if not isinstance(src["default_for_line"], bool):
                c.add(f"{where}.default_for_line", "must be true or false")
            elif src["default_for_line"] and isinstance(line, str):
                defaults.setdefault(line, []).append(src_id)

        variants = src.get("branch_variants", [])
        if not isinstance(variants, list):
            c.add(f"{where}.branch_variants", "must be a list")
            variants = []
        seen_flags = set()
        for i, variant in enumerate(variants):
            vwhere = f"{where}.branch_variants[{i}]"
            if not c.obj(variant, vwhere, VARIANT_KEYS, VARIANT_KEYS):
                continue
            c.string(variant.get("when_flag"), f"{vwhere}.when_flag", FLAG_RE, "as a flag name like NH")
            c.string(variant.get("branch"), f"{vwhere}.branch", BRANCH_RE, "as a branch name")
            c.string(variant.get("label"), f"{vwhere}.label")
            flag = variant.get("when_flag")
            if flag in seen_flags:
                c.add(vwhere, f"flag '{flag}' is already used by an earlier variant of this source")
            seen_flags.add(flag)

        assessments = src.get("devices", {})
        if not isinstance(assessments, dict):
            c.add(f"{where}.devices", "must be an object keyed by device id")
            continue
        for dev_id, a in assessments.items():
            awhere = f"{where}.devices.{dev_id}"
            if dev_id not in devices:
                c.add(awhere, f"unknown device '{dev_id}'. Add it under 'devices' first. Known: {', '.join(devices) or 'none'}")
            if not c.obj(a, awhere, ASSESSMENT_KEYS, {"compatibility", "build_verification", "boot_verification"}):
                continue
            compat = a.get("compatibility")
            c.choice(compat, f"{awhere}.compatibility", COMPATIBILITY)
            build = _check_verification(c, a.get("build_verification"), f"{awhere}.build_verification")
            boot = _check_verification(c, a.get("boot_verification"), f"{awhere}.boot_verification")

            if compat == "confirmed":
                if boot != "verified":
                    c.add(awhere, "'confirmed' requires boot_verification.state 'verified' with evidence that it boots on the device. "
                                  "A successful build alone is not enough. Use 'needs-verification' until there is boot evidence")
                if build == "unverified":
                    c.add(awhere, "'confirmed' also needs build_verification that is not 'unverified'")
            if compat == "unsupported":
                if not (isinstance(a.get("reason"), str) and a["reason"].strip()):
                    c.add(awhere, "'unsupported' needs a non-empty 'reason'")
                c.choice(a.get("basis"), f"{awhere}.basis", BASIS)
            if isinstance(a.get("basis"), str) and compat != "unsupported":
                c.add(f"{awhere}.basis", "is only used together with compatibility 'unsupported'")

            # A device runs one kernel line. A source on another line cannot be anything but unsupported for it.
            dev = devices.get(dev_id) if isinstance(devices.get(dev_id), dict) else None
            if dev and isinstance(dev.get("kernel_line"), str) and isinstance(line, str):
                if compat in ("confirmed", "needs-verification") and line != dev["kernel_line"]:
                    c.add(f"{awhere}.compatibility",
                          f"'{compat}' is not possible: {dev_id} is a {dev['kernel_line']} device and this source is the {line} line. "
                          f"Mark it 'unsupported' with a reason")

    for line_id in lines:
        found = defaults.get(line_id, [])
        if not found:
            c.add(f"lines.{line_id}", "has no default source. Mark exactly one source of this line with \"default_for_line\": true")
        elif len(found) > 1:
            c.add(f"lines.{line_id}", f"has several default sources ({', '.join(found)}). Exactly one may have \"default_for_line\": true")

    return c.problems


class Registry:
    def __init__(self, data: Dict[str, Any]):
        self.data = data

    @property
    def lines(self) -> List[str]:
        return list(self.data["lines"].keys())

    @property
    def legacy_override_line(self) -> str:
        return self.data["legacy_override_line"]

    def _line(self, line: str) -> Dict[str, Any]:
        if line not in self.data["lines"]:
            raise RegistryError([
                f"No kernel source is configured for KERNEL_VERSION '{line}'. "
                f"Configured lines: {', '.join(self.lines)} (see sources.json)"
            ])
        return self.data["lines"][line]

    def line_field(self, line: str, field: str) -> str:
        if field not in LINE_FIELDS:
            raise RegistryError([f"Unknown line field '{field}'. Use one of: {', '.join(LINE_FIELDS)}"])
        return self._line(line)[field]

    def default_source(self, line: str) -> tuple:
        self._line(line)
        for src_id, src in self.data["sources"].items():
            if src["line"] == line and src.get("default_for_line"):
                return src_id, src
        raise RegistryError([f"Line {line} has no default source"])  # unreachable once validated

    @property
    def default_device(self) -> str:
        return self.data["default_device"]

    def source_ids(self, line: Optional[str] = None) -> List[str]:
        return [i for i, s in self.data["sources"].items() if line is None or s["line"] == line]

    def pick_source(self, line: str, source_id: str = "") -> tuple:
        """The default source of `line`, or the explicitly requested one (which must belong to that line)."""
        if source_id in ("", "default"):
            return self.default_source(line)
        self._line(line)
        sources_ = self.data["sources"]
        if source_id not in sources_:
            close = difflib.get_close_matches(source_id, list(sources_), n=1)
            hint = f" Did you mean '{close[0]}'?" if close else ""
            raise RegistryError([
                f"Unknown kernel source '{source_id}'.{hint} "
                f"Sources for {line}: {', '.join(self.source_ids(line)) or 'none'}"
            ])
        src = sources_[source_id]
        if src["line"] != line:
            raise RegistryError([
                f"Source '{source_id}' is a {src['line']} source but the selected kernel line is {line}. "
                f"Sources for {line}: {', '.join(self.source_ids(line)) or 'none'}"
            ])
        return source_id, src

    def assessment(self, src_id: str, device: str) -> Optional[Dict[str, Any]]:
        return self.data["sources"][src_id].get("devices", {}).get(device)

    def device_status(self, src_id: str, device: str) -> str:
        a = self.assessment(src_id, device)
        return a["compatibility"] if a else "not-assessed"

    def describe_status(self, src_id: str, device: str) -> str:
        a = self.assessment(src_id, device)
        if not a:
            return f"not assessed for {device}"
        return (f"{a['compatibility']} for {device} "
                f"(build: {a['build_verification']['state']}, boot: {a['boot_verification']['state']})")

    def select(self, line: str, flags: Optional[Dict[str, str]] = None, source_id: str = "") -> Dict[str, str]:
        """The source for `line` (its default unless `source_id` is given), with the branch variant that matches the active flags."""
        flags = flags or {}
        src_id, src = self.pick_source(line, source_id)
        branch, branch_label, variant_flag = src["branch"], "built-in default", ""
        for variant in src.get("branch_variants", []):
            if flags.get(variant["when_flag"], "").lower() == "true":
                branch = variant["branch"]
                branch_label = f"built-in default ({variant['label']})"
                variant_flag = variant["when_flag"]
                break
        return {
            "repo": src["repo"],
            "branch": branch,
            "kmi": self.data["lines"][line]["kmi"],
            "source_id": src_id,
            "repo_label": f"built-in default ({src['label']})",
            "branch_label": branch_label,
            "variant_flag": variant_flag,
        }


def gate(registry: Registry, kernel_version: str, source_id: str = "", device: str = "",
         allow_unverified: bool = False, custom_lines: Optional[List[str]] = None) -> Dict[str, Any]:
    """Decide which kernel lines may be built for `device`, before any expensive job starts.

    Policy (see SOURCES.md):
      confirmed           always allowed
      needs-verification  the line's default source is allowed with a warning, so today's default keeps working;
                          a source chosen explicitly needs allow_unverified
      not assessed        same as needs-verification (nothing is known about this device)
      unsupported         always blocked
      custom              a repository variable replaced the registry's repository or branch: the assessment
                          does not describe what will be built, so it is allowed with a warning
    With kernel_version "All", blocked lines are skipped with a warning as long as one line remains.
    """
    device = device or registry.default_device
    custom = set(custom_lines or [])
    errors: List[str] = []
    notes: List[tuple] = []  # (level, text)

    if device not in registry.data["devices"]:
        raise RegistryError([f"Unknown device '{device}'. Configured devices: {', '.join(registry.data['devices'])}"])

    is_all = kernel_version.lower() == "all"
    if is_all and source_id not in ("", "default"):
        raise RegistryError([
            f"KERNEL_SOURCE '{source_id}' belongs to one kernel line, so it cannot be combined with All. "
            "Select a single kernel line"
        ])
    if not is_all and kernel_version not in registry.lines:
        raise RegistryError([f"No kernel source is configured for KERNEL_VERSION '{kernel_version}'. "
                             f"Configured lines: {', '.join(registry.lines)} (see sources.json)"])

    allowed: List[str] = []
    for line in (registry.lines if is_all else [kernel_version]):
        src_id, src = registry.pick_source(line, source_id)
        explicit = source_id not in ("", "default") and src_id != registry.default_source(line)[0]
        status = registry.device_status(src_id, device)
        a = registry.assessment(src_id, device)
        label = f"{line} ({src_id})"

        if line in custom:
            notes.append(("warning", f"{label}: a repository variable replaces the registry's repository or branch, so the "
                                     f"{status} assessment of {src_id} does not describe what will be built"))
            allowed.append(line)
        elif status == "confirmed":
            allowed.append(line)
        elif status == "unsupported":
            reason = a.get("reason", "")
            msg = f"{label} is unsupported for {device}: {reason}"
            if is_all:
                notes.append(("warning", f"Skipping {msg}"))
            else:
                errors.append(msg + " Edit sources.json only with evidence (see SOURCES.md)")
        elif explicit and not allow_unverified:
            errors.append(f"{label}: {registry.describe_status(src_id, device)}. "
                          "Selecting a source that is not verified needs ALLOW_UNVERIFIED=true")
        elif status == "not-assessed":
            notes.append(("warning", f"{label}: {registry.describe_status(src_id, device)}. Nothing is known about it for this device"))
            allowed.append(line)
        else:
            notes.append(("warning", f"{label}: {registry.describe_status(src_id, device)}. "
                                     "It will build, but it is not boot-verified on the device"))
            allowed.append(line)

    if is_all and not allowed and not errors:
        errors.append(f"No kernel line is buildable for {device}")
    return {"device": device, "lines": allowed, "errors": errors, "notes": notes}


def load(path: Optional[Path] = None) -> Registry:
    path = Path(path) if path else DEFAULT_REGISTRY
    data = read_registry(path)
    problems = check_registry(data, path.name)
    if problems:
        raise RegistryError(problems)
    return Registry(data)


def main(argv: List[str]) -> int:
    parser = argparse.ArgumentParser(prog="sources.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--registry", type=Path, default=None, help="path to sources.json (default: the one in the repository root)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("validate")
    sub.add_parser("lines")
    sub.add_parser("legacy-override-line")
    sub.add_parser("default-device")
    p_sources = sub.add_parser("sources")
    p_sources.add_argument("line", nargs="?")
    p_status = sub.add_parser("status")
    p_status.add_argument("source_id")
    p_status.add_argument("device", nargs="?", default="")
    p_gate = sub.add_parser("gate")
    p_gate.add_argument("--kernel-version", required=True)
    p_gate.add_argument("--source", default="")
    p_gate.add_argument("--device", default="")
    p_gate.add_argument("--allow-unverified", default="false")
    p_gate.add_argument("--custom-lines", default="")
    p_gate.add_argument("--gha", action="store_true")
    p_field = sub.add_parser("line-field")
    p_field.add_argument("line")
    p_field.add_argument("field")
    p_select = sub.add_parser("select")
    p_select.add_argument("line")
    p_select.add_argument("--source", default="")
    p_select.add_argument("--flag", action="append", default=[], metavar="NAME=value")
    args = parser.parse_args(argv[1:])

    try:
        registry = load(args.registry)
        if args.command == "validate":
            print(f"OK: {len(registry.lines)} kernel lines, {len(registry.data['sources'])} sources")
        elif args.command == "lines":
            print(" ".join(registry.lines))
        elif args.command == "legacy-override-line":
            print(registry.legacy_override_line)
        elif args.command == "default-device":
            print(registry.default_device)
        elif args.command == "sources":
            print(" ".join(registry.source_ids(args.line)))
        elif args.command == "status":
            if args.source_id not in registry.data["sources"]:
                raise RegistryError([f"Unknown kernel source '{args.source_id}'. Known: {', '.join(registry.source_ids())}"])
            print(registry.describe_status(args.source_id, args.device or registry.default_device))
        elif args.command == "gate":
            result = gate(registry, args.kernel_version, args.source, args.device,
                          args.allow_unverified.strip().lower() == "true", args.custom_lines.split())
            for level, text in result["notes"]:
                print(f"::{level}::{text}" if args.gha else f"[!] {text}", file=sys.stderr)
            for text in result["errors"]:
                print(f"::error::{text}" if args.gha else f"[-] {text}", file=sys.stderr)
            if result["errors"]:
                return 1
            print(" ".join(result["lines"]))
        elif args.command == "line-field":
            print(registry.line_field(args.line, args.field))
        elif args.command == "select":
            flags = {}
            for item in args.flag:
                name, _, value = item.partition("=")
                flags[name] = value
            chosen = registry.select(args.line, flags, args.source)
            print("|".join(chosen[k] for k in ("repo", "branch", "kmi", "source_id", "repo_label", "branch_label", "variant_flag")))
    except RegistryError as error:
        gha = getattr(args, "gha", False)
        for problem in error.problems:
            print(f"::error::{problem}" if gha else f"[-] {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
