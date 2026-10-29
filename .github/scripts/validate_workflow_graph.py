#!/usr/bin/env python3
"""Check that a workflow's jobs, inputs and outputs actually line up, before GitHub finds out at run time.

validate_workflows.py only checks the shape of the YAML. This checks what the expressions point at:

  * every `needs` names an existing job, and the jobs do not wait on each other in a circle
  * `needs.<job>.outputs.<name>` and `needs.<job>.result` only use a job that is listed in `needs`, and the output exists
  * `steps.<id>.outputs.<name>` uses a step id that exists in the same job
  * `inputs.<name>` / `github.event.inputs.<name>` is an input the workflow defines
  * a workflow_dispatch form has at most 25 inputs, and every default is valid for its type
  * `uses: ./path` points at a local action that exists
"""

import pathlib
import re
import sys
from typing import Any, Dict, Iterator, List

import yaml

WORKFLOW_DIR = pathlib.Path(".github/workflows")
MAX_DISPATCH_INPUTS = 25

NEEDS_RE = re.compile(r"(?<![\w.])needs\.([A-Za-z0-9_-]+)(?:\.(outputs)\.([A-Za-z0-9_-]+)|\.result)?")
STEPS_RE = re.compile(r"(?<![\w.])steps\.([A-Za-z0-9_-]+)\.(?:outputs|outcome|conclusion)")
INPUTS_RE = re.compile(r"(?<![\w])(?:github\.event\.)?inputs\.([A-Za-z0-9_]+)")


def strings(node: Any) -> Iterator[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from strings(value)


def triggers(data: Dict[str, Any]) -> Dict[str, Any]:
    on = data.get("on", data.get(True, {}))  # PyYAML reads the bare key `on` as the boolean True
    return on if isinstance(on, dict) else {}


def defined_inputs(data: Dict[str, Any]) -> Dict[str, Any]:
    found: Dict[str, Any] = {}
    for trigger in ("workflow_dispatch", "workflow_call"):
        spec = triggers(data).get(trigger)
        if isinstance(spec, dict) and isinstance(spec.get("inputs"), dict):
            found.update(spec["inputs"])
    return found


def needs_of(job: Dict[str, Any]) -> List[str]:
    needs = job.get("needs", [])
    return [needs] if isinstance(needs, str) else list(needs or [])


def find_cycle(jobs: Dict[str, Any]):
    state: Dict[str, int] = {}

    def visit(name, path):
        if state.get(name) == 2:
            return None
        if state.get(name) == 1:
            return path[path.index(name):] + [name]
        state[name] = 1
        for dep in needs_of(jobs.get(name) or {}):
            if dep in jobs:
                cycle = visit(dep, path + [name])
                if cycle:
                    return cycle
        state[name] = 2
        return None

    for job in jobs:
        cycle = visit(job, [])
        if cycle:
            return cycle
    return None


def check_workflow(data: Dict[str, Any], name: str, root: pathlib.Path = pathlib.Path(".")) -> List[str]:
    errors: List[str] = []
    jobs = data.get("jobs")
    if not isinstance(jobs, dict):
        return [f"{name}: no jobs"]
    inputs = defined_inputs(data)

    # dispatch form
    dispatch = triggers(data).get("workflow_dispatch")
    if isinstance(dispatch, dict) and isinstance(dispatch.get("inputs"), dict):
        form = dispatch["inputs"]
        if len(form) > MAX_DISPATCH_INPUTS:
            errors.append(f"{name}: the workflow_dispatch form has {len(form)} inputs, GitHub allows {MAX_DISPATCH_INPUTS}")
        for key, spec in form.items():
            spec = spec or {}
            kind, default = spec.get("type", "string"), spec.get("default")
            if kind == "choice":
                options = spec.get("options") or []
                if not options:
                    errors.append(f"{name}: input {key} is a choice without options")
                elif default is not None and default not in options:
                    errors.append(f"{name}: input {key} has default {default!r}, which is not one of its options {options}")
            elif kind == "boolean" and default is not None and not isinstance(default, bool):
                errors.append(f"{name}: input {key} is a boolean but its default is {default!r}")

    # job graph
    for job_name, job in jobs.items():
        for dep in needs_of(job):
            if dep not in jobs:
                errors.append(f"{name}: job '{job_name}' needs '{dep}', which does not exist")
    cycle = find_cycle(jobs)
    if cycle:
        errors.append(f"{name}: jobs wait on each other in a circle: {' -> '.join(cycle)}")

    # references inside each job
    for job_name, job in jobs.items():
        if not isinstance(job, dict):
            continue
        direct = set(needs_of(job))
        step_ids = {s.get("id") for s in job.get("steps", []) if isinstance(s, dict) and s.get("id")}
        for text in strings(job):
            for m in NEEDS_RE.finditer(text):
                target, is_output, output = m.group(1), m.group(2), m.group(3)
                if target not in jobs:
                    errors.append(f"{name}: job '{job_name}' uses needs.{target}, but there is no job '{target}'")
                elif target not in direct:
                    errors.append(f"{name}: job '{job_name}' uses needs.{target} without listing '{target}' in its needs")
                elif is_output and output not in (jobs[target].get("outputs") or {}):
                    errors.append(f"{name}: job '{job_name}' uses needs.{target}.outputs.{output}, "
                                  f"but job '{target}' declares no output '{output}'")
            for m in STEPS_RE.finditer(text):
                if m.group(1) not in step_ids:
                    errors.append(f"{name}: job '{job_name}' uses steps.{m.group(1)}, but it has no step with that id")
            for m in INPUTS_RE.finditer(text):
                if m.group(1) not in inputs:
                    errors.append(f"{name}: job '{job_name}' uses input '{m.group(1)}', which the workflow does not define")

        for step in job.get("steps", []) or []:
            uses = step.get("uses") if isinstance(step, dict) else None
            if isinstance(uses, str) and uses.startswith("./"):
                action = root / uses
                if not (action / "action.yml").exists() and not (action / "action.yaml").exists():
                    errors.append(f"{name}: job '{job_name}' uses the local action {uses}, but it has no action.yml")
    return errors


def main() -> int:
    errors: List[str] = []
    files = sorted(list(WORKFLOW_DIR.glob("*.yml")) + list(WORKFLOW_DIR.glob("*.yaml")))
    for path in files:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            errors.append(f"{path.name}: invalid YAML: {error}")
            continue
        errors.extend(check_workflow(data or {}, path.name))
    for error in errors:
        print(error)
    if errors:
        return 1
    print(f"Checked the job graph and references of {len(files)} workflow file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
