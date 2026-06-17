import argparse
import asyncio
import sys
from pathlib import Path

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
    parser.add_argument(
        "--init-settings",
        action="store_true",
        help="Initialize a clean, schema-compliant default settings template at the project path (.code_savant/settings.json) and exit."
    )
    args = parser.parse_args()

    # Import config, registry, and server dynamically to minimize start footprint
    from engine.config import SettingsManager
    from engine.registry import ModelRegistryService
    from engine.uds_server import UdsServer

    package_dir = Path(__file__).parent.resolve()
    
    # Configure defaults, user global, and project local paths
    default_settings_path = package_dir / "default_settings.json"
    user_settings_path = Path("~/.config/code_savant/settings.json").expanduser()
    project_settings_path = Path(".code_savant/settings.json").expanduser()
    
    bundled_models_path = package_dir / "bundled_models.json"
    cache_models_path = Path("~/.config/code_savant/models_cache.json").expanduser()

    # Configure the settings manager
    settings_manager = SettingsManager(
        default_path=default_settings_path,
        user_path=user_settings_path,
        project_path=project_settings_path
    )

    # Process default settings template initialization early if flagged
    if args.init_settings:
        try:
            await settings_manager.dump_default_settings(project_settings_path)
            print(f"Successfully initialized default settings template at: {project_settings_path}", file=sys.stdout)
            sys.exit(0)
        except Exception as e:
            print(f"Initialization error: {e}", file=sys.stderr)
            sys.exit(1)

    # Asynchronously load settings cascade (Rule 3)
    await settings_manager.load_settings()

    # Asynchronously initialize the model registry service
    model_registry = ModelRegistryService(
        bundled_path=bundled_models_path,
        cache_path=cache_models_path
    )
    await model_registry.initialize()

    # Resolve active socket path (precedence: CLI arg > settings.socket_path)
    socket_path = args.socket_path or settings_manager.settings.socket_path
    
    # Inject dependencies cleanly (Rule 8)
    server = UdsServer(
        socket_path=socket_path,
        settings_manager=settings_manager,
        model_registry=model_registry
    )
    
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
