"""Reads backend/requirements.in and requirements.txt for the dependency tests.

Deliberately strict. Only the forms the file actually uses are understood:
`name`, `name[extra,...]`, optionally followed by `==version`, plus
`--hash=...` options and backslash continuations so that a hashed lock can
be read later without changes here. Anything else (other version operators,
environment markers, -r/-c includes, URLs, index options) raises, so a new
form is noticed instead of being silently skipped by the tests that rely on
this parser.
"""
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

REQUIREMENTS_PATH = Path(__file__).resolve().parent.parent / "requirements.txt"
#: The direct dependencies requirements.txt (the hashed lock) is compiled from.
REQUIREMENTS_IN_PATH = Path(__file__).resolve().parent.parent / "requirements.in"

_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[(?P<extras>[A-Za-z0-9._,\s-]+)\])?"
    r"(?:\s*==\s*(?P<version>[A-Za-z0-9.!+_-]+))?$"
)
_COMMENT = re.compile(r"(^|\s)#.*$")
_HASH_OPTION = re.compile(r"\s+--hash=\S+")


def normalize(name: str) -> str:
    """PEP 503 name normalization: case-insensitive, `-_.` runs equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def versions_equal(a: str, b: str) -> bool:
    """Equality as an exact `==` pin means it: for plain release numbers,
    trailing zero components do not matter (2.0 == 2.0.0)."""
    release = re.compile(r"^\d+(\.\d+)*$")
    if release.match(a) and release.match(b):
        def key(v):
            parts = [int(p) for p in v.split(".")]
            while len(parts) > 1 and parts[-1] == 0:
                parts.pop()
            return parts
        return key(a) == key(b)
    return a.strip().lower() == b.strip().lower()


@dataclass(frozen=True)
class DeclaredRequirement:
    name: str
    extras: Tuple[str, ...]
    pinned: Optional[str]
    line: int

    @property
    def key(self) -> str:
        return normalize(self.name)


def _logical_lines(text: str):
    """Yields (first physical line number, text) with continuations joined
    and comments removed."""
    pending, start = "", None
    for number, raw in enumerate(text.splitlines(), start=1):
        line = _COMMENT.sub("", raw).rstrip()
        if start is None:
            start = number
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        pending += line
        if pending.strip():
            yield start, pending.strip()
        pending, start = "", None
    if pending.strip():
        yield start, pending.strip()


def read_requirements(path: Path = REQUIREMENTS_PATH) -> list:
    requirements, seen = [], {}
    for number, text in _logical_lines(path.read_text(encoding="utf-8")):
        spec = _HASH_OPTION.sub("", " " + text).strip()
        match = _REQUIREMENT.match(spec)
        if spec.startswith("-") or not match:
            raise ValueError(f"{path.name}:{number}: unsupported requirement syntax: {text!r}")
        extras = tuple(e.strip() for e in (match.group("extras") or "").split(",") if e.strip())
        requirement = DeclaredRequirement(match.group("name"), extras, match.group("version"), number)
        if requirement.key in seen:
            raise ValueError(
                f"{path.name}:{number}: {requirement.name} is already declared on line {seen[requirement.key]}"
            )
        seen[requirement.key] = number
        requirements.append(requirement)
    return requirements
