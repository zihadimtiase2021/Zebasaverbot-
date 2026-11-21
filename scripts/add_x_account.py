"""Register an X/Twitter account for twscrape (used for tweets + replies).

Using browser cookies (recommended; export with the Cookie-Editor extension as Netscape/Header):

    python -m scripts.add_x_account --username myacc --cookies-file data/cookies.txt

Or a raw cookie string:

    python -m scripts.add_x_account --username myacc --cookies "auth_token=...; ct0=..."
"""
import argparse
import asyncio
from pathlib import Path

from unified_core.config import settings
from unified_core.x_accounts import add_account, cookies_from_netscape, has_x_session


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--username", required=True)
    ap.add_argument("--cookies", default="")
    ap.add_argument("--cookies-file", default="")
    args = ap.parse_args()

    cookies = args.cookies or (cookies_from_netscape(Path(args.cookies_file)) if args.cookies_file else "")
    if not has_x_session(cookies):
        raise SystemExit("Cookies must include auth_token and ct0 (log in to x.com in your browser, then export).")

    settings.ensure_dirs()
    print("Account saved. Accounts:")
    for name, active in await add_account(settings.TWSCRAPE_DB, args.username, cookies):
        print(f" - {name} active={active}")


if __name__ == "__main__":
    asyncio.run(main())
