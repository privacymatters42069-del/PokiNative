"""Set both launcher project versions for a one-off CI test build."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROJECTS = (
    Path("launcher/KarPurLauncher.csproj"),
    Path("launcher-linux/KarPurLauncher.Linux.csproj"),
)
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:\.[0-9]+)?\Z")
VERSION_ELEMENT_RE = re.compile(r"(<Version>)[^<]*(</Version>)")


def replace_version(text: str, version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("Launcher version must be numeric, for example 0.4.6.8.")

    updated, count = VERSION_ELEMENT_RE.subn(
        lambda match: f"{match.group(1)}{version}{match.group(2)}",
        text,
    )
    if count != 1:
        raise ValueError("Expected exactly one <Version> element in each launcher project.")
    return updated


def set_launcher_version(version: str, root: Path = ROOT) -> None:
    updates: list[tuple[Path, str, Path]] = []
    for relative_path in PROJECTS:
        project = root / relative_path
        if not project.is_file():
            raise FileNotFoundError(f"Required launcher project is missing: {project}")
        text = project.read_text(encoding="utf-8")
        updates.append((project, replace_version(text, version), relative_path))

    for project, updated, relative_path in updates:
        project.write_text(updated, encoding="utf-8")
        print(f"Set {relative_path} to {version}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="numeric version, for example 0.4.6.8")
    args = parser.parse_args()
    try:
        set_launcher_version(args.version)
    except (OSError, ValueError) as error:
        parser.error(str(error))
