"""Test every configured translation provider with a real request and print the exact result/error.

    python3 run.py --test-translate
"""
import asyncio

from unified_core.config import settings
from unified_services.translator import Translator


def _mask(v: str) -> str:
    return f"set ({len(v)} chars)" if v else "NOT SET"


async def main() -> None:
    print("Keys found in .env / environment:")
    print(f"  OLLAMA_API_KEY      {_mask(settings.OLLAMA_API_KEY)}")
    print(f"  OPENROUTER_API_KEY  {_mask(settings.OPENROUTER_API_KEY)}")
    print(f"  OPENAI_API_KEY      {_mask(settings.OPENAI_API_KEY)}")
    print(f"  TRANSLATE_ENABLED={settings.TRANSLATE_ENABLED}  TRANSLATE_PROVIDER={settings.TRANSLATE_PROVIDER}\n")
    print(await Translator().diagnose())


if __name__ == "__main__":
    asyncio.run(main())
