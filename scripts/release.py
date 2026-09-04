"""Create and push a SetTag release commit and tag."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "src" / "settag" / "__init__.py"
SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
VERSION_LINE = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)


class ReleaseError(RuntimeError):
    """A release precondition was not met."""


def parse_version(version: str) -> tuple[int, int, int]:
    """Parse the simple semantic versions used for SetTag releases."""
    match = SEMVER.fullmatch(version)
    if match is None:
        raise ReleaseError(f"Expected a version like 1.2.3, found {version!r}.")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def bump_version(version: str, bump: str) -> str:
    """Return the next major, minor, or patch version."""
    major, minor, patch = parse_version(version)
    if bump == "major":
        return f"{major + 1}.0.0"
    if bump == "minor":
        return f"{major}.{minor + 1}.0"
    if bump == "patch":
        return f"{major}.{minor}.{patch + 1}"
    raise ReleaseError(f"Unknown bump {bump!r}; choose patch, minor, or major.")


def run(*args: str) -> None:
    """Run a command from the repository root."""
    subprocess.run(args, cwd=ROOT, check=True)


def output(*args: str) -> str:
    """Run a command and return its stripped standard output."""
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
        text=True,
    )
    return result.stdout.strip()


def current_version() -> str:
    """Read the package version without importing the package."""
    match = VERSION_LINE.search(VERSION_FILE.read_text(encoding="utf-8"))
    if match is None:
        raise ReleaseError(f"Could not find __version__ in {VERSION_FILE.relative_to(ROOT)}.")
    version = match.group(1)
    parse_version(version)
    return version


def assert_release_ready(version: str) -> None:
    """Check that this checkout has a safe, known release base."""
    branch = output("git", "branch", "--show-current")
    if branch != "main":
        raise ReleaseError(f"Releases must be made from main, not {branch or 'detached HEAD'}.")

    if output("git", "status", "--porcelain"):
        raise ReleaseError("The worktree is not clean; commit or stash changes first.")

    run(
        "git",
        "fetch",
        "origin",
        "refs/heads/main:refs/remotes/origin/main",
        "--tags",
    )
    head = output("git", "rev-parse", "HEAD")
    remote_main = output("git", "rev-parse", "refs/remotes/origin/main")
    if head != remote_main:
        raise ReleaseError("main must match origin/main; push or pull regular commits first.")

    tags = set(output("git", "tag", "--list", "v*").splitlines())
    current_tag = f"v{version}"
    if current_tag not in tags:
        raise ReleaseError(f"Current package version {version} has no matching {current_tag} tag.")

    released = [parse_version(tag.removeprefix("v")) for tag in tags if SEMVER.fullmatch(tag[1:])]
    if not released or parse_version(version) != max(released):
        raise ReleaseError(f"Package version {version} is not the latest release tag.")


def write_version(old_version: str, new_version: str) -> None:
    """Replace the single source-of-truth package version."""
    source = VERSION_FILE.read_text(encoding="utf-8")
    old_line = f'__version__ = "{old_version}"'
    if source.count(old_line) != 1:
        raise ReleaseError(
            f"Expected exactly one {old_line!r} in {VERSION_FILE.relative_to(ROOT)}."
        )
    VERSION_FILE.write_text(
        source.replace(old_line, f'__version__ = "{new_version}"'), encoding="utf-8"
    )


def confirm(version: str, *, assume_yes: bool) -> None:
    """Require an explicit confirmation before creating or publishing anything."""
    if assume_yes:
        return
    try:
        answer = input(f"Release v{version} to PyPI and GitHub? [y/N] ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in {"y", "yes"}:
        raise ReleaseError("Release cancelled.")


def release(*, bump: str, assume_yes: bool) -> None:
    """Validate, version, commit, tag, and atomically push a release."""
    old_version = current_version()
    new_version = bump_version(old_version, bump)
    assert_release_ready(old_version)
    confirm(new_version, assume_yes=assume_yes)

    run("make", "check")
    if output("git", "status", "--porcelain"):
        raise ReleaseError("The checks changed the worktree; inspect the changes before releasing.")

    write_version(old_version, new_version)
    run("git", "diff", "--check")
    run("git", "add", str(VERSION_FILE.relative_to(ROOT)))
    run("git", "commit", "-m", f"Prepare v{new_version} release")

    tag = f"v{new_version}"
    run("git", "tag", "-a", tag, "-m", f"SetTag {tag}")
    run("git", "push", "--atomic", "origin", "main", tag)
    print(f"Pushed {tag}; the GitHub release workflow is now running.")


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bump", choices=("patch", "minor", "major"), default="patch")
    parser.add_argument("--yes", action="store_true", help="skip the release confirmation")
    return parser.parse_args()


def main() -> int:
    """Run the release command."""
    args = parse_args()
    try:
        release(bump=args.bump, assume_yes=args.yes)
    except (ReleaseError, subprocess.CalledProcessError) as error:
        print(f"release failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
