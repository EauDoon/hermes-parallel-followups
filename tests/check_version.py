#!/usr/bin/env python3
"""Check that every version surface agrees, and print a release's notes.

VERSION is the single source. Each installer embeds a copy as __version__
because it has to stay a standalone drop-in script, and CHANGELOG.md records
each release under its own heading.

    python3 tests/check_version.py                # VERSION_OK <version>
    python3 tests/check_version.py --tag v1.2.3   # the tag must be v + VERSION
    python3 tests/check_version.py --notes        # that release's CHANGELOG section
"""
import argparse
import ast
import datetime
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
RELEASES = "https://github.com/EauDoon/hermes-parallel-followups/releases/tag/v"
# SemVer 2.0.0 core plus an optional prerelease. Build metadata is refused: a
# tag name should not carry it. Numeric prerelease parts are checked below.
SEMVER = re.compile(
    "(0|[1-9][0-9]*)[.](0|[1-9][0-9]*)[.](0|[1-9][0-9]*)(?:-([0-9A-Za-z-]+(?:[.][0-9A-Za-z-]+)*))?"
)
ISO_DATE = re.compile("[0-9]{4}-[0-9]{2}-[0-9]{2}")


class Mismatch(Exception):
    pass


def parse_version(text):
    """(base, prerelease or '') for a SemVer string, or None."""
    match = SEMVER.fullmatch(text)
    if not match:
        return None
    prerelease = match.group(4) or ""
    if any(part.isdigit() and len(part) > 1 and part.startswith("0") for part in prerelease.split(".")):
        return None  # SemVer forbids leading zeros in numeric identifiers
    return "%s.%s.%s" % match.group(1, 2, 3), prerelease


def read_version(root):
    lines = (root / "VERSION").read_text(encoding="utf-8").splitlines()
    if len(lines) != 1 or parse_version(lines[0]) is None:
        raise Mismatch("VERSION must hold exactly one SemVer line, found %r" % lines)
    return lines[0]


def installer_versions(root):
    versions = {}
    for script in sorted((root / "patches").glob("*.py")):
        tree = ast.parse(script.read_text(encoding="utf-8"), filename=str(script))
        values = [
            node.value.value for node in tree.body
            if isinstance(node, ast.Assign) and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name) and node.targets[0].id == "__version__"
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        ]
        if len(values) != 1:
            raise Mismatch("patches/%s must assign __version__ exactly once, as a string" % script.name)
        versions[script.name] = values[0]
    if not versions:
        raise Mismatch("no installers found under patches/")
    return versions


def changelog_sections(text):
    """{name: (date or None, body lines)} for every '## [name]' heading.

    A section ends at the next '## ' heading or at the link reference block
    ('[name]: url' lines) that closes the file.
    """
    sections = {}
    name = None
    for line in text.splitlines():
        if line.startswith("## "):
            heading = line[3:]
            label, closed, rest = heading[1:].partition("]")
            if not heading.startswith("[") or not closed or (rest and not rest.startswith(" - ")):
                raise Mismatch("CHANGELOG.md heading %r is not '## [version] - YYYY-MM-DD'" % line)
            if label in sections:
                raise Mismatch("CHANGELOG.md has two [%s] sections" % label)
            name = label
            sections[name] = (rest[3:] or None, [])
        elif line.startswith("[") and "]: " in line:
            name = None
        elif name is not None:
            sections[name][1].append(line)
    return sections


def check(root, tag=None):
    """Return (version, sections) when every surface agrees, else raise Mismatch."""
    version = read_version(root)
    for script, value in installer_versions(root).items():
        if value != version:
            raise Mismatch("patches/%s has __version__ %r but VERSION is %r" % (script, value, version))
    text = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    sections = changelog_sections(text)
    if "Unreleased" not in sections:
        raise Mismatch("CHANGELOG.md has no '## [Unreleased]' section")
    base, prerelease = parse_version(version)
    if prerelease:
        if base in sections:
            raise Mismatch("VERSION %s is a prerelease of %s, which CHANGELOG.md already records as "
                           "released" % (version, base))
    else:
        if version not in sections:
            raise Mismatch("CHANGELOG.md has no '## [%s] - YYYY-MM-DD' section" % version)
        date = sections[version][0]
        if date is None or not ISO_DATE.fullmatch(date):
            raise Mismatch("CHANGELOG.md [%s] heading needs an ISO date (YYYY-MM-DD), found %r"
                           % (version, date))
        try:
            datetime.date.fromisoformat(date)
        except ValueError:
            raise Mismatch("CHANGELOG.md [%s] date %s is not a valid date" % (version, date)) from None
        link = "[%s]: %s%s" % (version, RELEASES, version)
        if link not in text.splitlines():
            raise Mismatch("CHANGELOG.md lacks the link reference %s" % link)
    if tag is not None:
        if prerelease:
            raise Mismatch("tag %s would publish prerelease VERSION %s; only final versions are "
                           "released" % (tag, version))
        if tag != "v" + version:
            raise Mismatch("tag %s does not match VERSION %s (expected v%s)" % (tag, version, version))
    return version, sections


def release_notes(version, sections):
    if parse_version(version)[1]:
        raise Mismatch("VERSION %s is a prerelease; release notes exist only for a final version" % version)
    notes = "\n".join(sections[version][1]).strip()
    if not notes:
        raise Mismatch("CHANGELOG.md [%s] section is empty" % version)
    return notes + "\n"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--tag", help="release tag; must be v followed by a final VERSION")
    parser.add_argument("--notes", action="store_true", help="print the CHANGELOG section for VERSION")
    parser.add_argument("--root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        version, sections = check(args.root, args.tag)
        notes = release_notes(version, sections) if args.notes else None
    except (Mismatch, OSError, UnicodeError, SyntaxError) as error:
        print("VERSION_MISMATCH: %s" % error, file=sys.stderr)
        return 1
    if notes is not None:
        sys.stdout.write(notes)
    else:
        print("VERSION_OK %s" % version)
    return 0


if __name__ == "__main__":
    sys.exit(main())
