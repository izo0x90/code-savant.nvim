"""
Polymorphic model client implementations.
Keeps Live API interactions completely separate from high-fidelity Mocking.
Operates strictly via dependency injection; no internal environment variable reading.
"""

from typing import Any, AsyncIterator, Dict, List, Optional, Union
from google import genai
from google.genai import types

from engine.types import (
    ToolCall,
    ParsedThought,
    ThoughtChunk,
    ContentChunk,
    CompletionChunk,
    ChatMessage,
    TextPart,
    FunctionCallPart,
    FunctionResponsePart,
    FunctionDeclarationSpec,
)
from engine.constants import DEFAULT_MODEL_NAME
from engine.config import ModelCapabilityConfig, ModelLimitConfig


THOUGHT_HEADER_DELIMITER = "**"


def parse_thought(raw_text: str) -> ParsedThought:
    """
    Parses and extracts the bolded subject header from a raw streaming thought text block.
    Maintains a completely flat execution flow using clear guard clauses (Rule 4, Rule 8).
    """
    cleaned_text = raw_text.strip()

    # Pre-condition validation / early returns (Rule 4)
    if not cleaned_text:
        return ParsedThought(subject="", description="")

    start_idx = cleaned_text.find(THOUGHT_HEADER_DELIMITER)
    if start_idx == -1:
        return ParsedThought(subject="", description=cleaned_text)

    end_idx = cleaned_text.find(
        THOUGHT_HEADER_DELIMITER, start_idx + len(THOUGHT_HEADER_DELIMITER)
    )
    if end_idx == -1:
        return ParsedThought(subject="", description=cleaned_text)

    # Flat, successful extraction path
    subject = cleaned_text[start_idx + len(THOUGHT_HEADER_DELIMITER) : end_idx].strip()
    description = (
        cleaned_text[:start_idx]
        + cleaned_text[end_idx + len(THOUGHT_HEADER_DELIMITER) :]
    ).strip()

    return ParsedThought(subject=subject, description=description)


class BaseGenAIClient:
    """
    Abstract Interface for GenAI model communications.
    """

    async def generate_response_stream(
        self,
        system_prompt: str,
        chat_history: List[ChatMessage],
        tools_declarations: List[FunctionDeclarationSpec],
        agent_name: str = "",
        turn_counter: int = 0,
        agent_id: str = "",
    ) -> AsyncIterator[Union[ThoughtChunk, ContentChunk, CompletionChunk]]:
        """Streams thought blocks and function call structures asynchronously."""
        raise NotImplementedError
        # To make it technically an async generator and satisfy linters
        yield  # type: ignore


