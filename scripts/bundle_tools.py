#!/usr/bin/env python3
"""Copy, relocate, audit and sign native tools without changing their sources."""

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import stat
import subprocess
import tempfile


TOOL_NAMES = ("ffmpeg", "ffprobe", "sqlcipher")
LOAD_COMMANDS = {"LC_LOAD_DYLIB", "LC_LOAD_WEAK_DYLIB", "LC_REEXPORT_DYLIB",
                 "LC_LOAD_UPWARD_DYLIB", "LC_LAZY_LOAD_DYLIB"}
SYSTEM_PREFIXES = ("/usr/lib/", "/System/Library/")


class BundleToolsError(RuntimeError):
    """Report a fixed build error without embedding private build paths."""


def run(arguments):
    result = subprocess.run([str(value) for value in arguments], capture_output=True, text=True)
    if result.returncode:
        raise BundleToolsError("Native tool command failed: " + Path(arguments[0]).name)
    return result.stdout


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", value):
        raise BundleToolsError("The minimum macOS version must be a dotted version")
    values = tuple(int(part) for part in value.split("."))
    return values + (0,) * (3 - len(values))


@dataclass(frozen=True)
class MachO:
    dependencies: tuple
    rpaths: tuple
    install_id: str | None
    minimum_macos: str


def parse_load_commands(text):
    dependencies, rpaths, install_id, minimum = [], [], None, None
    for block in re.split(r"(?m)^Load command \d+\s*$", text)[1:]:
        match = re.search(r"(?m)^\s*cmd (LC_\w+)\s*$", block)
        if not match:
            continue
        command = match.group(1)
        if command in LOAD_COMMANDS or command == "LC_ID_DYLIB":
            entry = re.search(r"(?m)^\s*name (.+?) \(offset \d+\)", block)
            if not entry:
                raise BundleToolsError("A Mach-O load command is malformed")
            if command == "LC_ID_DYLIB":
                install_id = entry.group(1)
            else:
                dependencies.append(entry.group(1))
        elif command == "LC_RPATH":
            entry = re.search(r"(?m)^\s*path (.+?) \(offset \d+\)", block)
            if not entry:
                raise BundleToolsError("A Mach-O search path is malformed")
            rpaths.append(entry.group(1))
        elif command == "LC_BUILD_VERSION":
            target = re.search(r"(?m)^\s*platform (\S+)", block)
            entry = re.search(r"(?m)^\s*minos (\S+)", block)
            if target is None or target.group(1).lower() not in {"1", "macos"} or entry is None:
                raise BundleToolsError("A native dependency is not a macOS binary")
            minimum = entry.group(1)
        elif command == "LC_VERSION_MIN_MACOSX":
            entry = re.search(r"(?m)^\s*version (\S+)", block)
            minimum = entry.group(1) if entry else None
    if minimum is None:
        raise BundleToolsError("A native dependency has no minimum macOS version")
    version_tuple(minimum)
    return MachO(tuple(dependencies), tuple(rpaths), install_id, minimum)


def inspect_macho(path, arch, min_macos):
    if arch not in {"arm64", "x86_64"}:
        raise BundleToolsError("Only arm64 and x86_64 native builds are supported")
    if arch not in run(["/usr/bin/lipo", "-archs", path]).split():
        raise BundleToolsError("A native dependency does not contain the requested architecture")
    result = parse_load_commands(run(["/usr/bin/otool", "-arch", arch, "-l", path]))
    if version_tuple(result.minimum_macos) > version_tuple(min_macos):
        raise BundleToolsError("A native dependency requires a newer macOS version than the release")
    return result


def _expand(value, owner, executable):
    if value.startswith("@loader_path/"):
        return owner.parent / value[len("@loader_path/"):]
    if value.startswith("@executable_path/"):
        return executable.parent / value[len("@executable_path/"):]
    return Path(value) if value.startswith("/") else None


def dependency_path(value, owner, executable, search_paths):
    if value.startswith(SYSTEM_PREFIXES):
        return None
    candidates = []
    direct = _expand(value, owner, executable)
    if direct is not None:
        candidates.append(direct)
    elif value.startswith("@rpath/"):
        for folder in search_paths:
            expanded = _expand(folder, owner, executable)
            if expanded is not None:
                candidates.append(expanded / value[len("@rpath/"):])
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve(strict=True)
    raise BundleToolsError("A non-system native dependency could not be resolved")


def collect_dependencies(executables, arch, min_macos):
    nodes, edges, names = {}, {}, {}
    roots = {name: Path(path).resolve(strict=True) for name, path in executables.items()}
    if set(roots) != set(TOOL_NAMES):
        raise BundleToolsError("Exactly ffmpeg, ffprobe and sqlcipher must be bundled")

    def visit(path, executable, inherited):
        info = inspect_macho(path, arch, min_macos)
        local_paths = tuple(str(_expand(item, path, executable)) if _expand(item, path, executable) else item
                            for item in info.rpaths)
        resolved = {item: dependency_path(item, path, executable, local_paths + inherited)
                    for item in info.dependencies}
        if path in nodes:
            if edges[path] != resolved:
                raise BundleToolsError("A dependency resolves differently for two bundled tools")
            return
        nodes[path], edges[path] = info, resolved
        for dependency in resolved.values():
            if dependency is not None:
                previous = names.setdefault(dependency.name, dependency)
                if previous != dependency:
                    raise BundleToolsError("Two native dependencies have the same filename")
                visit(dependency, executable, local_paths + inherited)

    for path in roots.values():
        visit(path, path, ())
    return roots, nodes, edges


