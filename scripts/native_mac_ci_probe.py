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
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time

DOWNLOAD = "https://persistent.oaistatic.com/codex-app-prod/Codex.dmg"
SAFE_REASONS = {
    "Select a supported installed Codex Desktop bundle with identifier com.openai.codex.": "unsupported_bundle_metadata",
    "The signed Desktop executable layout is unsupported or redirected.": "unsupported_executable_layout",
    "Desktop signature verification failed. Reinstall the official app; signing and quarantine settings were not changed.": "signature_verification_failed",
    "Desktop signing identity is ambiguous.": "signing_identity_ambiguous",
    "Desktop must have the official OpenAI application and signing identity.": "signing_identity_mismatch",
    "A macOS identity check could not run. No application was launched.": "identity_check_unavailable",
}


class ProbeDeadline(Exception):
    pass


def command(argv, timeout=60):
    result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError("Probe subprocess failed")
    return result


def bundle_diagnostics(bundle, adapter):
    """Allowlisted metadata of the downloaded app only; no profile or raw errors."""
    bundle = bundle.resolve(strict=True)
    result = {"bundle_name": "", "app_version": "", "metadata_bundle_id": "",
              "main_executable_name": "", "expected_cli": {}, "main_executable": {},
              "codesign_verify_returncode": None, "codesign_details_returncode": None,
              "signing_identifier": "", "signing_team_id": "", "codex_candidates": [],
              "candidate_scan_truncated": False}
    def label(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9 ._()+-]{1,128}", value) else ""
    def facts(path):
        value = {"exists": False, "within_bundle": False, "symlink": path.is_symlink(),
                 "size": None, "mode": "", "magic_hex": ""}
        try:
            resolved = path.resolve(strict=True)
            value["within_bundle"] = resolved.is_relative_to(bundle)
            if not value["within_bundle"]: return value
            info = path.stat()
            value.update(exists=True, size=info.st_size, mode=oct(stat.S_IMODE(info.st_mode)))
            if stat.S_ISREG(info.st_mode):
                with path.open("rb") as stream: value["magic_hex"] = stream.read(4).hex()
        except OSError:
            pass
        return value
    result["bundle_name"] = label(bundle.name)
    info_path = bundle / "Contents/Info.plist"
    try:
        if not info_path.is_symlink() and info_path.resolve(strict=True).is_relative_to(bundle):
            with info_path.open("rb") as stream: body = stream.read(1024*1024+1)
            metadata = plistlib.loads(body) if len(body) <= 1024*1024 else {}
            result["app_version"] = label(metadata.get("CFBundleShortVersionString"))
            result["metadata_bundle_id"] = label(metadata.get("CFBundleIdentifier"))
            name = label(metadata.get("CFBundleExecutable"))
            result["main_executable_name"] = name
            if name and name not in {".", ".."}: result["main_executable"] = facts(bundle / "Contents/MacOS" / name)
    except (OSError, ValueError, TypeError):
        pass
    result["expected_cli"] = facts(bundle / adapter.CLI_RESOURCE)
    requirement = (f'identifier "{adapter.BUNDLE_ID}" and anchor apple generic '
                   f'and certificate leaf[subject.OU] = "{adapter.TEAM_ID}"')
    for kind, arguments in (("verify", ["--verify", "--deep", "--strict", "-R", requirement]),
                            ("details", ["-dv", "--verbose=4"])):
        try:
            response = subprocess.run(["/usr/bin/codesign", *arguments, str(bundle)], stdin=subprocess.DEVNULL,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
            result["codesign_" + kind + "_returncode"] = response.returncode
            if kind == "details":
                for line in (response.stdout + b"\n" + response.stderr)[:32768].decode("utf-8", "replace").splitlines():
                    key, separator, value = line.partition("=")
                    if separator and key in {"Identifier", "TeamIdentifier"}:
                        result["signing_identifier" if key == "Identifier" else "signing_team_id"] = label(value)
        except (OSError, subprocess.SubprocessError):
            pass
    resources = bundle / "Contents/Resources"
    visited = 0
    if resources.is_dir() and not resources.is_symlink() and resources.resolve().is_relative_to(bundle):
        pending = [(resources, 0)]
        while pending and not result["candidate_scan_truncated"]:
            base, depth = pending.pop()
            with os.scandir(base) as entries:
                for entry in entries:
                    visited += 1
                    if visited > 20000:
                        result["candidate_scan_truncated"] = True
                        break
                    if entry.is_symlink(): continue
                    if entry.is_dir(follow_symlinks=False):
                        if depth < 8: pending.append((Path(entry.path), depth+1))
                    elif entry.name == "codex" and entry.is_file(follow_symlinks=False):
                        candidate = Path(entry.path)
                        relative = candidate.relative_to(bundle).as_posix()
                        if len(relative) <= 512 and all(32 <= ord(char) < 127 for char in relative):
                            result["codex_candidates"].append(dict(relative_path=relative, **facts(candidate)))
                            if len(result["codex_candidates"]) >= 20:
                                result["candidate_scan_truncated"] = True
                                break
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
    receipt.update(app_version="", bundle_id="", team_id="", stage="environment", error_type="",
                   safe_error_reason="", bundle_diagnostics={})
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
        receipt["bundle_diagnostics"] = bundle_diagnostics(candidates[0], probe_platform)
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
        if type(error).__name__ == "SafeError":
            receipt["safe_error_reason"] = SAFE_REASONS.get(str(error), "unclassified_safe_error")
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
