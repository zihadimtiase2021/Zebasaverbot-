"""Create a Telegram login as TEXT (SESSION_STRING) for hosts without a terminal, e.g. Render.

    python3 -m scripts.export_session

It logs in with a NEW session (phone number + code), so your local bot keeps running with its own session and the
hosted bot gets a separate one. The printed text gives full access to your Telegram account: treat it like a
password, paste it only into the host's secret environment variable SESSION_STRING, never share or commit it.
"""
import asyncio

from unified_core.setup_wizard import SetupError, effective, pyro_login_prompt, say

_DESKTOP = dict(device_model="Desktop", system_version="Windows 10.0", app_version="4.14.8")


async def main() -> None:
    from unified_core.pyro_login import verify_telethon_string

    api_id, api_hash = effective("API_ID"), effective("API_HASH")
    if not api_id or not api_hash:
        raise SetupError("API_ID / API_HASH are not set (get them at https://my.telegram.org and put them in .env)")
    # login runs on pyrofork; the printed text is a Telethon StringSession, which is what the bot reads
    text = await pyro_login_prompt(desktop=True)
    if not await verify_telethon_string(text, int(api_id), api_hash, **_DESKTOP):
        raise SetupError("Logged in, but Telethon could not use the session text. Run this again.")
    say("\nSESSION_STRING (copy everything between the lines):")
    say("-" * 60)
    say(text)
    say("-" * 60)
    say("Keep it secret. Paste it into Render -> Environment -> SESSION_STRING.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SetupError as exc:
        raise SystemExit(f"Error: {exc}")
    except KeyboardInterrupt:
        raise SystemExit(130)
