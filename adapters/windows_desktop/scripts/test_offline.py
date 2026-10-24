"""Run isolated tests with network and external programs prohibited; test workers are allowed."""
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"bridge"))
sys.path.insert(0,str(ROOT/"tests"))


def prohibited(*args,**kwargs):
    raise AssertionError("Offline test attempted network or subprocess I/O")


def main():
    with tempfile.TemporaryDirectory(prefix="desktop-adapter-test-") as temporary:
        os.environ["CTD_DATA_DIR"]=str(Path(temporary)/"adapter")
        os.environ["LOCALAPPDATA"]=str(Path(temporary)/"local")
        os.environ["CODEX_HOME"]=str(Path(temporary)/"codex")
        os.environ.pop("CTD_BRIDGE_BOT_TOKEN",None)
        os.environ.pop("CTD_RELAY_KEY_FILE",None)
        os.environ.pop("CTD_RELAY_PUBLIC_URL",None)
        for folder in ("adapter","local","codex"):(Path(temporary)/folder).mkdir()
        with patch.object(urllib.request,"urlopen",prohibited), patch.object(socket.socket,"connect",prohibited), patch.object(subprocess,"Popen",prohibited):
            suite=unittest.defaultTestLoader.discover(str(ROOT/"tests"),pattern="test_*.py")
            result=unittest.TextTestRunner(verbosity=1).run(suite)
        raise SystemExit(0 if result.wasSuccessful() else 1)


if __name__ == "__main__":
    main()
