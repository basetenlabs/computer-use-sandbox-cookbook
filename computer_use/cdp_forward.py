"""Forward a TCP port on all interfaces to one on 127.0.0.1.

Runs inside the sandbox. Chrome only serves DevTools on 127.0.0.1, and the
sandbox's port route reaches services on the sandbox's own address, so this
opens Chrome's DevTools port to the port route. The port route still requires
a sandbox access token, so DevTools stays private.

Usage: python3 cdp_forward.py <listen_port> <target_port>
"""

import asyncio
import sys


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def _main(listen_port: int, target_port: int) -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            target_reader, target_writer = await asyncio.open_connection("127.0.0.1", target_port)
        except OSError:
            writer.close()
            return
        await asyncio.gather(
            _pipe(reader, target_writer), _pipe(target_reader, writer), return_exceptions=True
        )

    server = await asyncio.start_server(handle, "0.0.0.0", listen_port)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(_main(int(sys.argv[1]), int(sys.argv[2])))
