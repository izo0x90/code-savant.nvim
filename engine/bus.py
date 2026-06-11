import asyncio
import uuid
import sys
import traceback
from typing import Any, Callable, Coroutine, Dict, List, Optional, Union
from engine.types import EventEnvelope, EventType

# Type alias for asynchronous event listeners
AsyncListener = Callable[[Dict[str, Any]], Coroutine[Any, Any, None]]


class MessageBus:
    """
    Asynchronous MessageBus replicating chunk-DN4XSYRG.js (Lines 311730-311900).
    Uses non-blocking asyncio callback loops and Future correlation maps.
    Strictly UI-decoupled: contains zero console print statements except stderr for subscriber exceptions.
    """

    def __init__(self, name: str = "main", parent: Optional["MessageBus"] = None):
        self.name = name
        self.parent = parent
        self.listeners: Dict[str, List[AsyncListener]] = {}
        self._pending_futures: Dict[str, asyncio.Future] = {}

    def derive(self, subagent_name: str) -> "MessageBus":
        """
        Derives a nested subagent message bus.
        Binds publish actions back to the parent while intercepting confirmation requests
        to prefix the hierarchical subagent context name (e.g., 'parent/child').
        """
        child_bus = MessageBus(name=f"{self.name}/{subagent_name}", parent=self)

        async def subagent_publish(message: Union[Dict[str, Any], EventEnvelope[Any]]) -> None:
            # Handle EventEnvelope normalization for hierarchical delegation
            if isinstance(message, EventEnvelope):
                # If it's an EventEnvelope, create a dictionary and decorate it
                raw_dict = message.to_dict()
            else:
                raw_dict = message

            raw_dict.setdefault("sender", child_bus.name)
            if raw_dict.get("type") == "tool-confirmation-request":
                subagent_path = raw_dict.get("subagent")
                if subagent_path:
                    raw_dict["subagent"] = f"{subagent_name}/{subagent_path}"
                else:
                    raw_dict["subagent"] = subagent_name
            
            # Delegate publish to parent bus
            await self.publish(raw_dict)

        # Map methods to share registrations while routing publishers up the hierarchy
        child_bus.publish = subagent_publish
        child_bus.subscribe = self.subscribe
        child_bus.unsubscribe = self.unsubscribe
        child_bus.request = self.request
        return child_bus

    def subscribe(self, event_type: str, listener: AsyncListener) -> None:
        """Registers an asynchronous listener to a specific event type."""
        if event_type not in self.listeners:
            self.listeners[event_type] = []
        self.listeners[event_type].append(listener)

    def unsubscribe(self, event_type: str, listener: AsyncListener) -> None:
        """Removes a registered listener from an event type."""
        if event_type in self.listeners and listener in self.listeners[event_type]:
            self.listeners[event_type].remove(listener)

    async def publish(self, message: Union[Dict[str, Any], EventEnvelope[Any]]) -> None:
        """
        Dispatches an event payload concurrently to all listeners.
        Resolves matching active requests if a correlationId exists.
        """
        # Normalize EventEnvelope to dict for backward compatibility with dictionary listeners
        if isinstance(message, EventEnvelope):
            raw_dict = message.to_dict()
        else:
            raw_dict = message

        raw_dict.setdefault("sender", self.name)
        event_type = raw_dict.get("type")
        if not event_type:
            return

        correlation_id = raw_dict.get("correlationId")
        if correlation_id and correlation_id in self._pending_futures:
            future = self._pending_futures[correlation_id]
            if not future.done():
                future.set_result(raw_dict)

        if event_type in self.listeners:
            # Execute all async handlers concurrently in the background
            tasks = []
            for listener in list(self.listeners[event_type]):
                try:
                    tasks.append(listener(raw_dict))
                except Exception as e:
                    # Capture initialization exceptions before gathering
                    print(f"[MessageBus] Exception during listener invocation setup:", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)

            if tasks:
                # Key Finding 2.1: Resolve silent exception swallowing inside asyncio.gather
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        print(f"[MessageBus Error] Subscriber exception caught during publish:", file=sys.stderr)
                        traceback.print_exception(type(r), r, r.__traceback__, file=sys.stderr)

    async def request(self, request_payload: Dict[str, Any], response_type: str, timeout_sec: float = 60.0) -> Dict[str, Any]:
        """
        Asynchronous Request-Response pattern using correlation IDs.
        Publishes the request and suspends execution until the response arrives or timeout occurs.
        """
        correlation_id = str(uuid.uuid4())
        request_payload["correlationId"] = correlation_id

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_futures[correlation_id] = future

        try:
            await self.publish(request_payload)
            # Await future with timeout
            return await asyncio.wait_for(future, timeout=timeout_sec)
        finally:
            self._pending_futures.pop(correlation_id, None)
