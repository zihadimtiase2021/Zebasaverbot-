"""Interactive Telegram login only (creates / refreshes the userbot session). The login runs on pyrofork;
the saved session file is the Telethon one the bot reads.

    python3 -m scripts.login
"""
import asyncio
import sys

from unified_core.setup_wizard import (
    Cancelled, SetupError, _client_kwargs, effective, login_interactive, session_file,
)


async def main() -> None:
    from telethon import TelegramClient

    if not effective("API_ID") or not effective("API_HASH"):
        raise SystemExit("API_ID / API_HASH are not set yet - run  python3 run.py --setup  first.")
    session = session_file()
    session.parent.mkdir(parents=True, exist_ok=True)
    def make(sess):
        return TelegramClient(sess, int(effective("API_ID")), effective("API_HASH"), **_client_kwargs())

    client = make(str(session.with_suffix("")))
    await client.connect()
    try:
        if not await client.is_user_authorized():
            client = await login_interactive(client, make)   # login itself runs on pyrofork
        me = await client.get_me()
        print(f"Logged in as {me.first_name} (id={me.id}). Session: {session}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Cancelled:
        sys.exit(130)
    except SetupError as exc:
        sys.exit(f"Login failed: {exc}")
