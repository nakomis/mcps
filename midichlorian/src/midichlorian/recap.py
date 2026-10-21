"""Parse the PLAY RECAP and the failing task out of an ansible-playbook log."""

import re
from dataclasses import dataclass

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TASK = re.compile(r"^(?:TASK|RUNNING HANDLER) \[(.*)\]\s*(\*+\s*)?$")
_FATAL = re.compile(r"^(fatal:|.*UNREACHABLE!)")
_COUNT = re.compile(r"\b(ok|changed|unreachable|failed|skipped)=(\d+)")
_FIELDS = ("ok", "changed", "unreachable", "failed", "skipped")


@dataclass(frozen=True)
class Recap:
    ok: int | None
    changed: int | None
    unreachable: int | None
    failed: int | None
    skipped: int | None
    failing_task: str | None


def parse(log_text: str) -> Recap:
    lines = _ANSI.sub("", log_text).splitlines()
    task = None
    fatals: list[list] = []  # [task, ignored]
    in_recap = False
    totals: dict[str, int] | None = None
    for line in lines:
        if line.startswith("PLAY RECAP"):
            in_recap = True
            continue
        if in_recap:
            found = dict(_COUNT.findall(line))
            if found.keys() >= set(_FIELDS):
                totals = totals or dict.fromkeys(_FIELDS, 0)
                for f in _FIELDS:
                    totals[f] += int(found[f])
            continue
        m = _TASK.match(line)
        if m:
            task = m.group(1)
        elif _FATAL.match(line):
            fatals.append([task, False])
        elif line.strip() == "...ignoring" and fatals:
            fatals[-1][1] = True
    failing = next((t for t, ignored in fatals if not ignored), None)
    if totals and not (totals["failed"] or totals["unreachable"]):
        failing = None
    counts = totals or dict.fromkeys(_FIELDS)
    return Recap(**counts, failing_task=failing)
