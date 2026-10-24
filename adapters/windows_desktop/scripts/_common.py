from pathlib import Path
import sys
ADAPTER = Path(__file__).resolve().parents[1]
BRIDGE = ADAPTER / "bridge"
sys.path.insert(0, str(BRIDGE))
import bridge_config as config


def require_windows():
    import os
    if os.name != "nt" or sys.version_info[:2] != (3, 12):
        raise RuntimeError("This optional Desktop adapter requires Windows and Python 3.12; use the portable core on other platforms.")
