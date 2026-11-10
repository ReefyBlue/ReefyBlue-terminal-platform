"""Tiny asyncio TCP proxy whose link can be cut and restored (upstream keeps running)."""

from __future__ import annotations

import asyncio


class TcpProxy:
    def __init__(self, listen_port: int, target_port: int, host: str = "127.0.0.1") -> None:
        self.listen_port = listen_port
        self.target_port = target_port
        self.host = host
        self.up = True
        self._writers: list[asyncio.StreamWriter] = []
        self._server: asyncio.base_events.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.listen_port)

    async def stop(self) -> None:
        self.cut()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def cut(self) -> None:
        """Abort every open connection and refuse new ones until ``restore()``."""
        self.up = False
        for w in self._writers:
            w.transport.abort()
        self._writers = []

    def restore(self) -> None:
        self.up = True

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not self.up:
            writer.transport.abort()
            return
        try:
            ureader, uwriter = await asyncio.open_connection(self.host, self.target_port)
        except OSError:
            writer.transport.abort()
            return
        self._writers += [writer, uwriter]

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while data := await src.read(65536):
                    if not self.up:
                        break
                    dst.write(data)
                    await dst.drain()
            except (OSError, asyncio.IncompleteReadError):
                pass
            finally:
                dst.transport.abort()

        await asyncio.gather(pipe(reader, uwriter), pipe(ureader, writer))
