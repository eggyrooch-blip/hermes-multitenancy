"""Import the public TRAE connector icons with the existing SSRF guard."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from .connector_remote_probe import ValidatedEndpoint, _PinnedResolver, validate_remote_endpoint


_TRAE_API = "https://api.trae.com.cn/extensions/api/-/agent/search?offset=0&size=1000"
_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"RIFF", ".webp"))


async def _fetch(url: str, *, limit: int) -> bytes:
    parsed = urlsplit(url)
    clean = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    endpoint = validate_remote_endpoint(clean)
    endpoint = ValidatedEndpoint(url, endpoint.host, endpoint.port, endpoint.addresses)
    connector = aiohttp.TCPConnector(resolver=_PinnedResolver(endpoint), use_dns_cache=False)
    async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
        async with session.get(url, allow_redirects=False, timeout=aiohttp.ClientTimeout(total=20)) as response:
            if response.status != 200:
                raise ValueError(f"icon HTTP {response.status}")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("icon exceeds size limit")
            return bytes(data)


def _extension(data: bytes) -> str:
    for magic, suffix in _MAGIC:
        if data.startswith(magic) and (suffix != ".webp" or data[8:12] == b"WEBP"):
            return suffix
    raise ValueError("unsupported icon format")


async def import_trae_icons(output: Path) -> list[dict[str, str]]:
    catalog = json.loads((await _fetch(_TRAE_API, limit=4 * 1024 * 1024)).decode("utf-8"))
    rows = catalog.get("data")
    if not isinstance(rows, list):
        raise ValueError("invalid TRAE catalog response")
    output.mkdir(parents=True, exist_ok=True)
    semaphore = asyncio.Semaphore(12)

    async def one(row: dict[str, object]) -> dict[str, str] | None:
        icon = str(row.get("icon") or "").strip()
        catalog_id = str(row.get("id") or "").strip()
        if not icon or not catalog_id:
            return None
        try:
            async with semaphore:
                data = await _fetch(icon, limit=1024 * 1024)
            digest = hashlib.sha256(data).hexdigest()
            path = output / f"{digest}{_extension(data)}"
            if not path.exists():
                path.write_bytes(data)
            return {
                "row_key": f"trae solo cn:{catalog_id.casefold()}",
                "path": str(path),
                "sha256": digest,
                "source": "trae_official_public_market",
            }
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return None

    return [item for item in await asyncio.gather(*(one(row) for row in rows if isinstance(row, dict))) if item]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--map-output", type=Path, required=True)
    args = parser.parse_args(argv)
    results = asyncio.run(import_trae_icons(args.output_dir))
    args.map_output.write_text(
        "".join(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n" for item in results),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
