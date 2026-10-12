"""Locate portable app tools without depending on a developer's installation."""

import os
from pathlib import Path
import shutil


TOOL_NAMES = frozenset({"ffmpeg", "ffprobe", "sqlcipher"})
DEVELOPMENT_TOOL_DIRS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))


def bundled_resources():
    """Find Resources from the loaded module, including after an app move."""
    module = Path(__file__).resolve()
    resources = module.parent.parent.parent
    if (module.parent.name == "vault" and module.parent.parent.name == "app"
            and resources.name == "Resources" and resources.parent.name == "Contents"
            and resources.parent.parent.suffix == ".app"):
        return resources
    return None


def _executable(path, contained_in=None):
    try:
        if contained_in is not None and not path.resolve().is_relative_to(contained_in.resolve()):
            return None
        if path.is_file() and os.access(path, os.X_OK):
            return str(path.absolute())
    except (OSError, RuntimeError):
        pass
    return None


def find_tool(name):
    """Return an absolute executable path, or None when unavailable.

    A bundled app uses only its own tools: missing or externally linked tools
    must not silently depend on Homebrew or PATH. Source runs may explicitly
    select an absolute SHILIAO_TOOLS_DIR, then use the development fallbacks.
    Tool names are allowlisted so callers cannot supply paths or commands.
    """
    if name not in TOOL_NAMES:
        raise ValueError("Unsupported local runtime tool")
    resources = bundled_resources()
    if resources is not None:
        return _executable(resources / "tools" / "bin" / name, resources / "tools")
    configured = os.environ.get("SHILIAO_TOOLS_DIR")
    if configured:
        directory = Path(configured)
        if directory.is_absolute():
            selected = _executable(directory / name)
            if selected:
                return selected
    for directory in DEVELOPMENT_TOOL_DIRS:
        selected = _executable(directory / name)
        if selected:
            return selected
    selected = shutil.which(name)
    return _executable(Path(selected)) if selected else None
