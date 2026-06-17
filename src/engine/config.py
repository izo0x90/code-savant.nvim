import aiofiles
import os
from pathlib import Path
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class SettingsLoadError(Exception):
    """Raised when configuration resolution, file-loading, or schema validation fails."""
    def __init__(self, message: str, path: Optional[Path] = None, details: Optional[str] = None):
        super().__init__(f"{message} (Path: {path})" if path else message)
        self.path = path
        self.details = details


class ProviderSettings(BaseModel):
    """
    Extensible credentials and API gateway details for LLM providers.
    Deals purely with connectivity and authentication.
    """
    base_url: Optional[str] = Field(
        default=None,
        description="Custom API endpoint base URL. Set to override standard endpoints."
    )
    api_key: Optional[str] = Field(
        default=None,
        description="Explicit API authorization key. Overrides environment variable lookup."
    )
    api_key_env_var: Optional[str] = Field(
        default=None,
        description="Environment variable containing the authentication credential (e.g., GEMINI_API_KEY)."
    )
    api_key_file: Optional[str] = Field(
        default=None,
        description="Path to a separate file containing the raw API token/key (e.g., ~/.gemini/token)."
    )
    options: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary network-level and proxy parameters."
    )

    async def resolve_api_key(self) -> Optional[str]:
        """
        Asynchronously resolves the API key / token from credentials priority:
        1. Explicitly configured api_key string.
        2. api_key_file path (reads content asynchronously).
        3. api_key_env_var name (looks up environment variable).
        """
        if self.api_key:
            return self.api_key

        if self.api_key_file:
            resolved_path = Path(self.api_key_file).expanduser()
            if resolved_path.is_file():
                try:
                    async with aiofiles.open(resolved_path, mode="r", encoding="utf-8") as f:
                        content = await f.read()
                    return content.strip()
                except Exception as e:
                    raise SettingsLoadError(f"Failed to read API key from configured file '{self.api_key_file}'", path=resolved_path, details=str(e)) from e

        if self.api_key_env_var:
            return os.getenv(self.api_key_env_var)

        return None


class ModelCapabilityConfig(BaseModel):
    """
    Capability flags for features supported by the model.
    Used to dynamically adjust request payloads to avoid provider-side validation errors.
    """
    supports_temperature: bool = Field(
        default=True,
        description="True if model supports sampling temperature overrides. False for models like o1."
    )
    supports_reasoning: bool = Field(
        default=False,
        description="True if model supports native reasoning token output settings or thinking budgets."
    )
    supports_tools: bool = Field(
        default=True,
        description="True if model supports native tool/function calling."
    )
    supports_json_mode: bool = Field(
        default=False,
        description="True if model supports structured JSON output schemas natively."
    )


class ModelLimitConfig(BaseModel):
    """
    Context window boundaries and output token constraints.
    """
    context_tokens: int = Field(
        ...,
        gt=0,
        description="Total available input + output token window."
    )
    max_input_tokens: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional input token limit to prevent excessive cache or costs."
    )
    max_output_tokens: int = Field(
        ...,
        gt=0,
        description="Hard ceiling limit on generated tokens per single request."
    )


class ModelDefinition(BaseModel):
    """
    The rich metadata entry of a registered model in the Model Registry.
    """
    name: str = Field(
        ...,
        description="Fully qualified identifier, e.g., 'google/gemini-3.5-flash'."
    )
    provider: str = Field(
        ...,
        description="LLM gateway provider, e.g., 'anthropic', 'openai', 'gemini'."
    )
    capabilities: ModelCapabilityConfig = Field(
        default_factory=ModelCapabilityConfig,
        description="Feature capabilities mapping."
    )
    limits: ModelLimitConfig = Field(
        ...,
        description="Context boundaries and token caps."
    )
    options: Dict[str, Any] = Field(
        default_factory=dict,
        description="Arbitrary pass-through parameters forwarded directly to the API endpoint."
    )


class EngineDaemonSettings(BaseSettings):
    """
    The central Pydantic Settings class loading cascading config files.
    All required fields must be populated by the config loader pipeline at startup.
    """
    socket_path: str = Field(
        ...,
        description="The Unix Domain Socket path the background daemon binds and listens to."
    )
    model: str = Field(
        ...,
        description="The primary high-capability model used for planning, coding, and code audits."
    )
    small_model: Optional[str] = Field(
        default=None,
        description="The secondary, high-speed model used for fast acknowledgments, summaries, and title naming."
    )
    providers: Dict[str, ProviderSettings] = Field(
        default_factory=dict,
        description="Configuration and credentials indexed by provider key."
    )
    context_filenames: List[str] = Field(
        ...,
        description="Target memory filenames to look for hierarchically."
    )
    global_context_dir: str = Field(
        ...,
        description="Path to the global workspace instructions directory."
    )
    requires_approval: bool = Field(
        ...,
        description="Whether tool execution actions require human confirmation."
    )

    @field_validator("socket_path", mode="after")
    @classmethod
    def expand_socket_path(cls, v: str) -> str:
        """Type-safe path expansion utilizing Pydantic validator instead of raw dict logic."""
        return str(Path(v).expanduser())


