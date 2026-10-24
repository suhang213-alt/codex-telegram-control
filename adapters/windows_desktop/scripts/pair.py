"""Explicit Telegram getMe call; prints only the local one-time pairing link."""
from _common import config, require_windows
require_windows()
config.validate(config.settings())
import telegram_bridge as bridge
value = bridge.state()
if value.get("owner_user_id") is not None:
    print("Already paired. The configured numeric owner is required for every request.")
else:
    bot = bridge.api("getMe", {})
    print("Open privately: https://t.me/" + bot["username"] + "?start=" + value["pair_code"])
