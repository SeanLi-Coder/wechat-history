#!/usr/bin/env python3
"""Test the reviewed LLDB capture helper using only a synthetic child process."""

import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import tempfile


PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(PROJECT / "tests"))

from test_wechat_crypto import encrypt_fixture, make_sqlite


def main():
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="shiliao-synthetic-lldb-") as temporary:
        root = Path(temporary)
        plaintext_path = root / "synthetic.sqlite3"
        plaintext = make_sqlite(plaintext_path)
        plaintext_path.unlink()
        key = secrets.token_bytes(32)
        encrypted = encrypt_fixture(plaintext, key)
        database_root = root / "databases"
        database_root.mkdir(mode=0o700)
        (database_root / "message_0.db").write_bytes(encrypted)
        mac_salt = bytes(value ^ 0x3A for value in encrypted[:16])
        source = root / "synthetic_capture.c"
        source.write_text(
            "#include <CommonCrypto/CommonKeyDerivation.h>\n"
            "#include <unistd.h>\n"
            "int main(void) {\n"
            "    unsigned char key[32] = {" + ",".join(map(str, key)) + "};\n"
            "    unsigned char salt[16] = {" + ",".join(map(str, mac_salt)) + "};\n"
            "    unsigned char output[32];\n"
            "    sleep(1);\n"
            "    int result = CCKeyDerivationPBKDF(kCCPBKDF2, (const char *)key, 32,\n"
            "        salt, 16, kCCPRFHmacAlgSHA512, 2, output, 32);\n"
            "    sleep(1);\n"
            "    return result;\n"
            "}\n",
            encoding="utf-8",
        )
        executable = root / "synthetic-capture"
        compiler = subprocess.run(
            ["/usr/bin/xcrun", "clang", "-arch", "arm64", "-O0", str(source), "-o", str(executable)],
            capture_output=True, text=True, timeout=30, check=False,
        )
        if compiler.returncode:
            raise RuntimeError(f"Synthetic C compilation failed with exit code {compiler.returncode}")
        lldb = subprocess.run(["/usr/bin/lldb", "-P"], capture_output=True, text=True, timeout=10, check=False)
        if lldb.returncode or not lldb.stdout.strip():
            raise RuntimeError(f"LLDB Python discovery failed with exit code {lldb.returncode}")
        output = root / "private" / "keys.json"
        environment = dict(os.environ, PYTHONPATH=lldb.stdout.strip())
        command = [
            "/usr/bin/python3", str(PROJECT / "vendor/wechat-access/capture_keys.py"),
            "--exe", str(executable), "--db-root", str(database_root),
            "--out", str(output), "--timeout", "15",
        ]
        process = subprocess.Popen(command, env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=35)
        except subprocess.TimeoutExpired:
            # SIGINT lets the helper close its own synthetic debuggee in finally.
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
            raise RuntimeError("Synthetic LLDB capture timed out") from None
        if key.hex() in stdout or key.hex() in stderr:
            raise RuntimeError("The synthetic capture unexpectedly exposed key material in its logs")
        events = []
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if isinstance(event, dict):
                events.append(event)
        if process.returncode:
            errors = [str(event.get("error")) for event in events if event.get("type") in ("launch_failed", "capture_error")]
            detail = errors[-1] if errors else "No database key was verified"
            raise RuntimeError(f"Synthetic LLDB capture failed (exit {process.returncode}): {detail}")
        if not output.is_file():
            raise RuntimeError("Synthetic capture did not create a private key file")
        captured = json.loads(output.read_bytes()).get("keys", {})
        if len(captured) != 1 or captured.get("message_0.db", {}).get("enc_key") != key.hex():
            raise RuntimeError("Captured synthetic key did not match the fixture")
        if output.stat().st_mode & 0o777 != 0o600 or output.parent.stat().st_mode & 0o777 != 0o700:
            raise RuntimeError("Captured synthetic key permissions were not private")
        result = {"ok": True, "synthetic_only": True, "verified_databases": 1,
                  "key_file_private": True, "key_not_logged": True,
                  "capture_events": [event.get("type") for event in events]}
    print(json.dumps(result), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"ok": False, "synthetic_only": True, "error": str(error)}), flush=True)
        raise SystemExit(1)
