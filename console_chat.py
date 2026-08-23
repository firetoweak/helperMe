from __future__ import annotations

import asyncio
import sys


async def async_main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from helperme.channels.cli.console import run_runtime_console

    await run_runtime_console()


def main(argv: list[str] | None = None) -> None:
    try:
        asyncio.run(async_main(argv))
    except KeyboardInterrupt:
        print("\n已退出。")


if __name__ == "__main__":
    main()
