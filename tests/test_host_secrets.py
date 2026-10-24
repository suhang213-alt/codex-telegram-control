"""Synthetic owner-only connector keys; no account credentials or processes."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from codex_telegram.config import SafeError
from codex_telegram import host_secrets, relay_client


def protect_fixture(path, *, foreign=False):
    if os.name != "nt":
        path.chmod((0o755 if path.is_dir() else 0o644) if foreign else (0o700 if path.is_dir() else 0o600))
        return
    # Obtain only the synthetic fixture's owner SID through the native API;
    # the setup ACL writer is reused without invoking whoami or any process.
    import ctypes
    from ctypes import wintypes
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    security.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer), ctypes.POINTER(pointer)]
    security.GetNamedSecurityInfoW.restype = wintypes.DWORD
    security.ConvertSidToStringSidW.argtypes = [pointer, ctypes.POINTER(wintypes.LPWSTR)]
    security.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [pointer]
    kernel.LocalFree.restype = pointer
    owner, descriptor, text = pointer(), pointer(), wintypes.LPWSTR()
    try:
        if security.GetNamedSecurityInfoW(str(path), 1, 1, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)):
            raise AssertionError("Synthetic fixture owner is unavailable")
        if not security.ConvertSidToStringSidW(owner, ctypes.byref(text)): raise AssertionError("Synthetic SID conversion failed")
        sid = text.value
    finally:
        if text: kernel.LocalFree(ctypes.cast(text, pointer))
        if descriptor.value: kernel.LocalFree(descriptor)
    spec = importlib.util.spec_from_file_location("fixture_docker_setup", Path(__file__).resolve().parents[1] / "scripts/docker.py")
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    with patch.object(helper, "windows_sid", return_value="S-1-1-0" if foreign else sid):
        helper.set_private_windows_acl(path)


class HostKeyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name).resolve() / "private"
        self.folder.mkdir()
        protect_fixture(self.folder)
        self.key = self.folder / "key"
        self.key.write_text("ab" * 32 + "\n", encoding="ascii")
        protect_fixture(self.key)

    def test_native_owner_only_fixture_is_accepted(self):
        self.assertEqual(host_secrets.read_host_key(self.key), bytes.fromhex("ab" * 32))

    def test_hardlinked_key_is_rejected(self):
        os.link(self.key, self.folder / "other-key")
        with self.assertRaises(SafeError): host_secrets.read_host_key(self.key)

    def test_foreign_access_to_key_or_parent_is_rejected(self):
        for path in (self.key, self.folder):
            with self.subTest(kind="file" if path == self.key else "directory"):
                protect_fixture(path, foreign=True)
                try:
                    with self.assertRaises(SafeError): host_secrets.read_host_key(self.key)
                finally: protect_fixture(path)

    def test_parent_redirect_is_rejected_before_read(self):
        original = Path.is_symlink
        with patch.object(Path, "is_symlink", lambda value: value == self.folder or original(value)), \
                patch.object(host_secrets.os, "open", side_effect=AssertionError("Key must not be opened")):
            with self.assertRaises(SafeError): host_secrets.read_host_key(self.key)

    def test_direct_environment_entrypoint_requires_host_permissions(self):
        with patch.dict(os.environ, {"CTD_RELAY_KEY_FILE": str(self.key), "CTD_RELAY_PUBLIC_URL": "https://panel.example.test"}), \
                patch.object(host_secrets, "read_host_key", side_effect=SafeError("unsafe key")) as read, \
                patch.object(relay_client.RelayClient, "join", side_effect=AssertionError("No connection before permissions")):
            with self.assertRaises(SafeError): relay_client.start("synthetic", 123, "https://panel.example.test", "macos")
            read.assert_called_once_with(str(self.key))


if __name__ == "__main__": unittest.main()
