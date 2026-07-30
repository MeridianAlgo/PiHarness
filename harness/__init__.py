"""PiHarness — import programs from GitHub and keep them running on a Pi."""
from pathlib import Path

_version_file = Path(__file__).parent.parent / "VERSION"
__version__ = _version_file.read_text().strip() if _version_file.exists() else "dev"
