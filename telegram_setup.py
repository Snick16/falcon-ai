import os
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
        from telethon.errors import FloodWaitError, SendCodeUnavailableError  # noqa: PLC0415
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
    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        # Single authorization flow: this is the only login/authentication call.
        client.start(phone=phone)

        # Save immediately after successful start; do not trigger any additional auth calls.
        session_string = StringSession.save(client.session) or ""
    except FloodWaitError as error:
        wait_seconds = int(getattr(error, "seconds", 0) or 0)
        if wait_seconds > 0:
            print(f"Telegram rate limit hit. Wait {wait_seconds} seconds before retrying.")
        else:
            print("Telegram rate limit hit. Wait before retrying.")
        return 1
    except SendCodeUnavailableError:
        print("Telegram cannot send a login code right now. Wait before retrying; do not repeat attempts quickly.")
        return 1
    except Exception as error:
        print(f"Telegram setup failed: {type(error).__name__}")
        return 1
    finally:
        try:
            client.disconnect()
        except Exception:
            pass

    if not session_string:
        print("Failed to create reusable session string.")
        return 1

    out_dir = Path(__file__).resolve().parent / ".falcon_memory"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "telegram_session.txt"
    out_path.write_text(session_string, encoding="utf-8")

    print("Telegram session created successfully.")
    print("WARNING: TELEGRAM_SESSION is a secret. Store it securely and do not share it.")
    print("TELEGRAM_SESSION value:")
    print(session_string)
    print(f"Saved reusable TELEGRAM_SESSION value to: {out_path}")
    print("Copy this value into TELEGRAM_SESSION in your local .env or Render environment settings.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
