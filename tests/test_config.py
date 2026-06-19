import pytest
import json
from pathlib import Path
from engine.config import (
    SettingsManager,
    EngineDaemonSettings,
    SettingsLoadError,
    ProviderSettings,
)
from engine.registry import ModelRegistryService, ModelDefinition


@pytest.mark.asyncio
async def test_settings_manager_precedence_cascade(tmp_path: Path):
    """
    Verifies that configurations load sequentially and project-level overrides
    properly shadow user-level and system-level defaults.
    """
    default_file = tmp_path / "default.json"
    user_file = tmp_path / "user.json"
    proj_file = tmp_path / "proj.json"

    # 1. Write baseline defaults
    defaults_dict = {
        "socket_path": "/tmp/default.sock",
        "model": "google/gemini-3.5-flash",
        "small_model": None,
        "context_filenames": ["GEMINI.md"],
        "global_context_dir": "~/.gemini",
        "session_storage_dir": ".code_savant/sessions",
        "requires_approval": False,
        "providers": {},
    }
    default_file.write_text(json.dumps(defaults_dict))

    # 2. Write user overrides
    user_dict = {
        "model": "anthropic/claude-3-opus",
        "small_model": "google/gemini-3.5-flash",
    }
    user_file.write_text(json.dumps(user_dict))

    # 3. Write project override (Highest precedence)
    proj_dict = {"model": "openai/o3-mini"}
    proj_file.write_text(json.dumps(proj_dict))

    # Instantiate manager
    mgr = SettingsManager(
        default_path=default_file, user_path=user_file, project_path=proj_file
    )
    settings = await mgr.load_settings()

    # Assert correct resolution cascade (Rule 3)
    assert settings.socket_path == "/tmp/default.sock"  # Preserved from defaults
    assert (
        settings.small_model == "google/gemini-3.5-flash"
    )  # Inherited from user overrides
    assert settings.model == "openai/o3-mini"  # Shadowed by local project override


@pytest.mark.asyncio
async def test_settings_manager_missing_defaults_fails_loudly(tmp_path: Path):
    """
    Ensures that a missing core system configuration raises a SettingsLoadError immediately
    instead of falling back secretly.
    """
    non_existent = tmp_path / "missing_file.json"
    mgr = SettingsManager(
        default_path=non_existent,
        user_path=tmp_path / "user.json",
        project_path=tmp_path / "proj.json",
    )

    with pytest.raises(SettingsLoadError) as exc_info:
        await mgr.load_settings()

    assert "missing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_settings_manager_corrupted_json_fails_loudly(tmp_path: Path):
    """
    Ensures that corrupted JSON syntax in any settings file raises a SettingsLoadError cleanly.
    """
    default_file = tmp_path / "default.json"
    user_file = tmp_path / "user.json"

    default_file.write_text(
        json.dumps(
            {
                "socket_path": "/tmp/default.sock",
                "model": "google/gemini-3.5-flash",
                "small_model": None,
                "context_filenames": ["GEMINI.md"],
                "global_context_dir": "~/.gemini",
                "session_storage_dir": ".code_savant/sessions",
                "requires_approval": False,
                "providers": {},
            }
        )
    )
    user_file.write_text("{corrupted JSON: this is not valid}")

    mgr = SettingsManager(
        default_path=default_file,
        user_path=user_file,
        project_path=tmp_path / "proj.json",
    )

    with pytest.raises(SettingsLoadError) as exc_info:
        await mgr.load_settings()

    assert "corrupted" in str(exc_info.value)


@pytest.mark.asyncio
async def test_model_registry_atomic_persistence(tmp_path: Path):
    """
    Verifies that registering custom model overrides writes through atomically (Rule 13)
    and loads correctly upon re-initialization.
    """
    bundled_file = tmp_path / "bundled.json"
    cache_file = tmp_path / "cache.json"

    # Create bundled definitions database
    bundled_data = {
        "google/gemini-3.5-flash": {
            "name": "google/gemini-3.5-flash",
            "provider": "gemini",
            "limits": {"context_tokens": 1000000, "max_output_tokens": 8192},
        }
    }
    bundled_file.write_text(json.dumps(bundled_data))

    # Initial boot of registry
    registry = ModelRegistryService(bundled_path=bundled_file, cache_path=cache_file)
    await registry.initialize()

    # Prepare custom override
    new_model = ModelDefinition(
        name="custom/local-llama",
        provider="ollama",
        limits={"context_tokens": 8192, "max_output_tokens": 2048},
    )

    # Trigger override registration (persists to cache file)
    await registry.register_model_override(new_model)

    # Verify atomic file existences
    assert cache_file.is_file()
    assert not tmp_path.joinpath(
        "cache.tmp"
    ).exists()  # Temp file has been cleaned up/swapped

    # Boot a secondary registry pointing to the same files to verify persistent caching
    registry_reboot = ModelRegistryService(
        bundled_path=bundled_file, cache_path=cache_file
    )
    await registry_reboot.initialize()

    loaded_override = registry_reboot.get_model("custom/local-llama")
    assert loaded_override.provider == "ollama"
    assert loaded_override.limits.context_tokens == 8192