class PartialDaemonSettings(BaseModel):
    """
    Helper schema representing optional overrides.
    Validates structure and types prior to merging.
    """
    socket_path: Optional[str] = None
    model: Optional[str] = None
    small_model: Optional[str] = None
    providers: Optional[Dict[str, ProviderSettings]] = None
    context_filenames: Optional[List[str]] = None
    global_context_dir: Optional[str] = None
    requires_approval: Optional[bool] = None


def deep_merge(target: Dict[str, Any], source: Dict[str, Any]) -> None:
    """
    In-place deep merge of source dictionary into target dictionary.
    Excludes None values from the source.
    """
    for key, value in source.items():
        if value is None:
            continue
        if isinstance(value, dict) and key in target and isinstance(target[key], dict):
            deep_merge(target[key], value)
        else:
            target[key] = value


class SettingsManager:
    """
    Manages loading, merging, and validating configuration files with strict precedence.
    No default configurations are hardcoded within the Python source code.
    All operations are fully asynchronous and leverage Pydantic's C-parsed schema validations.
    """
    def __init__(self, default_path: Path, user_path: Path, project_path: Path):
        self.default_path = Path(default_path)
        self.user_path = Path(user_path)
        self.project_path = Path(project_path)
        self._settings: Optional[EngineDaemonSettings] = None

    @property
    def settings(self) -> EngineDaemonSettings:
        """Returns the loaded settings object. Raises if accessed before loading (Rule 1)."""
        if self._settings is None:
            raise SettingsLoadError("Settings accessed before loading. Call load_settings() first.")
        return self._settings

    async def load_settings(self) -> EngineDaemonSettings:
        """
        Asynchronously loads, merges, and validates the configuration cascade.
        Precedence: Project Local > User Global > Bundled Defaults.
        """
        # Rule 4: Early guard check for critical default config existence
        if not self.default_path.is_file():
            raise SettingsLoadError("Critical default settings file is missing.", path=self.default_path)

        # Read default settings asynchronously
        try:
            async with aiofiles.open(self.default_path, mode="r", encoding="utf-8") as f:
                content = await f.read()
            # Validate defaults schema directly with Pydantic
            defaults_model = EngineDaemonSettings.model_validate_json(content)
            merged_dict = defaults_model.model_dump()
        except Exception as e:
            raise SettingsLoadError("Failed to parse bundled default settings.", path=self.default_path, details=str(e)) from e

        # Resolve user settings override if exists
        user_resolved_path = self.user_path.expanduser()
        if user_resolved_path.is_file():
            try:
                async with aiofiles.open(user_resolved_path, mode="r", encoding="utf-8") as f:
                    content = await f.read()
                # Validate user overrides schema using Partial helper model
                user_overrides = PartialDaemonSettings.model_validate_json(content).model_dump(exclude_unset=True)
                deep_merge(merged_dict, user_overrides)
            except Exception as e:
                # Fail loudly with diagnostic details (Rule 1)
                raise SettingsLoadError("Global user settings file is corrupted.", path=user_resolved_path, details=str(e)) from e

        # Resolve project settings override if exists
        project_resolved_path = self.project_path.expanduser()
        if project_resolved_path.is_file():
            try:
                async with aiofiles.open(project_resolved_path, mode="r", encoding="utf-8") as f:
                    content = await f.read()
                # Validate project overrides schema using Partial helper model
                project_overrides = PartialDaemonSettings.model_validate_json(content).model_dump(exclude_unset=True)
                deep_merge(merged_dict, project_overrides)
            except Exception as e:
                raise SettingsLoadError("Project local settings file is corrupted.", path=project_resolved_path, details=str(e)) from e

        # Final pass validation of fully resolved configuration cascade
        try:
            self._settings = EngineDaemonSettings.model_validate(merged_dict)
            return self._settings
        except Exception as e:
            raise SettingsLoadError("Schema validation failed for merged configuration cascade.", details=str(e)) from e

    async def dump_default_settings(self, target_path: Path) -> None:
        """
        Asynchronously reads the bundled default settings, validates them against the
        EngineDaemonSettings type schema to guarantee correctness without any hardcoded logic,
        and serializes them into the target configuration path.
        """
        # Rule 4: Early guard check to fail early
        if not self.default_path.is_file():
            raise SettingsLoadError(
                "Critical default settings file is missing.", 
                path=self.default_path
            )

        # Rule 7: Always leverage asynchronous scoped context managers for file I/O
        try:
            async with aiofiles.open(self.default_path, mode="r", encoding="utf-8") as f:
                content = await f.read()
            
            # Rule 6: Strong typing verification. Validate the bundled defaults directly 
            # against our established Pydantic model.
            defaults = EngineDaemonSettings.model_validate_json(content)
        except Exception as e:
            # Rule 1: Fail loudly, fail early, and embed rich diagnostic context
            raise SettingsLoadError(
                "Failed to parse bundled default settings.", 
                path=self.default_path, 
                details=str(e)
            ) from e

        # Rule 3: Keep low-level utilities parameter-agnostic; operate on the injected path
        resolved_path = Path(target_path).expanduser()
        
        # Rule 13: Ensure operations are safe and idempotent
        resolved_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            async with aiofiles.open(resolved_path, mode="w", encoding="utf-8") as f:
                # Rule 6: Use Pydantic's serialization API to output a beautifully indented template JSON
                await f.write(defaults.model_dump_json(indent=2))
        except Exception as e:
            raise SettingsLoadError(
                f"Failed to write default settings template to '{target_path}'", 
                path=resolved_path, 
                details=str(e)
            ) from e

