"""Candidate CI-only signed Desktop probe; never signs in or starts a turn.

Run only on a disposable GitHub-hosted macOS ARM64 runner after pip install .
The checked application, its account-free profile and settings stay in RUNNER_TEMP.
Only the final allowlisted receipt may be uploaded, never the temporary directory.
"""
from __future__ import annotations
import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import platform
import plistlib
import signal
import subprocess
import sys
import tempfile
import time

DOWNLOAD = "https://persistent.oaistatic.com/codex-app-prod/Codex.dmg"


class ProbeDeadline(Exception):
    pass


def command(argv, timeout=60):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("Probe subprocess failed")
    return result


def stop_verified_group(group, leader):
    """The caller must cache this group's verified identity immediately at launch."""
    if group is None:
        return False
    def alive():
        try:
            os.killpg(group, 0)
            return True
        except ProcessLookupError:
            return False
    try:
        if not alive():
            return True
        for requested_signal in (signal.SIGTERM, signal.SIGKILL):
            try: os.killpg(group, requested_signal)
            except ProcessLookupError: return not alive()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                try: os.waitpid(leader, os.WNOHANG)
                except ChildProcessError: pass
                if not alive():
                    return True
                time.sleep(0.1)
        return False
    except OSError:
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    receipt = {key: False for key in (
        "runner_supported", "downloaded", "mounted", "bundle_verified", "copied_unchanged",
        "gui_session_available", "launched", "connected", "ready", "backend_desktop",
        "ambiguous", "doctor_connected", "poll_timed_out", "cleanup_complete", "passed")}
    receipt.update(app_version="", bundle_id="", team_id="", stage="environment", error_type="")
    if not (sys.platform == "darwin" and platform.machine() == "arm64"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"):
        print(json.dumps(receipt))
        return 2
    runner_temp = Path(os.environ["RUNNER_TEMP"]).resolve(strict=True)
    output = args.output.expanduser().resolve()
    if not output.is_relative_to(runner_temp) or output.is_symlink():
        print(json.dumps(receipt))
        return 2
    receipt["runner_supported"] = True
    root = Path(tempfile.mkdtemp(prefix="native-desktop-probe-", dir=runner_temp)).resolve()
    os.chmod(root, 0o700)
    mount = root / "mount"
    mount.mkdir()
    mounted = False
    attach_attempted = False
    app_pid = None
    verified_group = None
    identity = None
    probe_platform = None
    try:
        from codex_telegram import macos_platform as probe_platform
        from codex_telegram.config import SafeError, atomic_json, validate
        from codex_telegram.desktop_rpc import DesktopRpc
        from codex_telegram.macos_cli import doctor

        receipt["stage"] = "download"
        command(["/usr/bin/curl", "--fail", "--location", "--silent", "--show-error",
                 "--proto", "=https", "--proto-redir", "=https", "--max-time", "60",
                 "--output", str(root / "Codex.dmg"), DOWNLOAD], timeout=65)
        receipt["downloaded"] = True
        receipt["stage"] = "mount"
        attach_attempted = True
        command(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint",
                 str(mount), str(root / "Codex.dmg")])
        mounted = True
        receipt["mounted"] = True
        candidates = []
        for candidate in mount.glob("*.app"):
            with (candidate / "Contents/Info.plist").open("rb") as stream:
                metadata = plistlib.load(stream)
            if metadata.get("CFBundleIdentifier") == probe_platform.BUNDLE_ID:
                candidates.append(candidate)
        if len(candidates) != 1:
            raise RuntimeError("Installer has no unique supported bundle")
        receipt["stage"] = "inspect_original"
        original = probe_platform.inspect_bundle(candidates[0])
        receipt["bundle_verified"] = True
        app = root / "Codex.app"
        receipt["stage"] = "copy_verify"
        command(["/usr/bin/ditto", str(candidates[0]), str(app)])
        identity = probe_platform.inspect_bundle(app)
        if any(identity[key] != original[key] for key in ("app_version", "bundle_id", "team_id", "cli_sha256")):
            raise RuntimeError("Copied app identity differs")
        receipt["copied_unchanged"] = True
        receipt.update({key: identity[key] for key in ("app_version", "bundle_id", "team_id")})
        # Assess the unchanged signed application; never remove quarantine,
        # re-sign it, disable Gatekeeper, or substitute an executable.
        receipt["stage"] = "gatekeeper"
        command(["/usr/sbin/spctl", "--assess", "--type", "execute", str(app)])
        receipt["stage"] = "gui_session"
        command(["/bin/launchctl", "print", "gui/" + str(os.getuid())], timeout=10)
        command(["/usr/bin/pgrep", "-x", "WindowServer"], timeout=10)
        receipt["gui_session_available"] = True
        home, profile, workspace = (root / name for name in ("bridge", "empty-profile", "empty-workspace"))
        for folder in (home, profile, workspace):
            folder.mkdir(mode=0o700)
        identity["codex_home"] = str(profile)
        config = validate({"schema": 1, "backend": "desktop", "owner_id": 4242,
                           "workspace": str(workspace), "public_url": "https://panel.example.org",
                           "sandbox": "read-only", "desktop": identity})
        atomic_json(home / "config.json", config)
        receipt["stage"] = "launch"
        launched = probe_platform.launch(config, home)
        app_pid = launched["pid"]
        receipt["launched"] = True
        receipt["stage"] = "verify_process_group"
        if (probe_platform.process_path(app_pid) != Path(identity["app_executable"])
                or os.getpgid(app_pid) != app_pid):
            raise RuntimeError("Launched process group identity could not be verified")
        verified_group = app_pid

        def alarm(_signum, _frame):
            raise ProbeDeadline()

        receipt["stage"] = "handshake"
        previous = signal.signal(signal.SIGALRM, alarm)
        signal.setitimer(signal.ITIMER_REAL, 60)
        deadline = time.monotonic() + 60
        try:
            while time.monotonic() < deadline:
                try:
                    status = DesktopRpc.read_status(home)
                    for key in ("connected", "ready", "ambiguous"):
                        receipt[key] = status.get(key) is True
                    receipt["backend_desktop"] = status.get("backend") == "desktop"
                    if receipt["connected"] and receipt["ready"] and receipt["backend_desktop"] and not receipt["ambiguous"]:
                        # Doctor uses signature/hash + status only. Its console
                        # output is discarded; no native payload is logged.
                        with contextlib.redirect_stdout(io.StringIO()):
                            receipt["doctor_connected"] = doctor(config, home) == 0
                        break
                except (SafeError, OSError, ValueError):
                    pass
                time.sleep(0.5)
            else:
                receipt["poll_timed_out"] = True
        except ProbeDeadline:
            receipt["poll_timed_out"] = True
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
    except Exception as error:
        # No exception text, account state, native status, paths, or credentials
        # are included in the public receipt.
        receipt["error_type"] = type(error).__name__ if type(error).__name__ in {
            "SafeError", "OSError", "ValueError", "RuntimeError", "TimeoutExpired"
        } else "probe_error"
    finally:
        process_clean = app_pid is None
        if app_pid is not None:
            # The leader may have exited while its children are still alive.
            # Only disappearance of the cached verified group establishes cleanup.
            process_clean = stop_verified_group(verified_group, app_pid)
        # hdiutil may have attached the volume before its caller timed out.
        # Attempt cleanup even when attach did not return success; an uncertain
        # detach remains a conservative cleanup failure, never a claimed PASS.
        unmounted = not attach_attempted
        if attach_attempted:
            try:
                command(["/usr/bin/hdiutil", "detach", str(mount)], timeout=15)
                unmounted = True
            except Exception:
                pass
        receipt["cleanup_complete"] = process_clean and unmounted
        receipt["passed"] = (receipt["doctor_connected"] and receipt["cleanup_complete"]
                             and receipt["ready"] and receipt["connected"] and not receipt["ambiguous"])
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
        os.chmod(output, 0o600)
        print(json.dumps(receipt))
    return 0 if receipt["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