@pytest.mark.asyncio
async def test_provider_settings_resolve_api_key_file(tmp_path: Path):
    """
    Verifies that ProviderSettings resolves credentials by asynchronously reading
    a plain file containing the raw token.
    """
    token_file = tmp_path / "gemini_token.txt"
    token_file.write_text("AIzaSyMySecretMockTokenKey123")

    provider = ProviderSettings(api_key_file=str(token_file))
    resolved_key = await provider.resolve_api_key()

    assert resolved_key == "AIzaSyMySecretMockTokenKey123"


@pytest.mark.asyncio
async def test_settings_manager_dump_default_settings(tmp_path: Path):
    """
    Verifies that SettingsManager.dump_default_settings correctly copies,
    schema-validates, and outputs the bundled default settings file without any hardcoded logic.
    """
    default_file = tmp_path / "default.json"
    target_file = tmp_path / "subdir" / "dumped_template.json"

    defaults_dict = {
        "socket_path": "~/.cache/code_savant/engine.sock",
        "model": "google/gemini-3.5-flash",
        "small_model": None,
        "context_filenames": ["GEMINI.md"],
        "global_context_dir": "~/.gemini",
        "session_storage_dir": ".code_savant/sessions",
        "requires_approval": False,
        "providers": {
            "gemini": {
                "api_key_env_var": "GEMINI_API_KEY",
                "base_url": None,
                "options": {},
            }
        },
    }
    default_file.write_text(json.dumps(defaults_dict))

    # Happy path: Dump from an existing default file
    mgr = SettingsManager(
        default_path=default_file,
        user_path=tmp_path / "user.json",
        project_path=tmp_path / "proj.json",
    )
    await mgr.dump_default_settings(target_file)

    # Verify target file has been written
    assert target_file.is_file()

    # Verify target file is parsed correctly back into our types and matches the source data
    loaded_data = json.loads(target_file.read_text())
    assert loaded_data["socket_path"] == str(
        Path("~/.cache/code_savant/engine.sock").expanduser()
    )
    assert loaded_data["model"] == "google/gemini-3.5-flash"
    assert loaded_data["providers"]["gemini"]["api_key_env_var"] == "GEMINI_API_KEY"

    # Schema validity check
    parsed_settings = EngineDaemonSettings.model_validate(loaded_data)
    assert parsed_settings.model == "google/gemini-3.5-flash"

    # Error path: Fail loudly when the critical system defaults file is missing
    missing_mgr = SettingsManager(
        default_path=tmp_path / "nonexistent_defaults.json",
        user_path=tmp_path / "user.json",
        project_path=tmp_path / "proj.json",
    )
    with pytest.raises(SettingsLoadError) as exc_info:
        await missing_mgr.dump_default_settings(tmp_path / "out.json")
    assert "missing" in str(exc_info.value)


@pytest.mark.asyncio
async def test_session_storage_dir_expansion(tmp_path: Path):
    """
    Verifies that the tilde (~) expansion works flawlessly for session_storage_dir.
    """
    default_file = tmp_path / "default.json"
    defaults_dict = {
        "socket_path": "/tmp/default.sock",
        "model": "google/gemini-3.5-flash",
        "small_model": None,
        "context_filenames": ["GEMINI.md"],
        "global_context_dir": "~/.gemini",
        "session_storage_dir": "~/custom_sessions",
        "requires_approval": False,
        "providers": {},
    }
    default_file.write_text(json.dumps(defaults_dict))

    mgr = SettingsManager(
        default_path=default_file,
        user_path=tmp_path / "user.json",
        project_path=tmp_path / "proj.json",
    )
    settings = await mgr.load_settings()

    # Assert proper expansion
    assert settings.session_storage_dir == str(Path("~/custom_sessions").expanduser())
