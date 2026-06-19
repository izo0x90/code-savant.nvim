"""
Asynchronous Stateless Orchestration Engine.
Driven strictly by dynamic ExecutionContext passing and strategy pipelines.
"""

from __future__ import annotations
import uuid
from typing import Any, Dict, List, Optional, AsyncIterator, Union
from engine.skills import SkillManager
from engine.agents import AgentRegistry

from engine.bus import MessageBus
from engine.timer import DeadlineTimer
from engine.registry import ToolRegistry
from engine.types import (
    ToolCall,
    ThoughtChunk,
    ContentChunk,
    CompletionChunk,
    ToolExecutionOutcome,
    TurnOutcome,
    ChatMessage,
    TextPart,
    MessagePart,
    FunctionCallPart,
    FunctionResponsePart,
    ExecutionContext,
    ExecutorAgentConfig,
    MessageRole,
    LoopStatus,
    TerminationReason,
    EventEnvelope,
    EventType,
    TelemetryActivityType,
    TelemetryThoughtPayload,
    TelemetryContentPayload,
    TelemetryActivityPayload,
)
from engine.constants import COMPLETE_TASK_TOOL_NAME, DEFAULT_REQUEST_TIMEOUT
from engine.context import (
    ContextStrategy,
    DefaultPromptInputs,
    ContextSourceRepository,
    template_string,
    ChatCompressionService,
    ToolOutputTruncationService,
)
from engine.memory import HierarchicalContextManager
from engine.guards import ToolExecutionChain, UserConfirmationGuard


class AsyncToolScheduler:
    """
    Handles non-blocking concurrent tool dispatching and verification.
    Publishes all updates to the MessageBus and runs tools through stacked interceptor guards.
    """

    def __init__(
        self,
        bus: MessageBus,
        context_repo: ContextSourceRepository,
        strategy: ContextStrategy,
        timer: DeadlineTimer,
        requires_approval: bool,
    ):
        self.bus = bus
        self.context_repo = context_repo
        self.strategy = strategy
        self.timer = timer
        self.requires_approval = requires_approval

    async def schedule(
        self, function_calls: List[ToolCall], prompt_id: str, context: ExecutionContext
    ) -> ToolExecutionOutcome:
        tool_responses = []
        task_completed = False
        submitted_output = None
        aborted = False

        for call in function_calls:
            call_id = call.id
            name = call.name
            args = call.args

            block_id = uuid.uuid7()

            await self.bus.publish(
                EventEnvelope(
                    event_type=EventType.TELEMETRY_ACTIVITY,
                    payload=TelemetryActivityPayload(
                        activity_type=TelemetryActivityType.TOOL_CALL_START,
                        name=name,
                        callId=call_id,
                        args=args,
                        prompt_id=prompt_id,
                        block_id=block_id,
                    ),
                    sender=self.bus.name,
                )
            )

            tool = self.strategy.resolve_tool(name, self.context_repo)
            if not tool:
                resp = {
                    "error": f"Unauthorized tool call: '{name}' is not available to this agent."
                }
            else:
                # 1. Compile execution guards from the strategy
                guards = list(self.strategy.get_execution_guards(self.context_repo))

                # 2. Append interactive approval guard if needed
                if self.requires_approval:
                    guards.append(
                        UserConfirmationGuard(
                            timer=self.timer,
                            is_interactive=True,
                            block_id=block_id,
                            prompt_id=prompt_id,
                            timeout=DEFAULT_REQUEST_TIMEOUT,
                        )
                    )

                # 3. Execute through interceptor chain
                chain = ToolExecutionChain(guards, tool)
                try:
                    resp = await chain.execute(args, context)

                    if name == COMPLETE_TASK_TOOL_NAME and resp.get("taskCompleted"):
                        task_completed = True
                        submitted_output = resp.get("submittedOutput")
                except PermissionError as pe:
                    resp = {"error": str(pe)}
                except Exception as e:
                    err_msg = str(e) or e.__class__.__name__
                    resp = {"error": f"Unhandled tool exception: {err_msg}"}

            await self.bus.publish(
                EventEnvelope(
                    event_type=EventType.TELEMETRY_ACTIVITY,
                    payload=TelemetryActivityPayload(
                        activity_type=TelemetryActivityType.TOOL_CALL_END,
                        name=name,
                        id=call_id,
                        response=resp,
                        prompt_id=prompt_id,
                        block_id=block_id,
                    ),
                    sender=self.bus.name,
                )
            )

            tool_responses.append(
                {"functionResponse": {"name": name, "id": call_id, "response": resp}}
            )

        return ToolExecutionOutcome(
            tool_responses=tool_responses,
            task_completed=task_completed,
            submitted_output=submitted_output,
            aborted=aborted,
        )


