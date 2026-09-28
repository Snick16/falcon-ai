import json
import os
import ssl
import urllib.request
from pathlib import Path

import certifi
import requests


def load_bot_token(env_path: Path) -> str:
    if not env_path.exists():
        raise FileNotFoundError(".env file not found")

    token = ""
    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if not line.startswith("TELEGRAM_BOT_TOKEN="):
                continue
            token = line.split("=", 1)[1].strip().strip('"').strip("'")
            break

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is missing in .env")
    return token


def upsert_env_value(env_path: Path, key: str, value: str) -> None:
    lines = []
    if env_path.exists():
        with env_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()

    prefix = f"{key}="
    replaced = False
    for index, raw in enumerate(lines):
        if raw.strip().startswith(prefix):
            lines[index] = f"{key}={value}\n"
            replaced = True
            break

    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] = lines[-1] + "\n"
        lines.append(f"{key}={value}\n")

    with env_path.open("w", encoding="utf-8") as handle:
        handle.writelines(lines)


def fetch_updates_via_windows_store(url: str, timeout: int = 15) -> dict:
    try:
        import truststore
    except ImportError as error:
        raise RuntimeError(
            "Windows trust-store fallback requires truststore package. "
            "Install it with: py -m pip install truststore"
        ) from error

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with urllib.request.urlopen(url, timeout=timeout, context=context) as response:
        body = response.read().decode("utf-8")
    return json.loads(body)


def get_latest_chat_id(bot_token: str) -> str:
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    payload = None

    try:
        response = requests.get(url, timeout=15, verify=certifi.where())
        response.raise_for_status()
        payload = response.json()
    except requests.exceptions.SSLError as certifi_ssl_error:
        if os.name != "nt":
            raise certifi_ssl_error
        try:
            payload = fetch_updates_via_windows_store(url, timeout=15)
        except Exception as windows_ssl_error:
            raise requests.exceptions.SSLError(
                "SSL verification failed using certifi and Windows trust-store fallback."
            ) from windows_ssl_error

    if not isinstance(payload, dict) or not payload.get("ok"):
        raise RuntimeError("Telegram API returned an invalid response")

    updates = payload.get("result", [])
    if not isinstance(updates, list) or not updates:
        raise RuntimeError("No updates found. Send a message to your bot first.")

    latest_update = updates[-1]
    if not isinstance(latest_update, dict):
        raise RuntimeError("Unexpected update format from Telegram")

    message = latest_update.get("message") or latest_update.get("edited_message")
    if not isinstance(message, dict):
        raise RuntimeError("Latest update does not contain a message")

    chat = message.get("chat")
    if not isinstance(chat, dict) or "id" not in chat:
        raise RuntimeError("Chat ID not found in latest update")

    return str(chat["id"])


def manual_chat_id_fallback(env_path: Path) -> bool:
    entered = input(
        "Enter TELEGRAM_CHAT_ID manually to continue (or press Enter to cancel): "
    ).strip()
    if not entered:
        return False

    upsert_env_value(env_path, "TELEGRAM_CHAT_ID", entered)
    print(entered)
    return True


def main() -> None:
    bot_token = ""
    env_path = Path(__file__).resolve().parent / ".env"
    try:
        bot_token = load_bot_token(env_path)
        chat_id = get_latest_chat_id(bot_token)
        print(chat_id)
    except requests.exceptions.SSLError:
        print(
            "Error: SSL verification failed while contacting Telegram even with certifi. "
            "Attempting manual TELEGRAM_CHAT_ID fallback."
        )
        if not manual_chat_id_fallback(env_path):
            print("Error: No TELEGRAM_CHAT_ID provided. Nothing was changed.")
    except requests.exceptions.RequestException:
        print("Error: Telegram request failed.")
    except Exception as error:
        message = str(error)
        if bot_token:
            message = message.replace(bot_token, "<redacted>")
        print(f"Error: {message}")


if __name__ == "__main__":
    main()