def _copy_tree(source, destination):
    for item in sorted(source.rglob("*")):
        relative = item.relative_to(source)
        if item.is_symlink():
            raise BundleToolsError("Tool distribution metadata cannot contain symbolic links")
        target = destination / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
            target.chmod(0o644)
        else:
            raise BundleToolsError("Tool distribution metadata contains a special file")


def _sign(path, identity):
    arguments = ["/usr/bin/codesign", "--force", "--sign", identity, "--identifier",
                 "local.shiliao.tools." + re.sub(r"[^A-Za-z0-9.-]", "-", path.name)]
    if identity != "-":
        arguments += ["--options", "runtime", "--timestamp"]
    run([*arguments, path])
    run(["/usr/bin/codesign", "--verify", "--strict", path])


def audit_tree(root, arch, min_macos):
    """Reject external loads, private paths and unverified native architectures."""
    root = Path(root).resolve(strict=True)
    reports = []
    for path in sorted([*root.joinpath("bin").glob("*"), *root.joinpath("lib").glob("*")]):
        if path.is_symlink() or not path.is_file():
            raise BundleToolsError("Bundled native code must be a regular file")
        info = inspect_macho(path, arch, min_macos)
        if info.rpaths:
            raise BundleToolsError("A bundled native binary retained a search path")
        for dependency in info.dependencies:
            if dependency.startswith(SYSTEM_PREFIXES):
                continue
            if not dependency.startswith("@loader_path/"):
                raise BundleToolsError("A bundled native binary retained an external dependency")
            target = _expand(dependency, path, path).resolve(strict=True)
            if not target.is_relative_to(root) or not target.is_file():
                raise BundleToolsError("A bundled dependency escapes its tools directory")
        data = path.read_bytes()
        if re.search(rb"/(?:Users|home)/[^/\x00\s]+", data):
            raise BundleToolsError("A native binary contains a private build path")
        reports.append({"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data), "minimum_macos": info.minimum_macos})
    return reports


def bundle_tools(source, output, arch=None, min_macos="15.0", *, sign_identity="-", include_sources=False):
    """Relocate an isolated native build; publish only into a new output folder.

    Source must include bin/{ffmpeg,ffprobe,sqlcipher} and reviewed licenses.
    Corresponding source archives stay in the build output unless explicitly
    requested here; the release must distribute that sources directory too.
    """
    if platform.system() != "Darwin":
        raise BundleToolsError("Native tools can only be bundled on macOS")
    source = Path(source).resolve(strict=True)
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise BundleToolsError("The tools output already exists")
    if not (source / "licenses").is_dir() or (source / "licenses").is_symlink():
        raise BundleToolsError("Reviewed native-tool licenses are required")
    arch = arch or platform.machine()
    roots, nodes, edges = collect_dependencies({name: source / "bin" / name for name in TOOL_NAMES}, arch, min_macos)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".tools-stage-", dir=output.parent) as folder:
        staging = Path(folder)
        (staging / "bin").mkdir()
        (staging / "lib").mkdir()
        destinations = {path: staging / "bin" / name for name, path in roots.items()}
        for path in nodes:
            destinations.setdefault(path, staging / "lib" / path.name)
        for path, destination in destinations.items():
            before = path.stat()
            architectures = run(["/usr/bin/lipo", "-archs", path]).split()
            if architectures != [arch]:
                run(["/usr/bin/lipo", path, "-thin", arch, "-output", destination])
            else:
                shutil.copyfile(path, destination)
            after = path.stat()
            if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                raise BundleToolsError("A native source changed during bundling")
            destination.chmod(0o755)
        for path, destination in destinations.items():
            arguments = []
            for original, dependency in edges[path].items():
                if dependency is not None:
                    relative = os.path.relpath(destinations[dependency], destination.parent)
                    arguments += ["-change", original, "@loader_path/" + relative]
            if nodes[path].install_id:
                arguments += ["-id", "@loader_path/" + destination.name]
            for rpath in nodes[path].rpaths:
                arguments += ["-delete_rpath", rpath]
            if arguments:
                run(["/usr/bin/install_name_tool", *arguments, destination])
        _copy_tree(source / "licenses", staging / "licenses")
        if include_sources:
            _copy_tree(source / "sources", staging / "sources")
        signed = set()

        def sign(path):
            if path in signed:
                return
            signed.add(path)
            for dependency in edges[path].values():
                if dependency is not None:
                    sign(dependency)
            _sign(destinations[path], sign_identity)

        for path in roots.values():
            sign(path)
        report = {"architecture": arch, "minimum_macos": min_macos, "files": audit_tree(staging, arch, min_macos)}
        (staging / "tools-manifest.json").write_text(json.dumps(report, indent=2) + "\n")
        # mkdir is the exclusive reservation; a pre-existing directory is never replaced.
        output.mkdir(mode=0o755)
        try:
            for child in staging.iterdir():
                os.rename(child, output / child.name)
        except BaseException:
            shutil.rmtree(output)
            raise
    return report


def main():
    parser = argparse.ArgumentParser(description="Relocate and sign isolated macOS native tools")
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--arch", choices=("arm64", "x86_64"), default=platform.machine())
    parser.add_argument("--min-macos", default="15.0")
    parser.add_argument("--sign-identity", default="-")
    args = parser.parse_args()
    try:
        result = bundle_tools(args.source, args.output, arch=args.arch, min_macos=args.min_macos, sign_identity=args.sign_identity)
    except (BundleToolsError, OSError):
        raise SystemExit("Native tools could not be bundled; inspect the isolated build.") from None
    print(json.dumps({"ok": True, "architecture": result["architecture"], "file_count": len(result["files"])}))


if __name__ == "__main__":
    main()
