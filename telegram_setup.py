import os
import sys
from getpass import getpass
from pathlib import Path


def _read_env(name: str) -> str:
    value = os.getenv(name)
    return str(value).strip() if value is not None else ""


def main() -> int:
    print("Falcon Telegram Session Setup (local use only)")
    print("Run this on your local machine. Do not run this during Render deployment.")

    api_id_raw = _read_env("TELEGRAM_API_ID")
    api_hash = _read_env("TELEGRAM_API_HASH")

    if not api_id_raw or not api_hash:
        print("Missing TELEGRAM_API_ID or TELEGRAM_API_HASH in environment variables.")
        print("Set them locally, then run this script again.")
        return 1

    try:
        api_id = int(api_id_raw)
    except (TypeError, ValueError):
        print("TELEGRAM_API_ID must be a valid integer.")
        return 1

    try:
        from telethon.errors import SessionPasswordNeededError  # noqa: PLC0415
        from telethon.sessions import StringSession  # noqa: PLC0415
        from telethon.sync import TelegramClient  # noqa: PLC0415
    except Exception:
        print("Telethon is not installed. Install dependencies first: python -m pip install -r requirements.txt")
        return 1

    phone = input("Enter your Telegram phone number in international format (example +15551234567): ").strip()
    if not phone:
        print("Phone number is required.")
        return 1

    session_string = ""
    with TelegramClient(StringSession(), api_id, api_hash) as client:
        try:
            client.send_code_request(phone)
            code = input("Enter the Telegram login code you received: ").strip()
            if not code:
                print("Login code is required.")
                return 1

            try:
                client.sign_in(phone=phone, code=code)
            except SessionPasswordNeededError:
                password = getpass("Enter your Telegram 2FA password: ").strip()
                if not password:
                    print("2FA password is required.")
                    return 1
                client.sign_in(password=password)

            if not client.is_user_authorized():
                print("Authorization failed. Try again.")
                return 1

            session_string = client.session.save() or ""
        except Exception as error:
            print(f"Telegram setup failed: {type(error).__name__}")
            return 1

    if not session_string:
        print("Failed to create reusable session string.")
        return 1

    out_dir = Path(__file__).resolve().parent / ".falcon_memory"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "telegram_session.txt"
    out_path.write_text(session_string, encoding="utf-8")

    print("Session created successfully.")
    print(f"Saved reusable TELEGRAM_SESSION value to: {out_path}")
    print("Copy that value into TELEGRAM_SESSION in your local .env or Render environment settings.")
    print("This script does not print the session value to avoid exposing secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
