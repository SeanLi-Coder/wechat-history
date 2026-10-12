"""Install a private, project-owned macOS launchd backup job."""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
import tempfile


LABEL = "com.shiliao.backup"
LAUNCHCTL = "/bin/launchctl"
BUNDLE_ID = "local.shiliao.archive"
MACHO_HEADERS = {b"\xcf\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xce",
                 b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xca\xfe\xba\xbf", b"\xbf\xba\xfe\xca"}


class Scheduler:
    def __init__(self, state_dir: Path, project_dir: Path, agents_dir: Path | None = None):
        self.state_dir = Path(state_dir).expanduser().resolve()
        self.project_dir = Path(project_dir).expanduser().resolve()
        self.agents_dir = (
            Path(agents_dir).expanduser().resolve()
            if agents_dir is not None
            else Path.home() / "Library" / "LaunchAgents"
        )
        self.plist_path = self.agents_dir / f"{LABEL}.plist"
        self.domain = f"gui/{os.getuid()}"
        self.target = f"{self.domain}/{LABEL}"

    def _bundle_launcher(self) -> Path | None:
        resources = self.project_dir.parent
        contents = resources.parent
        if self.project_dir.name != "app" or resources.name != "Resources" or contents.name != "Contents":
            return None
        try:
            with (contents / "Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            launcher = contents / "MacOS/Shiliao"
            if not isinstance(info, dict) or info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("CFBundleExecutable") != "Shiliao":
                return None
            if launcher.is_symlink() or not launcher.is_file() or not os.access(launcher, os.X_OK):
                return None
            with launcher.open("rb") as stream:
                if stream.read(4) not in MACHO_HEADERS:
                    return None
            return launcher
        except (OSError, ValueError, plistlib.InvalidFileException):
            return None

    def _arguments(self) -> list[str]:
        launcher = self._bundle_launcher()
        if launcher is not None:
            return [str(launcher), "--scheduled", "--state-dir", str(self.state_dir)]
        return [sys.executable, str(self.project_dir / "app.py"), "--scheduled", "--state-dir", str(self.state_dir)]

    def _owns_arguments(self, arguments: object) -> bool:
        if not isinstance(arguments, list) or len(arguments) not in {4, 5}:
            return False
        if not all(isinstance(value, str) for value in arguments):
            return False
        try:
            if len(arguments) == 4:
                launcher = self._bundle_launcher()
                return (launcher is not None and arguments[1:3] == ["--scheduled", "--state-dir"]
                        and Path(arguments[0]).is_absolute() and Path(arguments[3]).is_absolute()
                        and Path(arguments[0]).resolve() == launcher
                        and Path(arguments[3]).resolve() == self.state_dir)
            return (
                arguments[2:4] == ["--scheduled", "--state-dir"]
                and Path(arguments[1]).is_absolute()
                and Path(arguments[4]).is_absolute()
                and Path(arguments[1]).resolve() == self.project_dir / "app.py"
                and Path(arguments[4]).resolve() == self.state_dir
            )
        except (OSError, ValueError):
            return False

    def _configuration(self) -> dict:
        configuration = {
            "Label": LABEL,
            "ProgramArguments": self._arguments(),
            "WorkingDirectory": str(self.project_dir),
            "StartInterval": 900,
            "RunAtLoad": True,
            "StandardOutPath": str(self.state_dir / "logs" / "scheduler.stdout.log"),
            "StandardErrorPath": str(self.state_dir / "logs" / "scheduler.stderr.log"),
            "Umask": 0o077,
        }
        if self._bundle_launcher() is not None:
            configuration["AssociatedBundleIdentifiers"] = [BUNDLE_ID]
        return configuration

    def _existing(self) -> tuple[bytes | None, dict | None]:
        try:
            info = self.plist_path.lstat()
        except FileNotFoundError:
            return None, None
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise RuntimeError("The scheduler plist is not a regular, supported file; it was not changed.")
        try:
            raw = self.plist_path.read_bytes()
            configuration = plistlib.loads(raw)
        except (OSError, ValueError, plistlib.InvalidFileException) as exc:
            raise RuntimeError("The existing scheduler plist cannot be verified; it was not changed.") from exc
        if (
            not isinstance(configuration, dict)
            or configuration.get("Label") != LABEL
            or not self._owns_arguments(configuration.get("ProgramArguments"))
        ):
            raise RuntimeError("Another project or state directory owns the scheduler label; it was not changed.")
        return raw, configuration

    def _run(self, *arguments: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [LAUNCHCTL, *arguments], capture_output=True, text=True, timeout=15, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"launchctl is unavailable or did not respond: {exc}") from exc

    @staticmethod
    def _missing(result: subprocess.CompletedProcess) -> bool:
        message = f"{result.stdout or ''}\n{result.stderr or ''}".lower()
        return result.returncode == 113 or "could not find service" in message or "could not find specified service" in message

    @staticmethod
    def _failure(action: str, result: subprocess.CompletedProcess) -> RuntimeError:
        detail = (result.stderr or result.stdout or f"exit code {result.returncode}").strip()[:1000]
        return RuntimeError(f"launchctl {action} failed: {detail}")

    def _loaded(self) -> list[str] | None:
        result = self._run("print", self.target)
        if result.returncode:
            if self._missing(result):
                return None
            raise self._failure("print", result)
        lines = result.stdout.splitlines()
        arguments = []
        in_arguments = False
        for line in lines:
            value = line.strip()
            if not in_arguments:
                if value == "arguments = {":
                    in_arguments = True
                continue
            if value == "}":
                break
            arguments.append(value)
        if not self._owns_arguments(arguments):
            raise RuntimeError("The loaded scheduler belongs to another project or could not be verified; it was not changed.")
        return arguments

    def _result(self, installed: bool, loaded: bool, error: str | None = None) -> dict:
        return {"installed": installed, "loaded": loaded, "error": error, "plist_path": str(self.plist_path)}

    def status(self) -> dict:
        try:
            _, existing = self._existing()
            loaded = self._loaded()
            error = None
            if existing is not None and loaded is None:
                error = "The scheduler plist exists, but its launchd job is not loaded. Enable the schedule again to repair it."
            elif existing is None and loaded is not None:
                error = "The scheduler is loaded without its plist. Enable the schedule again to repair it."
            elif existing is not None and loaded is not None and self._bundle_launcher() is not None:
                if (loaded != self._arguments() or existing.get("AssociatedBundleIdentifiers") != [BUNDLE_ID]):
                    error = "The scheduler uses an older application launcher. Save the schedule again to update it."
            return self._result(existing is not None and loaded is not None, loaded is not None, error)
        except (RuntimeError, OSError) as exc:
            return self._result(False, False, str(exc))

    @contextmanager
    def _lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state_dir.chmod(0o700)
        self.agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.agents_dir / f".{LABEL}.lock"
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _write(self, raw: bytes):
        self.agents_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix=f".{LABEL}.", dir=self.agents_dir, delete=False) as stream:
                temporary_path = Path(stream.name)
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, self.plist_path)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    def _bootout(self):
        result = self._run("bootout", self.target)
        if result.returncode and not self._missing(result):
            raise self._failure("bootout", result)

    def _bootstrap(self):
        result = self._run("bootstrap", self.domain, str(self.plist_path))
        if result.returncode:
            raise self._failure("bootstrap", result)

    def install(self) -> dict:
        try:
            with self._lock():
                if not (self.project_dir / "app.py").is_file():
                    raise RuntimeError("The application entry point app.py does not exist.")
                previous_raw, previous = self._existing()
                loaded = self._loaded()
                configuration = self._configuration()
                logs = self.state_dir / "logs"
                logs.mkdir(parents=True, exist_ok=True, mode=0o700)
                logs.chmod(0o700)
                if previous == configuration and loaded == self._arguments():
                    self.plist_path.chmod(0o600)
                    return self._result(True, True)
                if loaded is not None:
                    self._bootout()
                try:
                    self._write(plistlib.dumps(configuration))
                    self._bootstrap()
                    if self._loaded() != self._arguments():
                        raise RuntimeError("The scheduler did not load with the requested program arguments.")
                except (RuntimeError, OSError) as exc:
                    rollback_error = None
                    try:
                        if self._loaded() is not None:
                            self._bootout()
                        self._existing()
                        if previous_raw is None:
                            self.plist_path.unlink(missing_ok=True)
                        else:
                            self._write(previous_raw)
                            if loaded is not None:
                                self._bootstrap()
                    except (RuntimeError, OSError) as rollback:
                        rollback_error = str(rollback)
                    if rollback_error:
                        raise RuntimeError(f"{exc} Rollback also failed: {rollback_error}") from exc
                    raise
                return self._result(True, True)
        except OSError as exc:
            raise RuntimeError(f"Could not install the backup scheduler: {exc}") from exc

    def uninstall(self) -> dict:
        try:
            with self._lock():
                _, previous = self._existing()
                loaded = self._loaded()
                if loaded is not None:
                    self._bootout()
                if previous is not None:
                    self._existing()
                    self.plist_path.unlink()
                return self._result(False, False)
        except OSError as exc:
            raise RuntimeError(f"Could not uninstall the backup scheduler: {exc}") from exc
