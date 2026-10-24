"""Foreground launch after a fresh read-only compatibility gate."""
import json
from _common import config, require_windows
from doctor import check


def main():
    require_windows()
    result=check(native=True)
    if not result["ok"] or not result["native_verified"]:
        print(json.dumps(result,ensure_ascii=False,indent=2))
        raise SystemExit("Desktop adapter compatibility not confirmed; no Telegram polling started")
    import ctypes
    from ctypes import wintypes
    import hashlib
    name="Local\\CodexTelegramDesktop-"+hashlib.sha256(str(config.data_dir()).encode()).hexdigest()[:16]
    kernel=ctypes.WinDLL("kernel32",use_last_error=True)
    kernel.CreateMutexW.argtypes=[ctypes.c_void_p,wintypes.BOOL,wintypes.LPCWSTR]
    kernel.CreateMutexW.restype=ctypes.c_void_p
    handle=kernel.CreateMutexW(None,True,name)
    if not handle or ctypes.get_last_error()==183:
        raise SystemExit("This adapter instance is already running")
    (config.data_dir()/".background_native_enabled").touch()
    import telegram_bridge as bridge
    try:
        bridge.run()
    finally:
        kernel.ReleaseMutex(ctypes.c_void_p(handle))
        kernel.CloseHandle(ctypes.c_void_p(handle))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Adapter stopped")
