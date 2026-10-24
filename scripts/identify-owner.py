"""Read your numeric ID from your own dedicated bot; no third-party lookup bot."""
import getpass
import json
import re
import secrets
import time
import urllib.request


def main():
    token = getpass.getpass("Your own new BotFather token (hidden, not saved): ")
    if not re.fullmatch(r"[0-9]{5,16}:[A-Za-z0-9_-]{20,100}", token):
        raise ValueError("Invalid token format")
    def call(method, data=None):
        request = urllib.request.Request("https://api.telegram.org/bot" + token + "/" + method,
                    data=json.dumps(data or {}).encode(), headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(request, timeout=35) as response:
            value = json.loads(response.read(2_000_000))
        if not value.get("ok"):
            raise ValueError("Telegram request failed")
        return value["result"]
    call("getMe")
    if call("getWebhookInfo").get("url"):
        raise ValueError("Use a dedicated bot without an existing webhook")
    challenge = "/identify " + secrets.token_urlsafe(12)
    print("Open your bot's PRIVATE chat and send this exact temporary command:")
    print(challenge)
    print("Waiting up to three minutes. This script sends no Telegram messages.")
    deadline, offset = time.monotonic()+180, 0
    while time.monotonic() < deadline:
        for update in call("getUpdates", {"offset":offset,"timeout":20,"allowed_updates":["message"]}):
            offset = max(offset,update["update_id"]+1)
            message = update.get("message",{})
            sender = message.get("from",{}).get("id")
            if (message.get("text")==challenge and type(sender) is int and sender>0
                    and message.get("chat",{}).get("type")=="private"
                    and message["chat"].get("id")==sender):
                print("Your numeric owner ID:",sender)
                return 0
    print("No matching private message. Run again with your own bot.")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError,ValueError,KeyError,TypeError):
        print("Identity lookup failed. Check your dedicated bot and connection. No token or raw diagnostics were printed.")
        raise SystemExit(2)
