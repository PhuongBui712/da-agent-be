"""Unit tests for da_agent.server.google_sheets."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from da_agent.server.google_sheets import (
    InvalidUrlError,
    NetworkError,
    NotFoundError,
    NotPublicError,
    download_sheet_as_xlsx,
    extract_sheet_id,
)

_VALID_ID = "1XSOLsjlPL2F6jILErWtqHvLHJujOMVa6jRlmPq-vJ48"


# --------------------------------------------------------------------------- #
# extract_sheet_id
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url",
    [
        f"https://docs.google.com/spreadsheets/d/{_VALID_ID}/edit?usp=sharing",
        f"https://docs.google.com/spreadsheets/d/{_VALID_ID}/edit#gid=0",
        f"https://docs.google.com/spreadsheets/d/{_VALID_ID}",
        f"  https://docs.google.com/spreadsheets/d/{_VALID_ID}/edit?usp=sharing\n",
    ],
)
def test_extract_sheet_id_happy(url):
    assert extract_sheet_id(url) == _VALID_ID


@pytest.mark.parametrize(
    "url",
    [
        "",
        "https://example.com",
        "docs.google.com/forms/d/1XSOLsjlPL2F6jILErWtqHvLHJujOMVa6jRlmPq-vJ48",
        "https://docs.google.com/spreadsheets/d/short",
    ],
)
def test_extract_sheet_id_raises_invalid(url):
    with pytest.raises(InvalidUrlError):
        extract_sheet_id(url)


# --------------------------------------------------------------------------- #
# Fake client helpers
# --------------------------------------------------------------------------- #
class FakeStream:
    def __init__(self, status_code: int, chunks: list[bytes]):
        self.status_code = status_code
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code)
            raise httpx.HTTPStatusError("error", request=None, response=response)  # type: ignore[arg-type]

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class FakeClient:
    def __init__(self, stream_factory):
        self._stream_factory = stream_factory

    def stream(self, method, url):
        return self._stream_factory(url)

    async def aclose(self):
        pass


# --------------------------------------------------------------------------- #
# download_sheet_as_xlsx
# --------------------------------------------------------------------------- #
async def test_download_happy_path(tmp_path):
    chunks = [b"PK\x03\x04" + b"\x00" * 100, b"\x00" * 50]
    client = FakeClient(lambda _: FakeStream(200, chunks))
    dest = tmp_path / "out.xlsx"
    total = await download_sheet_as_xlsx(
        _VALID_ID, dest, max_bytes=1024 * 1024, client=client
    )
    assert dest.exists()
    assert total == 154
    assert dest.read_bytes()[:4] == b"PK\x03\x04"


async def test_download_html_response_raises_not_public(tmp_path):
    chunks = [b"<html><body>Sign in</body></html>"]
    client = FakeClient(lambda _: FakeStream(200, chunks))
    dest = tmp_path / "out.xlsx"
    with pytest.raises(NotPublicError):
        await download_sheet_as_xlsx(
            _VALID_ID, dest, max_bytes=1024 * 1024, client=client
        )
    assert not dest.exists()


async def test_download_404_raises_not_found(tmp_path):
    client = FakeClient(lambda _: FakeStream(404, []))
    dest = tmp_path / "out.xlsx"
    with pytest.raises(NotFoundError):
        await download_sheet_as_xlsx(
            _VALID_ID, dest, max_bytes=1024 * 1024, client=client
        )


async def test_download_timeout_raises_network_error(tmp_path):
    class TimeoutStream:
        status_code = 200

        async def __aenter__(self):
            raise httpx.TimeoutException("timed out")

        async def __aexit__(self, *a):
            pass

    client = FakeClient(lambda _: TimeoutStream())
    dest = tmp_path / "out.xlsx"
    with pytest.raises(NetworkError):
        await download_sheet_as_xlsx(
            _VALID_ID, dest, max_bytes=1024 * 1024, client=client
        )


async def test_download_oversized_raises_not_public(tmp_path):
    # cap=10, two chunks totalling 14 bytes → exceeds cap
    chunks = [b"PK\x03\x04\x00\x00", b"\x00" * 8]
    client = FakeClient(lambda _: FakeStream(200, chunks))
    dest = tmp_path / "out.xlsx"
    with pytest.raises(NotPublicError, match="exceeds"):
        await download_sheet_as_xlsx(_VALID_ID, dest, max_bytes=10, client=client)
    assert not dest.exists()


async def test_download_split_magic_accumulates(tmp_path):
    """First chunk has only 2 bytes of the magic; second completes it."""
    chunks = [b"PK", b"\x03\x04" + b"\x00" * 50]
    client = FakeClient(lambda _: FakeStream(200, chunks))
    dest = tmp_path / "out.xlsx"
    total = await download_sheet_as_xlsx(
        _VALID_ID, dest, max_bytes=1024 * 1024, client=client
    )
    assert dest.exists()
    assert dest.read_bytes()[:4] == b"PK\x03\x04"
    assert total == 54