class LiveGenAIClient(BaseGenAIClient):
    """
    Pure client implementation using the official google-genai SDK.
    All credentials are fully injected; no os.getenv calls allowed.
    """

    def __init__(
        self,
        api_key: str,
        model_name: str = DEFAULT_MODEL_NAME,
        base_url: Optional[str] = None,
        capabilities: Optional[ModelCapabilityConfig] = None,
        limits: Optional[ModelLimitConfig] = None,
        options: Optional[Dict[str, Any]] = None,
        provider_options: Optional[Dict[str, Any]] = None,
    ):
        if not api_key:
            raise ValueError("LiveGenAIClient requires a valid api_key.")
        self.api_key = api_key
        self.model_name = model_name
        self.capabilities = capabilities
        self.limits = limits
        self.options = options or {}

        client_kwargs = {"api_key": self.api_key}

        # Build http_options if base_url or provider-level http options exist
        http_opts = {}
        if base_url:
            http_opts["base_url"] = base_url
            http_opts["base_url_resource_scope"] = types.ResourceScope.COLLECTION

        if provider_options:
            valid_http_fields = getattr(types.HttpOptions, "model_fields", {})
            for k, v in provider_options.items():
                if k in valid_http_fields:
                    http_opts[k] = v

        if http_opts:
            client_kwargs["http_options"] = types.HttpOptions(**http_opts)

        self._client = genai.Client(**client_kwargs)

    async def generate_response_stream(
        self,
        system_prompt: str,
        chat_history: List[ChatMessage],
        tools_declarations: List[FunctionDeclarationSpec],
        agent_name: str = "",
        turn_counter: int = 0,
        agent_id: str = "",
    ) -> AsyncIterator[Union[ThoughtChunk, ContentChunk, CompletionChunk]]:
        # Map chat history to types.Content objects
        contents = []
        for turn in chat_history:
            role = turn.role
            parts = []
            for part in turn.parts:
                if isinstance(part, TextPart):
                    parts.append(types.Part.from_text(text=part.text))
                elif isinstance(part, FunctionCallPart):
                    # Bypassing standard helper to append thought_signature bytes directly!
                    sig_bytes = None
                    if part.thought_signature:
                        try:
                            import base64

                            sig_bytes = base64.b64decode(part.thought_signature)
                        except Exception:
                            sig_bytes = b"skip_thought_signature_validator"
                    else:
                        sig_bytes = b"skip_thought_signature_validator"

                    parts.append(
                        types.Part(
                            function_call=types.FunctionCall(
                                name=part.name, args=part.args, id=part.call_id
                            ),
                            thought_signature=sig_bytes,
                        )
                    )
                elif isinstance(part, ThoughtChunk):
                    parts.append(types.Part(text=part.text, thought=True))
                elif isinstance(part, FunctionResponsePart):
                    parts.append(
                        types.Part.from_function_response(
                            name=part.name, response=part.response
                        )
                    )
            contents.append(types.Content(role=role, parts=parts))

        # Build GenerateContentConfig based on capabilities, limits and model options
        config_kwargs = {
            "system_instruction": system_prompt,
        }

        # Handle temperature option based on capabilities
        if not self.capabilities or self.capabilities.supports_temperature:
            config_kwargs["temperature"] = 0.0

        # Handle tools option based on capabilities
        if tools_declarations and (
            not self.capabilities or self.capabilities.supports_tools
        ):
            config_kwargs["tools"] = [
                types.Tool(
                    function_declarations=[t.model_dump() for t in tools_declarations]
                )
            ]

        # Handle limits
        if self.limits and self.limits.max_output_tokens:
            config_kwargs["max_output_tokens"] = self.limits.max_output_tokens

        # Merge model-level options if they are valid GenerateContentConfig fields
        if self.options:
            valid_config_fields = getattr(
                types.GenerateContentConfig, "model_fields", {}
            )
            for k, v in self.options.items():
                if k in valid_config_fields:
                    config_kwargs[k] = v

        config = types.GenerateContentConfig(**config_kwargs)

        response_stream = await self._client.aio.models.generate_content_stream(
            model=self.model_name, contents=contents, config=config
        )

        import base64

        async for response_chunk in response_stream:
            yielded_from_parts = False
            if response_chunk.candidates:
                for cand in response_chunk.candidates:
                    if cand.content and cand.content.parts:
                        for part in cand.content.parts:
                            is_thought = getattr(part, "thought", False)
                            part_text = getattr(part, "text", None)
                            if part_text:
                                yielded_from_parts = True
                                if is_thought:
                                    parsed = parse_thought(part_text)
                                    title = parsed.subject if parsed.subject else None
                                    yield ThoughtChunk(text=part_text, title=title)
                                else:
                                    yield ContentChunk(text=part_text)

            if not yielded_from_parts:
                text = response_chunk.text
                if text:
                    yield ContentChunk(text=text)

            sig_b64 = None
            if response_chunk.candidates:
                for cand in response_chunk.candidates:
                    if cand.content and cand.content.parts:
                        for part in cand.content.parts:
                            sig = getattr(part, "thought_signature", None)
                            if sig:
                                sig_b64 = base64.b64encode(sig).decode("utf-8")
                                break
                    if sig_b64:
                        break

            if response_chunk.function_calls:
                f_calls = []
                for fc in response_chunk.function_calls:
                    f_calls.append(
                        ToolCall(
                            name=fc.name,
                            args=fc.args or {},
                            id=getattr(fc, "id", f"call-{agent_id}"),
                            thought_signature=sig_b64,
                        )
                    )
                yield CompletionChunk(function_calls=f_calls, thought_signature=sig_b64)
            elif sig_b64:
                yield CompletionChunk(function_calls=[], thought_signature=sig_b64)


