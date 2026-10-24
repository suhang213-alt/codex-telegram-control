"""Wake only the deterministic native worker; never create a model service turn."""
import bridge_config as config
from pathlib import Path
ROOT = Path(__file__).resolve().parent

def codex_binary():
    from find_codex_cli import find_codex_cli
    return Path(find_codex_cli())

def worker_thread_id():
    return config.service_thread_id()
def notify_pending(operation_id=None, *, background=True):
    if not (config.data_dir() / ".background_native_enabled").is_file():
        return {"ok": True, "enabled": False, "scheduled": False}
    try:
        from background_events import notify
        scheduled = notify()
        return {"ok": True, "enabled": True, "scheduled": scheduled}
    except Exception:
        return {"ok": False, "enabled": True, "scheduled": False}
def notify_control_change():
    return notify_pending()
