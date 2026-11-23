"""python -m hosted.spawner"""

from __future__ import annotations

import asyncio

from hosted.spawner.service import main

if __name__ == "__main__":
    asyncio.run(main())