class LocalAgentExecutor:
    """
    Asynchronous Stateless Orchestration Engine.
    Driven purely by dynamic ExecutionContext passing and strategy pipelines.
    """

    def __init__(
        self,
        definition: ExecutorAgentConfig,
        context_strategy: ContextStrategy,
        skill_manager: Optional[SkillManager] = None,
        agent_registry: Optional[AgentRegistry] = None,
        memory_manager: Optional[HierarchicalContextManager] = None,
        tool_registry: Optional[ToolRegistry] = None,
    ):
        self.definition = definition
        self.context_strategy = context_strategy
        self.registry = tool_registry or ToolRegistry()
        self.pending_hints_queue: List[str] = []
        self.skill_manager = skill_manager
        self.agent_registry = agent_registry
        self.memory_manager = memory_manager

    async def inject_steering(self, message: str, context: ExecutionContext) -> None:
        """Appends interactive guidance steering directives into queue."""
        self.pending_hints_queue.append(message)
        await context.message_bus.publish(
            EventEnvelope(
                event_type=EventType.TELEMETRY_ACTIVITY,
                payload=TelemetryActivityPayload(
                    activity_type=TelemetryActivityType.STEERING_QUEUED,
                    msg=f"Queued user steering payload: '{message}'",
                ),
                sender=context.message_bus.name,
            )
        )

    async def call_model_async(
        self, turn_counter: int, context: ExecutionContext
    ) -> AsyncIterator[Union[ThoughtChunk, CompletionChunk]]:
        """
        Delegates generation to the GenAIClient using dynamic context compile.
        """
        request_context = await self.context_strategy.compile_context(
            inputs=DefaultPromptInputs(
                is_interactive=self.definition.plan_mode,
                approval_mode="plan" if self.definition.plan_mode else "default",
                remaining_depth=context.remaining_depth,
            ),
            context_repo=ContextSourceRepository(
                workspace_path=context.workspace_path,
                tool_registry=self.registry,
                skill_manager=self.skill_manager,
                agent_registry=self.agent_registry,
                memory_manager=self.memory_manager,
            ),
            history=context.session.chat_history,
        )

        async def _client_agent_id() -> str:
            return f"agent-{self.definition.name}"

        async for chunk in context.client.generate_response_stream(
            system_prompt=request_context.system_instruction,
            chat_history=request_context.contents,
            tools_declarations=request_context.tools,
            agent_name=self.definition.name,
            turn_counter=turn_counter,
            agent_id=f"agent-{self.definition.name}",
        ):
            yield chunk

    async def execute_turn(
        self,
        current_message: ChatMessage,
        turn_counter: int,
        deadline_timer: DeadlineTimer,
        context: ExecutionContext,
    ) -> TurnOutcome:
        """Orchestrates single turn execution context compile, model generate, and scheduling."""
        prompt_id = f"agent-{self.definition.name}#{turn_counter}"

        model_message_id = uuid.uuid7()

        await context.message_bus.publish(
            EventEnvelope(
                event_type=EventType.TELEMETRY_ACTIVITY,
                payload=TelemetryActivityPayload(
                    activity_type=TelemetryActivityType.TURN_START,
                    msg=f"Starting dispatch turn cycle #{turn_counter}",
                    prompt_id=prompt_id,
                ),
                sender=context.message_bus.name,
            )
        )

        # 1. Chat compression service executed before compilation ticks
        compressor = ChatCompressionService(
            context.client, threshold=self.definition.compression_threshold
        )
        await compressor.compress_if_needed(context.session)

        # Process asynchronous streaming model response
        parts: List[MessagePart] = []
        function_calls: List[ToolCall] = []
        async for chunk in self.call_model_async(turn_counter, context):
            if isinstance(chunk, ThoughtChunk):
                await context.message_bus.publish(
                    EventEnvelope(
                        event_type=EventType.TELEMETRY_THOUGHT,
                        payload=TelemetryThoughtPayload(
                            text=chunk.text,
                            block_id=model_message_id,
                            prompt_id=prompt_id,
                            title=chunk.title,
                        ),
                        sender=context.message_bus.name,
                    )
                )
                parts.append(chunk)
            elif isinstance(chunk, ContentChunk):
                await context.message_bus.publish(
                    EventEnvelope(
                        event_type=EventType.TELEMETRY_CONTENT,
                        payload=TelemetryContentPayload(
                            text=chunk.text,
                            block_id=model_message_id,
                            prompt_id=prompt_id,
                        ),
                        sender=context.message_bus.name,
                    )
                )
                parts.append(TextPart(text=chunk.text))
            elif isinstance(chunk, CompletionChunk):
                if chunk.function_calls:
                    function_calls.extend(chunk.function_calls)
                    for f in chunk.function_calls:
                        parts.append(
                            FunctionCallPart(
                                name=f.name,
                                args=f.args,
                                id=f.id,
                                thought_signature=chunk.thought_signature,
                            )
                        )

        # Append model message turn containing ALL parts in-order
        model_msg = ChatMessage(
            id=model_message_id, role=MessageRole.MODEL.value, parts=parts
        )
        await context.session.append_message(model_msg)

        # 💾 BATCH SAVE 1: Guarantee model response & thoughts are written to disk
        await context.session_manager.save_session(context.session)

        # Handle direct thought/text output with no tools
        if not function_calls:
            final_text = "".join([p.text for p in parts if isinstance(p, TextPart)])
            await context.message_bus.publish(
                EventEnvelope(
                    event_type=EventType.TELEMETRY_ACTIVITY,
                    payload=TelemetryActivityPayload(
                        activity_type=TelemetryActivityType.STOP,
                        msg="Model generated completion response with zero tool dispatches. Stopping loop.",
                        prompt_id=prompt_id,
                    ),
                    sender=context.message_bus.name,
                )
            )
            return TurnOutcome(
                status=LoopStatus.STATUS_STOP.value,
                terminate_reason=TerminationReason.REASON_GOAL.value,
                final_result=final_text,
            )

        # Delegate execution to dedicated ToolScheduler
        scheduler = AsyncToolScheduler(
            bus=context.message_bus,
            context_repo=ContextSourceRepository(
                workspace_path=context.workspace_path,
                tool_registry=self.registry,
                skill_manager=self.skill_manager,
                agent_registry=self.agent_registry,
                memory_manager=self.memory_manager,
            ),
            strategy=self.context_strategy,
            timer=deadline_timer,
            requires_approval=self.definition.requires_approval,
        )
        outcome = await scheduler.schedule(function_calls, prompt_id, context)

        user_msg_parts = []
        for tr in outcome.tool_responses:
            fr = tr["functionResponse"]
            user_msg_parts.append(
                FunctionResponsePart(name=fr["name"], response=fr["response"])
            )

        user_msg = ChatMessage(role=MessageRole.USER.value, parts=user_msg_parts)

        # 2. Tool output truncation service executed post-scheduler dispatch
        truncator = ToolOutputTruncationService()
        sanitized_user_msg = await truncator.truncate_if_needed(
            user_msg, context.workspace_path
        )

        next_message = sanitized_user_msg
        await context.session.append_message(sanitized_user_msg)

        # 💾 BATCH SAVE 2: Write tool results to disk!
        await context.session_manager.save_session(context.session)

        if outcome.task_completed:
            return TurnOutcome(
                status=LoopStatus.STATUS_STOP.value,
                terminate_reason=TerminationReason.REASON_GOAL.value,
                final_result=outcome.submitted_output,
            )

        if outcome.aborted:
            return TurnOutcome(
                status=LoopStatus.STATUS_STOP.value,
                terminate_reason=TerminationReason.REASON_ABORTED.value,
                final_result=None,
            )

        return TurnOutcome(
            status=LoopStatus.STATUS_CONTINUE.value,
            terminate_reason=None,
            next_message=next_message,
        )

    async def execute_final_warning_turn(
        self, turn_counter: int, context: ExecutionContext, reason: str
    ) -> Optional[str]:
        """Implements final grace warning fallback turn with genuine model inference."""
        prompt_id = f"agent-{self.definition.name}#{turn_counter}"
        await context.message_bus.publish(
            EventEnvelope(
                event_type=EventType.TELEMETRY_ACTIVITY,
                payload=TelemetryActivityPayload(
                    activity_type=TelemetryActivityType.RECOVERY,
                    msg=f"Execution bounds exceeded due to {reason}. Granting final recovery turn...",
                    prompt_id=prompt_id,
                ),
                sender=context.message_bus.name,
            )
        )

        # Retrieve strategy-driven warning prompt
        warning_prompt = self.context_strategy.compile_recovery_prompt(reason)

        # Append User warning to session history
        user_warning = ChatMessage(
            role=MessageRole.USER.value, parts=[TextPart(text=warning_prompt)]
        )
        await context.session.append_message(user_warning)

        # Setup isolated grace deadline timer from config
        grace_timer = DeadlineTimer(float(self.definition.recovery_time_seconds))
        grace_timer.start()

        # Execute genuine model turn under grace timer
        try:
            turn_result = await self.execute_turn(
                user_warning, turn_counter, grace_timer, context
            )
            if (
                turn_result.status == LoopStatus.STATUS_STOP.value
                and turn_result.terminate_reason == TerminationReason.REASON_GOAL.value
            ):
                return turn_result.final_result
        except Exception as e:
            await context.message_bus.publish(
                EventEnvelope(
                    event_type=EventType.TELEMETRY_ACTIVITY,
                    payload=TelemetryActivityPayload(
                        activity_type=TelemetryActivityType.RECOVERY_FAILED,
                        msg=f"Recovery turn aborted due to error: {e}",
                        prompt_id=prompt_id,
                    ),
                    sender=context.message_bus.name,
                )
            )
        finally:
            grace_timer.stop()

        return None

    async def run(
        self, context: ExecutionContext, inputs: Dict[str, Any]
    ) -> Optional[str]:
        """Recreates runInternal async execution loop in a completely stateless manner."""
        for t_name in self.registry.get_all_tool_names():
            t = self.registry.get_tool(t_name)
            if t:
                t.message_bus = context.message_bus

        max_turns = self.definition.max_turns
        max_time_sec = self.definition.max_time_seconds

        deadline_timer = DeadlineTimer(max_time_sec)

        turn_counter = 0
        terminate_reason = TerminationReason.REASON_ERROR.value
        final_result = None

        query_text = template_string(self.definition.query, inputs)

        # Append initial query or continue instruction directly to stateful session
        if not context.session.chat_history:
            await context.session.append_message(
                ChatMessage(
                    role=MessageRole.USER.value, parts=[TextPart(text=query_text)]
                )
            )
        else:
            last_msg = context.session.chat_history[-1]
            if last_msg.role != MessageRole.USER.value:
                await context.session.append_message(
                    ChatMessage(
                        role=MessageRole.USER.value,
                        parts=[TextPart(text="Continue task execution.")],
                    )
                )

        current_message = context.session.chat_history[-1]

        # Compile start request context once for logging system prompt metadata
        request_context = await self.context_strategy.compile_context(
            inputs=DefaultPromptInputs(
                is_interactive=self.definition.plan_mode,
                approval_mode="plan" if self.definition.plan_mode else "default",
                remaining_depth=context.remaining_depth,
            ),
            context_repo=ContextSourceRepository(
                workspace_path=context.workspace_path,
                tool_registry=self.registry,
                skill_manager=self.skill_manager,
                agent_registry=self.agent_registry,
                memory_manager=self.memory_manager,
            ),
            history=context.session.chat_history,
        )
        system_prompt = request_context.system_instruction

        prompt_id = f"agent-{self.definition.name}#{turn_counter}"
        await context.message_bus.publish(
            EventEnvelope(
                event_type=EventType.TELEMETRY_ACTIVITY,
                payload=TelemetryActivityPayload(
                    activity_type=TelemetryActivityType.START,
                    msg=f"Starting asynchronous agent loop: '{self.definition.name}'",
                    system_prompt=system_prompt,
                    query=query_text,
                    prompt_id=prompt_id,
                ),
                sender=context.message_bus.name,
            )
        )

        try:
            while True:
                deadline_timer.start()

                if turn_counter >= max_turns:
                    terminate_reason = TerminationReason.REASON_MAX_TURNS.value
                    break

                if deadline_timer.is_triggered:
                    terminate_reason = TerminationReason.REASON_TIMEOUT.value
                    break

                turn_result = await self.execute_turn(
                    current_message, turn_counter, deadline_timer, context
                )
                turn_counter += 1

                if turn_result.status == LoopStatus.STATUS_STOP.value:
                    if self.pending_hints_queue:
                        # User has injected manual steering: intercept the stop and force-continue the loop!
                        turn_result = TurnOutcome(
                            status=LoopStatus.STATUS_CONTINUE.value,
                            terminate_reason=None,
                            next_message=turn_result.next_message,
                        )
                    else:
                        terminate_reason = (
                            turn_result.terminate_reason
                            or TerminationReason.REASON_ERROR.value
                        )
                        if turn_result.final_result:
                            final_result = turn_result.final_result
                        break

                current_message = turn_result.next_message

                if self.pending_hints_queue:
                    hints = "\n".join(self.pending_hints_queue)
                    self.pending_hints_queue.clear()

                    await context.message_bus.publish(
                        EventEnvelope(
                            event_type=EventType.TELEMETRY_ACTIVITY,
                            payload=TelemetryActivityPayload(
                                activity_type=TelemetryActivityType.STEERING_INJECTED,
                                msg=f"Injecting manual user directive: '{hints}'",
                                prompt_id=f"agent-{self.definition.name}#{turn_counter - 1}",
                            ),
                            sender=context.message_bus.name,
                        )
                    )
                    steering_prompt = self.context_strategy.compile_steering_prompt(
                        hints
                    )
                    steer_msg = ChatMessage(
                        role=MessageRole.USER.value,
                        parts=[TextPart(text=steering_prompt)],
                    )
                    await context.session.append_message(steer_msg)
                    current_message = steer_msg

        finally:
            deadline_timer.stop()

        if terminate_reason in [
            TerminationReason.REASON_MAX_TURNS.value,
            TerminationReason.REASON_TIMEOUT.value,
        ]:
            recovery_output = await self.execute_final_warning_turn(
                turn_counter, context, terminate_reason
            )
            if recovery_output:
                terminate_reason = TerminationReason.REASON_GOAL.value
                final_result = recovery_output
            else:
                final_result = f"Failed to complete: terminated with {terminate_reason}"

        await context.message_bus.publish(
            EventEnvelope(
                event_type=EventType.TELEMETRY_ACTIVITY,
                payload=TelemetryActivityPayload(
                    activity_type=TelemetryActivityType.END,
                    msg=f"Asynchronous loop execution finished: {terminate_reason}",
                    response=final_result,
                    prompt_id=f"agent-{self.definition.name}#{turn_counter - 1}",
                ),
                sender=context.message_bus.name,
            )
        )

        return final_result
