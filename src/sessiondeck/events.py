import json
from collections.abc import AsyncIterable, AsyncIterator


async def parse_sse(lines: AsyncIterable[str]) -> AsyncIterator[dict]:
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                try:
                    event = json.loads("\n".join(data))
                    if isinstance(event, dict):
                        yield event
                except (ValueError, TypeError):
                    pass
                data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
