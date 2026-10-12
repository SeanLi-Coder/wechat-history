#!/usr/bin/env python3
"""Build and verify a self-contained macOS application from reviewed sources."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT))
from scripts import build_app
from scripts.package_release import collect_sources, _version
from scripts.python_licenses import PYTHON_RELEASE, PYTHON_VERSION

MINIMUM_MACOS = "15.0"
PYTHON_ARCHIVES = {
    "arm64": ("aarch64", "f58d38af4680fbf9f89c329e732b7cca1bd06ff7a93884798b27d541bee138f6"),
    "x86_64": ("x86_64", "af19b09f79e784b912ac8b2c659805ac26eb5b8c7dc793151ea4dd2e3456f023"),
}
PILK_URL = "https://files.pythonhosted.org/packages/66/bb/938dd697b6bc2d851ffec4ffe82ed20078e58bd0ef049e84f4d038d6f991/pilk-0.2.4.tar.gz"
PILK_SHA256 = "d4a1bcf93dc6ef5e95e0cfd728ed4ef4d49f9c0476d70816fecbe456cc762e7f"
MACHO = {bytes.fromhex(value) for value in ("cffaedfe", "feedfacf", "cefaedfe", "feedface", "cafebabe", "bebafeca", "cafebabf", "bfbafeca")}


def run(arguments, **kwargs):
    return subprocess.run([str(value) for value in arguments], check=True, **kwargs)


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def download(url, expected, path):
    path = Path(path)
    if path.exists():
        if digest(path) != expected:
            raise ValueError("Cached dependency checksum differs from its pinned value")
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    try:
        run(["/usr/bin/curl", "--fail", "--location", "--retry", "3", "--silent", "--show-error",
             url, "--output", partial])
        if digest(partial) != expected:
            raise ValueError("Downloaded dependency failed its pinned checksum")
        os.replace(partial, path)
    finally:
        partial.unlink(missing_ok=True)
    return path


def extract_python(archive, resources):
    root = Path(resources).resolve()
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len({member.name for member in members}) != len(members):
            raise ValueError("Python archive contains duplicate members")
        for member in members:
            name = Path(member.name)
            if name.is_absolute() or ".." in name.parts or not name.parts or name.parts[0] != "python":
                raise ValueError("Python archive contains an unsafe member")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError("Python archive contains a special file")
            if member.issym() or member.islnk():
                parent = root / member.name if member.islnk() else (root / member.name).parent
                target = (root / member.linkname) if member.islnk() else (parent / member.linkname)
                if Path(member.linkname).is_absolute() or not target.resolve().is_relative_to(root / "python"):
                    raise ValueError("Python archive link escapes the runtime")
        # Create regular files before links, also on older Python 3.11 builds
        # that do not provide tarfile's extraction filter.
        for member in members:
            target = root / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as inp, target.open("xb") as out:
                    shutil.copyfileobj(inp, out)
                target.chmod(member.mode & 0o755)
        for member in members:
            target = root / member.name
            if member.issym():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(member.linkname)
            elif member.islnk():
                os.link(root / member.linkname, target)
        for path in (root / "python").rglob("*"):
            if path.is_symlink() and not path.resolve().is_relative_to(root / "python"):
                raise ValueError("Python archive contains an escaping link chain")


def macho_files(root):
    result = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as stream:
                if stream.read(4) in MACHO:
                    result.append(path)
    return result


def _version_tuple(value):
    return tuple((list(map(int, value.split("."))) + [0, 0])[:3])


def clean_native_search_paths(bundle, architecture):
    from scripts.bundle_tools import parse_load_commands
    for path in macho_files(bundle):
        loads = subprocess.check_output(["/usr/bin/otool", "-arch", architecture, "-l", str(path)], text=True)
        info = parse_load_commands(loads)
        # Wheels can retain unused build-runner rpaths. All actual loads are
        # separately checked; no dependency may rely on such an external path.
        for rpath in info.rpaths:
            if not rpath.startswith(("@loader_path", "@executable_path")):
                run(["/usr/bin/install_name_tool", "-delete_rpath", rpath, path], capture_output=True)


def audit_bundle(bundle, architecture):
    from scripts.bundle_tools import parse_load_commands
    bundle = Path(bundle).resolve()
    resources = bundle / "Contents/Resources"
    info = plistlib.loads((bundle / "Contents/Info.plist").read_bytes())
    if info.get("ShiliaoBundledPython") != "python/bin/python3" or "ShiliaoPythonExecutable" in info:
        raise ValueError("Portable application must contain a relative Python runtime")
    binaries = macho_files(bundle)
    if not binaries:
        raise ValueError("Application contains no native executables")
    maximum = (0, 0, 0)
    for path in bundle.rglob("*"):
        if path.is_symlink() and not path.resolve().is_relative_to(bundle):
            raise ValueError("Application contains a link outside its bundle")
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix == ".pyc" or "__pycache__" in path.parts:
            raise ValueError("Application contains build-specific bytecode")
        # Third-party files are inspected too, without printing matched values.
        private_home = str(Path.home()).encode()
        if os.environ.get("GITHUB_ACTIONS") == "true" and Path.home().name in {"runner", "runneradmin"}:
            # Published upstream wheels can contain their own public GitHub
            # build paths. The current checkout must still never be embedded.
            private_home = os.environ["GITHUB_WORKSPACE"].encode()
        with path.open("rb") as stream:
            previous = b""
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                block = previous + chunk
                if private_home in block:
                    raise ValueError("Application contains a private build path")
                previous = block[-max(512, len(private_home)):]
    for path in binaries:
        architectures = subprocess.check_output(["/usr/bin/lipo", "-archs", str(path)], text=True).split()
        if architecture not in architectures:
            raise ValueError("A native dependency has the wrong CPU architecture")
        loads = subprocess.check_output(["/usr/bin/otool", "-arch", architecture, "-l", str(path)], text=True)
        native = parse_load_commands(loads)
        maximum = max(maximum, _version_tuple(native.minimum_macos))
        rpaths = native.rpaths
        for rpath in rpaths:
            if not rpath.startswith(("@loader_path", "@executable_path")):
                raise ValueError("A native dependency contains an external search path")
        for value in native.dependencies:
            if value.startswith(("/System/Library/", "/usr/lib/")):
                continue
            executable = resources / "python/bin"
            def expand(item):
                return Path(item.replace("@loader_path", str(path.parent)).replace("@executable_path", str(executable)))
            candidates = ([expand(value)] if not value.startswith("@rpath/") else
                          [expand(item) / value.removeprefix("@rpath/") for item in rpaths] +
                          [resources / "python/lib" / value.removeprefix("@rpath/")])
            if not any(candidate.is_file() and candidate.resolve().is_relative_to(bundle) for candidate in candidates):
                raise ValueError("A native dependency points outside the application or is missing")
    if maximum > _version_tuple(MINIMUM_MACOS):
        raise ValueError("A native dependency requires a newer macOS version than advertised")
    return {"native_binaries": len(binaries), "architecture": architecture,
            "minimum_macos": MINIMUM_MACOS, "maximum_native_minimum": ".".join(map(str, maximum)),
            "external_non_system_dependencies": 0, "private_build_paths": 0}


def sign_bundle(bundle):
    for path in macho_files(bundle):
        run(["/usr/bin/codesign", "--force", "--sign", "-", path], capture_output=True)
    run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", build_app.BUNDLE_ID, bundle], capture_output=True)
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", bundle], capture_output=True)


def install_dependencies(python, project, work, cache, sources):
    wheels = cache / ("wheels-" + platform.machine())
    wheels.mkdir(parents=True, exist_ok=True)
    home = work / "build-home"
    home.mkdir()
    flags = "-g0 -ffile-prefix-map=" + str(work) + "=/shiliao-build -mmacosx-version-min=" + MINIMUM_MACOS
    environment = {**os.environ, "HOME": str(home), "PIP_CONFIG_FILE": os.devnull,
                   "PIP_INDEX_URL": "https://pypi.org/simple", "PIP_EXTRA_INDEX_URL": "",
                   "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
                   "MACOSX_DEPLOYMENT_TARGET": MINIMUM_MACOS, "CFLAGS": flags,
                   "CXXFLAGS": flags, "TMPDIR": str(work)}
    for key in ("PYTHONPATH", "PYTHONHOME"):
        environment.pop(key, None)
    pip = [python, "-B", "-m", "pip", "--disable-pip-version-check"]
    requirement = project / "requirements-portable.txt"
    pilk = download(PILK_URL, PILK_SHA256, cache / "pilk-0.2.4.tar.gz")
    run([*pip, "wheel", "--wheel-dir", wheels, "--no-cache-dir", "--no-deps", pilk], env=environment)
    run([*pip, "wheel", "--wheel-dir", wheels, "--find-links", wheels, "--no-cache-dir", "--index-url", "https://pypi.org/simple", "-r", requirement], env=environment)
    run([*pip, "install", "--no-compile", "--no-index", "--find-links", wheels, "-r", requirement], env=environment)
    shutil.copy2(pilk, sources / pilk.name)
    packages = json.loads(subprocess.check_output([*pip, "list", "--format=json"], env=environment))
    return {"packages": packages, "wheels": [{"filename": path.name, "sha256": digest(path)} for path in sorted(wheels.glob("*.whl"))]}


def copy_declared_sources(source, destination):
    source, destination = Path(source), Path(destination)
    if source.is_symlink() or (source / "SOURCES.json").is_symlink():
        raise ValueError("Dependency sources cannot be symbolic links")
    manifest = json.loads((source / "SOURCES.json").read_text())
    names = set()
    destination.mkdir()
    for item in manifest["files"]:
        name = Path(item["path"])
        if name.is_absolute() or ".." in name.parts or not name.parts or str(name) in names:
            raise ValueError("Dependency source manifest contains unsafe paths")
        names.add(str(name))
        path = source
        for part in name.parts:
            path /= part
            if path.is_symlink():
                raise ValueError("Dependency source paths cannot be symbolic links")
        if not path.is_file() or digest(path) != item["sha256"]:
            raise ValueError("Dependency source failed its declared checksum")
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    shutil.copyfile(source / "SOURCES.json", destination / "SOURCES.json")


def build_portable(architecture, tools, output_dir, cache):
    if sys.platform != "darwin" or platform.machine() != architecture:
        raise ValueError("Build each architecture on its matching macOS runner")
    from scripts.bundle_tools import bundle_tools
    from scripts.portable_smoke import run_smoke
    _, collected = collect_sources(PROJECT)
    version = _version(collected, None)
    output_dir, cache, tools = map(lambda p: Path(p).resolve(), (output_dir, cache, tools))
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "shiliao-" + version + "-macos-" + architecture
    if list(output_dir.glob(stem + "*")):
        raise ValueError("Portable release outputs already exist")
    target, expected = PYTHON_ARCHIVES[architecture]
    filename = f"cpython-{PYTHON_VERSION}+{PYTHON_RELEASE}-{target}-apple-darwin-install_only_stripped.tar.gz"
    url = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PYTHON_RELEASE}/{filename.replace('+', '%2B')}"
    archive = download(url, expected, cache / filename)
    with tempfile.TemporaryDirectory(prefix="shiliao-portable-", dir="/tmp") as temporary, \
            tempfile.TemporaryDirectory(prefix=".portable-assets-", dir=output_dir) as pending:
        work = Path(temporary).resolve()
        artifact_dir = Path(pending)
        project, staging = work / "source", work / "disk"
        for name, data, _ in collected:
            path = project / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        bundle = staging / "拾聊.app"
        resources = bundle / "Contents/Resources"
        resources.mkdir(parents=True)
        extract_python(archive, resources)
        python = resources / "python/bin/python3"
        sources = work / "third-party-sources"
        copy_declared_sources(tools / "sources", sources)
        dependencies = install_dependencies(python, project, work, cache, sources)
        python_license_dir = resources / "licenses/python-build-standalone"
        # The bundled runtime now provides zstandard; the build-host Python does
        # not need third-party modules just to read the pinned full archive.
        run([python, "-B", "-I", project / "scripts/python_licenses.py", "--arch", architecture,
             "--destination", python_license_dir, "--cache", cache])
        python_license_record = json.loads((python_license_dir / "LICENSES.json").read_text())
        shutil.copytree(python_license_dir, sources / "python-build-standalone")
        run([python, "-B", "-I", project / "scripts/wheel_licenses.py", "--python-root",
             resources / "python", "--cache", cache / "wheel-licenses"])
        wheel_license_dir = resources / "python/licenses/wheels"
        wheel_license_record = json.loads((wheel_license_dir / "LICENSES.json").read_text())
        shutil.copytree(wheel_license_dir, sources / "wheel-licenses")
        original_project = build_app.PROJECT
        try:
            build_app.PROJECT = project
            build_app.build_app(bundle, python, bundled_python=True, minimum_macos=MINIMUM_MACOS)
        finally:
            build_app.PROJECT = original_project
        payload = resources / "app"
        for name in ("scripts/wechat_initialize.py", "vendor/wechat-access/capture_keys.py", "vendor/wechat-access/LICENSE"):
            dest = payload / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / name, dest)
        bundle_tools(tools, resources / "tools", architecture, MINIMUM_MACOS)
        shutil.copy2(project / "THIRD_PARTY_NOTICES.md", resources / "THIRD_PARTY_NOTICES.md")
        for path in list(resources.rglob("__pycache__")):
            if path.is_dir():
                shutil.rmtree(path)
        for path in resources.rglob("*.pyc"):
            path.unlink()
        for path in resources.rglob("direct_url.json"):
            path.unlink()
        for name in ("requirements.txt", "requirements-portable.txt", "scripts/build_portable.py", "scripts/build_tools.py", "scripts/bundle_tools.py", "scripts/python_licenses.py", "scripts/wheel_licenses.py", "scripts/native_launcher.m"):
            dest = sources / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(project / name, dest)
        dependency_record = {"python": {"version": PYTHON_VERSION, "url": url, "sha256": expected,
                            "licenses": "licenses/python-build-standalone/LICENSES.json",
                            "full_distribution": python_license_record["source"]},
                            "wheel_licenses": {"manifest": "python/licenses/wheels/LICENSES.json",
                                               "packages": wheel_license_record["packages"],
                                               "files": len(wheel_license_record["files"])}, **dependencies}
        (resources / "DEPENDENCIES.json").write_text(json.dumps(dependency_record, indent=2) + "\n")
        (sources / "DEPENDENCIES.json").write_text(json.dumps(dependency_record, indent=2) + "\n")
        clean_native_search_paths(bundle, architecture)
        audit = audit_bundle(bundle, architecture)
        sign_bundle(bundle)
        smoke = run_smoke(bundle)
        if not smoke.get("ok"):
            raise ValueError("Portable application smoke check failed: " + str(smoke.get("failed_check", "unknown")))
        audit_bundle(bundle, architecture)
        application_zip = artifact_dir / (stem + ".app.zip")
        run(["/usr/bin/ditto", "-c", "-k", "--keepParent", "--norsrc", "--noextattr", bundle, application_zip])
        (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
        instructions = "将拾聊.app拖到Applications，然后从应用程序打开。\n当前使用ad-hoc签名，未经Apple公证。确认从官方Release下载后，如首次被macOS阻止，请在系统设置→隐私与安全性中选择仍要打开。\n最低系统：macOS 15。此包无需安装Python或Homebrew。\n"
        (staging / "安装说明.txt").write_text(instructions)
        dmg = artifact_dir / (stem + ".dmg")
        run(["/usr/bin/hdiutil", "create", "-volname", "拾聊 " + version, "-srcfolder", staging,
             "-format", "UDZO", "-ov", dmg], capture_output=True)
        source_zip = artifact_dir / (stem + "-third-party-sources.zip")
        with zipfile.ZipFile(source_zip, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as package:
            for path in sorted(sources.rglob("*")):
                if path.is_file():
                    package.write(path, "third-party-sources/" + path.relative_to(sources).as_posix())
        assets = [application_zip, dmg, source_zip]
        manifest = {"version": version, "architecture": architecture, "minimum_macos": MINIMUM_MACOS,
                    "signature": "ad-hoc", "notarized": False, "audit": audit, "smoke": smoke,
                    "assets": [{"filename": path.name, "bytes": path.stat().st_size, "sha256": digest(path)} for path in assets]}
        manifest_path = artifact_dir / (stem + "-manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
        for path in [*assets, manifest_path]:
            path.with_name(path.name + ".sha256").write_text(digest(path) + "  " + path.name + "\n")
        published = []
        try:
            for path in sorted(artifact_dir.iterdir()):
                target = output_dir / path.name
                os.link(path, target)
                published.append(target)
        except BaseException:
            for path in published:
                path.unlink()
            raise
        return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arch", choices=tuple(PYTHON_ARCHIVES), required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=PROJECT / "dist/installers")
    parser.add_argument("--cache", type=Path, default=PROJECT / "dist/portable-cache")
    args = parser.parse_args()
    print(json.dumps(build_portable(args.arch, args.tools, args.output_dir, args.cache)))


if __name__ == "__main__":
    main()
