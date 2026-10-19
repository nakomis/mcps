"""Parse the PLAY RECAP and the failing task out of an ansible-playbook log."""

import re
from dataclasses import dataclass

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TASK = re.compile(r"^TASK \[(.*)\]\s*(\*+\s*)?$")
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
    task = failing = None
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
        elif failing is None and _FATAL.match(line):
            failing = task
    counts = totals or dict.fromkeys(_FIELDS)
    return Recap(**counts, failing_task=failing)