class MockGenAIClient(BaseGenAIClient):
    """
    High-fidelity candidate generator simulator for local tests and offline dev.
    Entirely decoupled from live network APIs.
    """

    async def generate_response_stream(
        self,
        system_prompt: str,
        chat_history: List[ChatMessage],
        tools_declarations: List[FunctionDeclarationSpec],
        agent_name: str = "",
        turn_counter: int = 0,
        agent_id: str = "",
    ) -> AsyncIterator[Union[ThoughtChunk, ContentChunk, CompletionChunk]]:
        import asyncio

        await asyncio.sleep(0.01)  # Simulated latency

        # ----------------------------------------------------------------------
        # Resolve target file name dynamically from prompt/chat history
        # ----------------------------------------------------------------------
        target_file = "scratch_test.py"
        for msg in chat_history:
            if msg.role in ("user", "model", "system"):
                for part in msg.parts:
                    if isinstance(part, TextPart):
                        for word in part.text.split():
                            cleaned_word = word.strip(".,'\"`();")
                            if cleaned_word.endswith(".py"):
                                target_file = cleaned_word
                                break

        # ----------------------------------------------------------------------
        # Subagent "coder" simulated flow
        # ----------------------------------------------------------------------
        if "coder" in agent_name:
            if turn_counter == 0:
                yield ThoughtChunk(text="Analyzing target compute files.")
                await asyncio.sleep(0.01)
                yield ThoughtChunk(text=f"Reading {target_file} boundary ranges.")
                yield CompletionChunk(
                    function_calls=[
                        ToolCall(
                            name="read_file",
                            args={"file_path": target_file},
                            id=f"call-{agent_id}-{turn_counter}",
                        )
                    ]
                )
            elif turn_counter == 1:
                yield ThoughtChunk(
                    text="Rewriting calculation logic in replace buffer."
                )
                yield CompletionChunk(
                    function_calls=[
                        ToolCall(
                            name="replace",
                            args={
                                "file_path": target_file,
                                "instruction": "Replace TODO with computation logic.",
                                "old_string": "def calculate_area(a: float, b: float) -> float:\n    return a + b",
                                "new_string": "def calculate_area(a: float, b: float) -> float:\n    return a * b  # Corrected calculation!",
                            },
                            id=f"call-{agent_id}-{turn_counter}",
                        )
                    ]
                )
            else:
                yield ThoughtChunk(text="Filing output results to coordinator.")
                yield CompletionChunk(
                    function_calls=[
                        ToolCall(
                            name="complete_task",
                            args={
                                "result": f"Subagent successfully edited {target_file} and implemented correct computation rules."
                            },
                            id=f"call-{agent_id}-done",
                        )
                    ]
                )
            return

        # ----------------------------------------------------------------------
        # Parent "refactor_helper" simulated flow
        # ----------------------------------------------------------------------
        if turn_counter == 0:
            yield ThoughtChunk(text="Finding active tasks in local workspace.")
            yield CompletionChunk(
                function_calls=[
                    ToolCall(
                        name="grep_search",
                        args={"pattern": "TODO"},
                        id=f"call-{agent_id}-{turn_counter}",
                    )
                ]
            )
        elif turn_counter == 1:
            yield ThoughtChunk(
                text="Spawning isolated subagent 'coder' to rewrite compute files."
            )
            yield CompletionChunk(
                function_calls=[
                    ToolCall(
                        name="agent",
                        args={
                            "agent_name": "coder",
                            "prompt": f"Please edit {target_file} and replace the TODO comment with actual calculation logic.",
                        },
                        id=f"call-{agent_id}-{turn_counter}",
                    )
                ]
            )
        else:
            # Extract subagent outcome
            subagent_outcome = "Success"
            for turn in reversed(chat_history):
                if turn.role == "user":
                    for part in turn.parts:
                        if isinstance(part, FunctionResponsePart):
                            resp = part.response
                            if (
                                isinstance(resp, dict)
                                and resp.get("agent_name") == "coder"
                            ):
                                outcome_val = resp.get(
                                    "outcome", "Completed successfully."
                                )
                                if isinstance(outcome_val, dict):
                                    subagent_outcome = outcome_val.get(
                                        "outcome", str(outcome_val)
                                    )
                                else:
                                    subagent_outcome = str(outcome_val)

            yield ThoughtChunk(text="Compiling final execution reports.")
            yield CompletionChunk(
                function_calls=[
                    ToolCall(
                        name="complete_task",
                        args={
                            "result": f"Orchestrator finished task! Nested subagent outcome: {subagent_outcome}"
                        },
                        id=f"call-{agent_id}-done",
                    )
                ]
            )


export = [BaseGenAIClient, LiveGenAIClient, MockGenAIClient]
