import argparse
import asyncio
import sys

from engine.constants import DEFAULT_SOCKET_PATH


async def bootstrap_agent_cli() -> None:
    """Bootstraps and starts the UDS server daemon."""
    parser = argparse.ArgumentParser(description="CodeSavant UDS Server Launcher")
    parser.add_argument(
        "--socket-path",
        type=str,
        default=None,
        help="The Unix Domain Socket path to bind to."
    )
    args = parser.parse_args()

    # Delay import of UdsServer to keep main startup footprint small
    from engine.uds_server import UdsServer
    
    socket_path = args.socket_path or DEFAULT_SOCKET_PATH
    server = UdsServer(socket_path=socket_path)
    
    print(f"Starting CodeSavant UDS Server on {socket_path}...", file=sys.stderr)
    await server.start()
    
    try:
        # Keep server loop running indefinitely until cancelled
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await server.stop()


def main() -> None:
    """Main entrypoint wrapper running the async bootstrap loop."""
    try:
        asyncio.run(bootstrap_agent_cli())
    except KeyboardInterrupt:
        print("\nServer shutting down via KeyboardInterrupt...", file=sys.stderr)
        sys.exit(0)


if __name__ == "__main__":
    main()
