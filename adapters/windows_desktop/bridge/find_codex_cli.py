"""Discover the CLI bundled inside the current user's registered Codex package.

No PATH, version/hash pinning, copied executable, process launch or secret read.
Registration and the desktop manifest are checked by WindowsStartupBackend.
"""
from __future__ import annotations

from pathlib import Path, PureWindowsPath
import re

from desktop_startup import FULL_NAME, PackageTarget, WindowsStartupBackend


class CodexCliDiscoveryError(RuntimeError):
    """Fixed diagnostics, never raw paths or operating-system exception text."""


def find_codex_cli(*, backend=None):
    """Return the verified installed app/resources/codex.exe absolute path."""
    try:
        target = (backend if backend is not None else WindowsStartupBackend()).target()
        if not isinstance(target, PackageTarget) or not FULL_NAME.fullmatch(target.full_name):
            raise CodexCliDiscoveryError("codex_cli_package_identity_invalid")
        lexical = PureWindowsPath(target.install_path)
        if (not lexical.is_absolute() or not re.fullmatch(r"[A-Za-z]:", lexical.drive)
                or ".." in lexical.parts or lexical.name.casefold() != target.full_name.casefold()):
            raise CodexCliDiscoveryError("codex_cli_package_path_invalid")
        root = Path(target.install_path).resolve(strict=True)
        if root.name.casefold() != target.full_name.casefold():
            raise CodexCliDiscoveryError("codex_cli_package_path_invalid")
        expected = root / "app" / "resources" / "codex.exe"
        actual = expected.resolve(strict=True)
        # Reject a linked executable/path redirected outside the registered
        # package, including an in-package redirect to another executable.
        if actual != expected or not actual.is_file():
            raise CodexCliDiscoveryError("codex_cli_executable_invalid")
        with actual.open("rb") as stream:
            if stream.read(2) != b"MZ":
                raise CodexCliDiscoveryError("codex_cli_executable_invalid")
        return str(actual)
    except CodexCliDiscoveryError:
        raise
    except Exception:
        raise CodexCliDiscoveryError("codex_cli_discovery_unavailable") from None
