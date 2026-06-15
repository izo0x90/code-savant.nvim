"""
Polymorphic model client implementations.
Keeps Live API interactions completely separate from high-fidelity Mocking.
Operates strictly via dependency injection; no internal environment variable reading.
"""

from typing import AsyncIterator, List, Union
from google import genai
from google.genai import types

from engine.types import (
    ToolCall,
    ThoughtChunk,
    CompletionChunk,
    ChatMessage,
    TextPart,
    FunctionCallPart,
    FunctionResponsePart,
    FunctionDeclarationSpec
)
from engine.constants import DEFAULT_MODEL_NAME


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
        agent_id: str = ""
    ) -> AsyncIterator[Union[ThoughtChunk, CompletionChunk]]:
        """Streams thought blocks and function call structures asynchronously."""
        raise NotImplementedError
        # To make it technically an async generator and satisfy linters
        yield  # type: ignore


class LiveGenAIClient(BaseGenAIClient):
    """
    Pure client implementation using the official google-genai SDK.
    All credentials are fully injected; no os.getenv calls allowed.
    """

    def __init__(self, api_key: str, model_name: str = DEFAULT_MODEL_NAME):
        if not api_key:
            raise ValueError("LiveGenAIClient requires a valid api_key.")
        self.api_key = api_key
        self.model_name = model_name
        self._client = genai.Client(api_key=self.api_key)

    async def generate_response_stream(
        self,
        system_prompt: str,
        chat_history: List[ChatMessage],
        tools_declarations: List[FunctionDeclarationSpec],
        agent_name: str = "",
        turn_counter: int = 0,
        agent_id: str = ""
    ) -> AsyncIterator[Union[ThoughtChunk, CompletionChunk]]:
        # Map chat history to types.Content objects
        contents = []
        for turn in chat_history:
            role = turn.role
            parts = []
            for part in turn.parts:
                if isinstance(part, TextPart):
                    parts.append(types.Part.from_text(text=part.text))
                elif isinstance(part, FunctionCallPart):
                    parts.append(types.Part.from_function_call(
                        name=part.name,
                        args=part.args
                    ))
                elif isinstance(part, FunctionResponsePart):
                    parts.append(types.Part.from_function_response(
                        name=part.name,
                        response=part.response
                    ))
            contents.append(types.Content(role=role, parts=parts))

        config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            tools=[types.Tool(function_declarations=[t.model_dump() for t in tools_declarations])] if tools_declarations else [],
            temperature=0.0
        )

        response_stream = await self._client.aio.models.generate_content_stream(
            model=self.model_name,
            contents=contents,
            config=config
        )

        async for response_chunk in response_stream:
            text = response_chunk.text
            if text:
                yield ThoughtChunk(text=text)
            
            if response_chunk.function_calls:
                f_calls = []
                for fc in response_chunk.function_calls:
                    f_calls.append(ToolCall(
                        name=fc.name,
                        args=fc.args or {},
                        id=getattr(fc, "id", f"call-{agent_id}")
                    ))
                yield CompletionChunk(function_calls=f_calls)


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
        agent_id: str = ""
    ) -> AsyncIterator[Union[ThoughtChunk, CompletionChunk]]:
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
                yield CompletionChunk(function_calls=[
                    ToolCall(
                        name="read_file",
                        args={"file_path": target_file},
                        id=f"call-{agent_id}-{turn_counter}"
                    )
                ])
            elif turn_counter == 1:
                yield ThoughtChunk(text="Rewriting calculation logic in replace buffer.")
                yield CompletionChunk(function_calls=[
                    ToolCall(
                        name="replace",
                        args={
                            "file_path": target_file,
                            "instruction": "Replace TODO with computation logic.",
                            "old_string": "def calculate_area(a: float, b: float) -> float:\n    return a + b",
                            "new_string": "def calculate_area(a: float, b: float) -> float:\n    return a * b  # Corrected calculation!"
                        },
                        id=f"call-{agent_id}-{turn_counter}"
                    )
                ])
            else:
                yield ThoughtChunk(text="Filing output results to coordinator.")
                yield CompletionChunk(function_calls=[
                    ToolCall(
                        name="complete_task",
                        args={"result": f"Subagent successfully edited {target_file} and implemented correct computation rules."},
                        id=f"call-{agent_id}-done"
                    )
                ])
            return

        # ----------------------------------------------------------------------
        # Parent "refactor_helper" simulated flow
        # ----------------------------------------------------------------------
        if turn_counter == 0:
            yield ThoughtChunk(text="Finding active tasks in local workspace.")
            yield CompletionChunk(function_calls=[
                ToolCall(
                    name="grep_search",
                    args={"pattern": "TODO"},
                    id=f"call-{agent_id}-{turn_counter}"
                )
            ])
        elif turn_counter == 1:
            yield ThoughtChunk(text="Spawning isolated subagent 'coder' to rewrite compute files.")
            yield CompletionChunk(function_calls=[
                ToolCall(
                    name="agent",
                    args={
                        "agent_name": "coder",
                        "prompt": f"Please edit {target_file} and replace the TODO comment with actual calculation logic."
                    },
                    id=f"call-{agent_id}-{turn_counter}"
                )
            ])
        else:
            # Extract subagent outcome
            subagent_outcome = "Success"
            for turn in reversed(chat_history):
                if turn.role == "user":
                    for part in turn.parts:
                        if isinstance(part, FunctionResponsePart):
                            resp = part.response
                            if isinstance(resp, dict) and resp.get("agent_name") == "coder":
                                outcome_val = resp.get("outcome", "Completed successfully.")
                                if isinstance(outcome_val, dict):
                                    subagent_outcome = outcome_val.get("outcome", str(outcome_val))
                                else:
                                    subagent_outcome = str(outcome_val)

            yield ThoughtChunk(text="Compiling final execution reports.")
            yield CompletionChunk(function_calls=[
                ToolCall(
                    name="complete_task",
                    args={"result": f"Orchestrator finished task! Nested subagent outcome: {subagent_outcome}"},
                    id=f"call-{agent_id}-done"
                )
            ])
export = [BaseGenAIClient, LiveGenAIClient, MockGenAIClient]
