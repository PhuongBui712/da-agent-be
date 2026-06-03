from __future__ import annotations

import re
from pathlib import Path

import httpx


class GoogleSheetError(Exception): ...
class InvalidUrlError(GoogleSheetError): ...
class NotPublicError(GoogleSheetError): ...
class NotFoundError(GoogleSheetError): ...
class NetworkError(GoogleSheetError): ...


_SHEET_ID_RE = re.compile(r"docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_ZIP_MAGIC = b"PK\x03\x04"
_DEFAULT_TIMEOUT = 30.0


def extract_sheet_id(url: str) -> str:
    """Extract the sheet id; raise InvalidUrlError if URL doesn't look like a Google Sheets share URL."""
    m = _SHEET_ID_RE.search(url.strip())
    if m is None:
        raise InvalidUrlError(f"not a Google Sheets URL: {url!r}")
    return m.group(1)


def export_url(sheet_id: str, fmt: str = "xlsx") -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format={fmt}"


async def download_sheet_as_xlsx(
    sheet_id: str,
    dest: Path,
    *,
    max_bytes: int,
    client: httpx.AsyncClient | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> int:
    """
    Stream the export URL, validate ZIP magic on first chunk, write to `dest`,
    raise typed errors:
      - HTML body / non-ZIP magic → NotPublicError
      - HTTP 404 → NotFoundError
      - timeout / connect refused → NetworkError
      - >max_bytes mid-stream → NotPublicError("sheet exceeds N MB")
    Returns total bytes written.
    """
    url = export_url(sheet_id)
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    try:
        try:
            async with client.stream("GET", url) as response:
                if response.status_code == 404:
                    raise NotFoundError(f"sheet not found: {sheet_id}")
                response.raise_for_status()

                dest.parent.mkdir(parents=True, exist_ok=True)
                total = 0
                header_buf = b""
                header_checked = False

                try:
                    with dest.open("wb") as out:
                        async for chunk in response.aiter_bytes():
                            if not header_checked:
                                header_buf += chunk
                                if len(header_buf) < 4:
                                    continue
                                if header_buf[:4] != _ZIP_MAGIC:
                                    raise NotPublicError(
                                        "sheet is not public; share with 'Anyone with the link can view'"
                                    )
                                header_checked = True
                                out.write(header_buf)
                                total += len(header_buf)
                                header_buf = b""
                                continue
                            total += len(chunk)
                            if total > max_bytes:
                                raise NotPublicError(
                                    f"sheet exceeds {max_bytes // (1024 * 1024)} MB"
                                )
                            out.write(chunk)
                    # header never reached 4 bytes — likely empty or non-ZIP
                    if not header_checked:
                        if len(header_buf) < 4 or header_buf[:4] != _ZIP_MAGIC:
                            raise NotPublicError(
                                "sheet is not public; share with 'Anyone with the link can view'"
                            )
                except (NotPublicError, NotFoundError):
                    dest.unlink(missing_ok=True)
                    raise
                except BaseException:
                    dest.unlink(missing_ok=True)
                    raise
        except httpx.HTTPStatusError as exc:
            dest.unlink(missing_ok=True)
            if exc.response.status_code == 404:
                raise NotFoundError(f"sheet not found: {sheet_id}") from exc
            raise NotPublicError(str(exc)) from exc
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            dest.unlink(missing_ok=True)
            raise NetworkError(str(exc)) from exc
    finally:
        if own_client:
            await client.aclose()

    return total
