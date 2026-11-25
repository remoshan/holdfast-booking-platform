"""What the library's tests share besides fixtures: a link that fails on demand, and a way to wait
for the code under test."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from typing import Any, Self

from holdfast.messaging.records import JobRecord

WAIT_SECONDS = 10
"""How long a test waits for something that should happen almost at once."""

type StoredJobs = Callable[[], Awaitable[list[JobRecord]]]
type Count = Callable[[], Awaitable[int]]
type Worker = Callable[[asyncio.Event], Coroutine[Any, Any, None]]


class FaultyLink:
    """A TCP proxy in front of a server. Code under test connects through it, and the test can then
    cut every connection, as when the server goes down, or stall them, as when it stops answering,
    and later restore the link."""

    def __init__(self, host: str, port: int) -> None:
        self._target = (host, port)
        self._cut = False
        self._flowing = asyncio.Event()
        self._flowing.set()
        self._writers: set[asyncio.StreamWriter] = set()
        self.connections = 0
        """How many connections the link has accepted, cut or not."""

    async def __aenter__(self) -> Self:
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._server.close()
        self.cut()
        self._flowing.set()  # Wakes stalled transfers, which then find their connection closed.
        await self._server.wait_closed()

    @property
    def port(self) -> int:
        port: int = self._server.sockets[0].getsockname()[1]
        return port

    def cut(self) -> None:
        """Drop every connection, and refuse new ones until restored."""
        self._cut = True
        for writer in self._writers:
            writer.close()

    def stall(self) -> None:
        """Keep every connection open, but carry nothing either way until restored."""
        self._flowing.clear()

    def restore(self) -> None:
        self._cut = False
        self._flowing.set()

    async def _accept(
        self, client_reader: asyncio.StreamReader, client: asyncio.StreamWriter
    ) -> None:
        self.connections += 1
        writers = [client]
        try:
            if self._cut:
                return
            server_reader, server = await asyncio.open_connection(*self._target)
            writers.append(server)
            self._writers.update(writers)
            await asyncio.gather(
                self._carry(client_reader, server), self._carry(server_reader, client)
            )
        except OSError:
            pass
        finally:
            for writer in writers:
                self._writers.discard(writer)
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()

    async def _carry(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while data := await reader.read(65536):
                await self._flowing.wait()
                writer.write(data)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()  # So the transfer in the other direction ends too.


async def eventually(condition: Callable[[], Awaitable[bool]]) -> None:
    """Wait for a condition that the code under test should soon make true."""
    async with asyncio.timeout(WAIT_SECONDS):
        while True:
            if await condition():
                return
            await asyncio.sleep(0.05)


@asynccontextmanager
async def running(*workers: Worker) -> AsyncIterator[None]:
    """Run workers for the length of the block, then stop them as a signal would and wait until
    they have finished what they hold."""
    stopping = asyncio.Event()
    tasks = [asyncio.create_task(worker(stopping)) for worker in workers]
    try:
        yield
    finally:
        stopping.set()
        async with asyncio.timeout(WAIT_SECONDS):
            await asyncio.gather(*tasks)
