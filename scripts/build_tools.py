#!/usr/bin/env python3
"""Build pinned, offline-only native tools using Apple's system libraries."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.bundle_tools import BundleToolsError, bundle_tools, version_tuple


PROJECT = Path(__file__).resolve().parents[1]
SOURCES = (
    {"name": "ffmpeg", "version": "8.1.3", "filename": "ffmpeg-8.1.3.tar.xz",
     "url": "https://ffmpeg.org/releases/ffmpeg-8.1.3.tar.xz",
     "sha256": "7138d28c96d9d3e3af4ee3d8cad72741f8ffb40da90c1112235dea3ecd3178a3",
     "license": "LGPL-2.1-or-later", "license_url": "https://ffmpeg.org/legal.html"},
    {"name": "sqlcipher", "version": "4.19.0", "filename": "sqlcipher-4.19.0.tar.gz",
     "url": "https://codeload.github.com/sqlcipher/sqlcipher/tar.gz/refs/tags/v4.19.0",
     "sha256": "7075f96cbabe45b4ecfc2e6b1745a625f856f695b0827a5506ce9ed85b906aa0",
     "license": "BSD-3-Clause and SQLite public-domain dedication",
     "license_url": "https://www.zetetic.net/sqlcipher/license/"},
)
MAX_DOWNLOAD = 100 * 1024 * 1024
MAX_SOURCE = 1024 ** 3


class BuildToolsError(RuntimeError):
    """Report reproducible build failures without leaking private directories."""


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def download_source(specification, cache):
    """Accept only the exact pinned HTTPS bytes, including cached downloads."""
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / specification["filename"]
    if target.is_symlink():
        raise BuildToolsError("A source cache entry must not be a symbolic link")
    if target.exists():
        if not target.is_file() or target.stat().st_size > MAX_DOWNLOAD or sha256(target) != specification["sha256"]:
            raise BuildToolsError("A cached upstream source failed its pinned SHA-256 check")
        return target
    if not specification["url"].startswith("https://"):
        raise BuildToolsError("Upstream source downloads require HTTPS")
    with tempfile.NamedTemporaryFile(prefix=".source-", dir=cache, delete=False) as output:
        temporary = Path(output.name)
        try:
            request = urllib.request.Request(specification["url"], headers={"User-Agent": "Shiliao-Reproducible-Build"})
            with urllib.request.urlopen(request, timeout=90) as response:
                if not response.geturl().startswith("https://"):
                    raise BuildToolsError("An upstream source redirected away from HTTPS")
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise BuildToolsError("An upstream source exceeded its download size limit")
                    output.write(chunk)
            output.flush()
            if sha256(temporary) != specification["sha256"]:
                raise BuildToolsError("An upstream source failed its pinned SHA-256 check")
            try:
                os.link(temporary, target)
            except FileExistsError:
                if target.is_symlink() or sha256(target) != specification["sha256"]:
                    raise BuildToolsError("The source cache changed during download") from None
        finally:
            temporary.unlink(missing_ok=True)
    return target


def extract_source(archive, destination):
    """Extract only bounded regular files and directories into a new folder."""
    destination = Path(destination)
    destination.mkdir(mode=0o700)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        total, names, top_levels = 0, set(), set()
        if len(members) > 100000:
            raise BuildToolsError("An upstream source has too many archive entries")
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or member.name in names or not (member.isfile() or member.isdir())
                    or member.size < 0):
                raise BuildToolsError("An upstream source contains an unsafe archive entry")
            names.add(member.name)
            top_levels.add(path.parts[0])
            total += member.size
            if total > MAX_SOURCE:
                raise BuildToolsError("An upstream source exceeds its extracted size limit")
        if len(top_levels) != 1:
            raise BuildToolsError("An upstream source must have one top-level directory")
        for member in members:
            target = destination / member.name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                with source.extractfile(member) as inp, target.open("xb") as out:
                    shutil.copyfileobj(inp, out)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return destination / next(iter(top_levels))


def ffmpeg_arguments(arch, min_macos):
    return ["./configure", "--prefix=/shiliao-tools", "--cc=/usr/bin/clang",
            "--arch=" + {"arm64": "aarch64", "x86_64": "x86_64"}[arch], "--target-os=darwin",
            "--disable-autodetect", "--disable-network", "--disable-doc", "--disable-debug",
            "--disable-ffplay", "--disable-gpl", "--disable-nonfree", "--disable-version3",
            "--disable-shared", "--enable-static", "--disable-x86asm", "--enable-zlib", "--enable-bzlib",
            f"--extra-cflags=-mmacosx-version-min={min_macos} -g0",
            f"--extra-ldflags=-mmacosx-version-min={min_macos}"]


def sqlcipher_configuration(min_macos):
    flags = (f"-O2 -g0 -mmacosx-version-min={min_macos} -DSQLITE_HAS_CODEC -DSQLCIPHER_CRYPTO_CC "
             "-DSQLITE_TEMP_STORE=2 -DSQLITE_EXTRA_INIT=sqlcipher_extra_init "
             "-DSQLITE_EXTRA_SHUTDOWN=sqlcipher_extra_shutdown")
    return (["./configure", "--disable-tcl", "--disable-shared", "--disable-readline",
             "--disable-load-extension", "--with-tempstore=yes", "--fts5", "--prefix=/shiliao-tools"],
            {"CFLAGS": flags, "LDFLAGS": f"-mmacosx-version-min={min_macos} -framework Security -framework CoreFoundation"})


def build_command(arguments, cwd, environment, log):
    result = subprocess.run(arguments, cwd=cwd, env=environment, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        raise BuildToolsError("The isolated native build failed: " + Path(arguments[0]).name)


def smoke_tools(folder):
    """Exercise native crypto and media with synthetic data and no network."""
    folder = Path(folder)
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"}

    def execute(name, arguments, data=None):
        result = subprocess.run([str(folder / "bin" / name), *arguments], input=data, capture_output=True,
                                env=environment, timeout=45)
        if result.returncode:
            raise BuildToolsError("A relocated native tool failed its synthetic smoke test: " + name)
        return result.stdout

    version = execute("ffmpeg", ["-version"]).decode()
    if "--enable-gpl" in version or "--enable-nonfree" in version or "--disable-network" not in version:
        raise BuildToolsError("FFmpeg's compiled feature policy differs from the pinned build")
    protocols = execute("ffmpeg", ["-hide_banner", "-protocols"]).decode()
    if any(line.strip() in {"http", "https", "tcp", "udp", "tls", "ftp"} for line in protocols.splitlines()):
        raise BuildToolsError("FFmpeg unexpectedly includes a network protocol")
    with tempfile.TemporaryDirectory(prefix="shiliao-tools-smoke-") as temporary:
        root = Path(temporary)
        encrypted = root / "synthetic.db"
        sql = ("PRAGMA key='synthetic-toolchain-only';\nPRAGMA cipher_provider;\n"
               "CREATE TABLE synthetic(value INTEGER); INSERT INTO synthetic VALUES(7);\n")
        result = execute("sqlcipher", ["-batch", "-bail", "-init", os.devnull, str(encrypted)], sql.encode())
        if b"commoncrypto" not in result or encrypted.read_bytes().startswith(b"SQLite format 3"):
            raise BuildToolsError("SQLCipher did not use CommonCrypto encryption")
        result = execute("sqlcipher", ["-batch", "-bail", "-init", os.devnull, str(encrypted)],
                         b"PRAGMA key='synthetic-toolchain-only'; SELECT value FROM synthetic;\n")
        if result.strip().splitlines()[-1:] != [b"7"]:
            raise BuildToolsError("SQLCipher did not read its synthetic encrypted database")
        flac = root / "synthetic.flac"
        execute("ffmpeg", ["-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=duration=0.1",
                           "-c:a", "flac", str(flac)])
        info = json.loads(execute("ffprobe", ["-v", "error", "-show_streams", "-of", "json", str(flac)]))
        if info["streams"][0]["codec_name"] != "flac":
            raise BuildToolsError("FFmpeg did not preserve the synthetic FLAC audio")
        video = root / "synthetic.mp4"
        execute("ffmpeg", ["-nostdin", "-v", "error", "-f", "lavfi", "-i", "color=size=16x16:duration=0.1",
                           "-c:v", "mpeg4", str(video)])
        execute("ffmpeg", ["-nostdin", "-v", "error", "-i", str(video), "-frames:v", "1", str(root / "synthetic.png")])


def assemble_tools(source_folders, archives, output, arch, min_macos):
    """Assemble compiled tools and the exact upstream sources used for them."""
    with tempfile.TemporaryDirectory(prefix="shiliao-tools-input-") as temporary:
        stage = Path(temporary)
        (stage / "bin").mkdir()
        for name in ("ffmpeg", "ffprobe"):
            shutil.copyfile(source_folders["ffmpeg"] / name, stage / "bin" / name)
        shutil.copyfile(source_folders["sqlcipher"] / "sqlite3", stage / "bin" / "sqlcipher")
        for path in (stage / "bin").iterdir():
            path.chmod(0o755)
        license_root, sources_root = stage / "licenses", stage / "sources"
        license_root.mkdir()
        sources_root.mkdir()
        for specification in SOURCES:
            name = specification["name"]
            license_folder = license_root / name
            license_folder.mkdir()
            for candidate in sorted(source_folders[name].iterdir()):
                if candidate.is_file() and candidate.name.upper().startswith(("LICENSE", "COPYING")):
                    shutil.copyfile(candidate, license_folder / candidate.name)
            if not any(license_folder.iterdir()):
                raise BuildToolsError("An upstream component has no redistributable license file")
            shutil.copyfile(archives[name], sources_root / specification["filename"])
        for script in ("build_tools.py", "bundle_tools.py"):
            shutil.copyfile(PROJECT / "scripts" / script, sources_root / script)
        sqlite_arguments, sqlite_flags = sqlcipher_configuration(min_macos)
        metadata = {"architecture": arch, "minimum_macos": min_macos, "sources": SOURCES,
                    "upstream_source_changes": "None; only compiled Mach-O load paths and code signatures are changed when bundling.",
                    "ffmpeg_configure": ffmpeg_arguments(arch, min_macos),
                    "sqlcipher_configure": sqlite_arguments, "sqlcipher_environment": sqlite_flags,
                    "build_environment": {"MACOSX_DEPLOYMENT_TARGET": min_macos, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}}
        for folder in (license_root, sources_root):
            (folder / "BUILD-MANIFEST.json").write_text(json.dumps(metadata, indent=2) + "\n")
        (license_root / "README.txt").write_text(
            "此应用随附 FFmpeg（LGPL-2.1-or-later）及 SQLCipher Community Edition（BSD-3-Clause）。\n"
            "FFmpeg 未启用 GPL、nonfree、网络协议或外部编解码库；SQLCipher 使用 Apple CommonCrypto。\n"
            "各上游完整许可证在对应子目录。Apple 系统库由 macOS 提供，不随应用复制。\n"
            "与这些二进制对应的完整源码、固定 SHA-256 和构建脚本随同一 Release 的第三方源码 ZIP 提供。\n"
            "这些第三方许可证只适用于对应组件，不代替拾聊项目自身的授权。\n")
        (sources_root / "README.txt").write_text(
            "本目录包含随发布二进制分发的完整上游源码归档及重建配置，未修改上游源码。\n"
            "在 macOS 15 或更高系统安装 Xcode Command Line Tools 和 Python 3.10+，按架构原生重建：\n"
            "将 build_tools.py、bundle_tools.py 放入项目的 scripts 目录后运行：\n"
            f"python3 scripts/build_tools.py --arch {arch} --min-macos {min_macos} --output rebuilt-tools --cache source-cache\n"
            "把本目录的上游源码归档复制到 source-cache，可避免重新下载。完整构建选项见 BUILD-MANIFEST.json。\n")
        source_files = [{"path": path.name, "sha256": sha256(path)}
                        for path in sorted(sources_root.iterdir()) if path.is_file()]
        (sources_root / "SOURCES.json").write_text(json.dumps({"version": 1, "files": source_files}, indent=2) + "\n")
        report = bundle_tools(stage, output, arch, min_macos, include_sources=True)
    try:
        smoke_tools(output)
    except BaseException:
        shutil.rmtree(output)
        raise
    return report


def build_tools(output, arch=None, min_macos="15.0", cache=None, jobs=None):
    arch = arch or platform.machine()
    if platform.system() != "Darwin" or arch not in {"arm64", "x86_64"} or arch != platform.machine():
        raise BuildToolsError("Build each architecture natively on its matching macOS runner")
    version_tuple(min_macos)
    output = Path(output).expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise BuildToolsError("The native tools output already exists")
    jobs = jobs or min(os.cpu_count() or 2, 8)
    if not 1 <= jobs <= 64:
        raise BuildToolsError("Build parallelism must be between 1 and 64")
    cache = Path(cache or PROJECT / "dist/portable-cache")
    archives = {source["name"]: download_source(source, cache) for source in SOURCES}
    environment = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C", "MACOSX_DEPLOYMENT_TARGET": min_macos}
    with tempfile.TemporaryDirectory(prefix="shiliao-native-tools-") as temporary:
        root = Path(temporary)
        folders = {name: extract_source(path, root / name) for name, path in archives.items()}
        with (root / "build.log").open("w") as log:
            print("Building FFmpeg from pinned source", flush=True)
            build_command(ffmpeg_arguments(arch, min_macos), folders["ffmpeg"], environment, log)
            build_command(["/usr/bin/make", f"-j{jobs}", "ffmpeg", "ffprobe"], folders["ffmpeg"], environment, log)
            print("Building SQLCipher with CommonCrypto", flush=True)
            arguments, crypto_environment = sqlcipher_configuration(min_macos)
            build_command(arguments, folders["sqlcipher"], {**environment, **crypto_environment}, log)
            build_command(["/usr/bin/make", f"-j{jobs}", "sqlite3"], folders["sqlcipher"], {**environment, **crypto_environment}, log)
        print("Auditing, relocating and testing native tools", flush=True)
        return assemble_tools(folders, archives, output, arch, min_macos)


def main():
    parser = argparse.ArgumentParser(description="Build pinned native FFmpeg and SQLCipher for macOS")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--arch", choices=("arm64", "x86_64"), default=platform.machine())
    parser.add_argument("--min-macos", default="15.0")
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--jobs", type=int)
    args = parser.parse_args()
    try:
        report = build_tools(args.output, args.arch, args.min_macos, args.cache, args.jobs)
    except (BuildToolsError, BundleToolsError, OSError) as error:
        message = str(error) if isinstance(error, (BuildToolsError, BundleToolsError)) else "Native tool build failed during local I/O or download"
        raise SystemExit(message) from None
    print(json.dumps({"ok": True, "architecture": args.arch, "files": len(report["files"])}))


if __name__ == "__main__":
    main()
