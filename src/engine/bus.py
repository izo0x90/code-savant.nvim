import asyncio
import uuid
import sys
import traceback
import time
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple, Union
from engine.types import EventEnvelope, EventType
from engine.constants import EVENT_TOOL_CONFIRMATION_REQUEST

# Type alias for asynchronous event listeners
AsyncListener = Callable[[EventEnvelope[Any]], Coroutine[Any, Any, None]]


class MessageBus:
    """
    Asynchronous MessageBus replicating chunk-DN4XSYRG.js (Lines 311730-311900).
    Uses non-blocking asyncio callback loops and Future correlation maps.
    Strictly UI-decoupled: contains zero console print statements except stderr for subscriber exceptions.
    """

    def __init__(self, name: str = "main", parent: Optional["MessageBus"] = None):
        self.name: str = name
        self.parent: Optional[MessageBus] = parent
        self.listeners: Dict[str, List[AsyncListener]] = {}
        self._pending_futures: Dict[str, Tuple[asyncio.Future[Dict[str, Any]], str]] = {}
        self._subscriptions: Dict[str, Tuple[str, AsyncListener]] = {}

    def derive(self, subagent_name: str) -> "MessageBus":
        """
        Derives a nested subagent message bus.
        Binds publish actions back to the parent while intercepting confirmation requests
        to prefix the hierarchical subagent context name (e.g., 'parent/child').
        """
        if not isinstance(subagent_name, str) or not subagent_name:
            raise ValueError("subagent_name must be a non-empty string")

        child_bus = MessageBus(name=f"{self.name}/{subagent_name}", parent=self)

        async def subagent_publish(
            message: Optional[Union[Dict[str, Any], EventEnvelope[Any]]] = None,
            *,
            event: Optional[Union[Dict[str, Any], EventEnvelope[Any]]] = None
        ) -> None:
            # Accept either message or event to satisfy both Python signature and API contract schemas
            target_payload = message if message is not None else event
            if target_payload is None:
                raise ValueError("Either 'message' or 'event' must be provided to publish.")

            # Handle EventEnvelope normalization for hierarchical delegation
            if isinstance(target_payload, EventEnvelope):
                raw_dict = target_payload.to_dict()
            else:
                raw_dict = dict(target_payload)

            raw_dict.setdefault("sender", child_bus.name)
            if raw_dict.get("type") == EVENT_TOOL_CONFIRMATION_REQUEST:
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

    def subscribe(self, event_type: str, listener: AsyncListener) -> Dict[str, str]:
        """
        Registers an asynchronous listener to a specific event type.
        Complies with message_bus_subscribe contract by validating input types
        and returning a subscription object containing subscription_id.
        """
        if not isinstance(event_type, str):
            raise TypeError("event_type must be a string")
        if not event_type:
            raise ValueError("event_type must be a non-empty string")
        if not callable(listener):
            raise TypeError("listener must be a callable AsyncListener coroutine")

        if event_type not in self.listeners:
            self.listeners[event_type] = []
        
        self.listeners[event_type].append(listener)

        subscription_id = str(uuid.uuid7())
        self._subscriptions[subscription_id] = (event_type, listener)
        return {"subscription_id": subscription_id}

    def unsubscribe(self, event_type: str, listener: Union[AsyncListener, str]) -> None:
        """Removes a registered listener from an event type by callback or subscription_id."""
        if isinstance(listener, str):
            # It's a subscription_id
            sub_id = listener
            if sub_id in self._subscriptions:
                et, lst = self._subscriptions.pop(sub_id)
                if et in self.listeners and lst in self.listeners[et]:
                    self.listeners[et].remove(lst)
        else:
            # It's the callable listener itself
            if not callable(listener):
                raise TypeError("listener must be a callable AsyncListener or a subscription_id string")
            
            if event_type in self.listeners and listener in self.listeners[event_type]:
                self.listeners[event_type].remove(listener)
                
                # Clean from self._subscriptions map
                to_remove = [k for k, v in self._subscriptions.items() if v == (event_type, listener)]
                for k in to_remove:
                    self._subscriptions.pop(k, None)

    async def publish(
        self,
        message: Optional[Union[Dict[str, Any], EventEnvelope[Any]]] = None,
        *,
        event: Optional[Union[Dict[str, Any], EventEnvelope[Any]]] = None
    ) -> None:
        """
        Dispatches an event payload concurrently to all listeners.
        Resolves matching active requests if a correlationId exists.
        Supports both message and event parameters for full contract compliance.
        """
        target_payload = message if message is not None else event
        if target_payload is None:
            raise ValueError("Either 'message' or 'event' must be provided to publish.")

        if isinstance(target_payload, EventEnvelope):
            envelope = target_payload
        else:
            raw_dict = dict(target_payload)
            sender = raw_dict.setdefault("sender", self.name)
            event_type_str = raw_dict.get("type", "unknown")
            try:
                event_type = EventType(event_type_str)
            except ValueError:
                event_type = event_type_str

            correlation_id = raw_dict.get("correlationId")
            payload = raw_dict.get("payload", raw_dict)
            envelope = EventEnvelope(
                event_type=event_type,
                payload=payload,
                sender=sender,
                correlation_id=correlation_id,
                timestamp=raw_dict.get("timestamp", time.time())
            )

        raw_dict = envelope.to_dict()
        event_type = raw_dict.get("type")
        if not event_type:
            return

        correlation_id = raw_dict.get("correlationId")
        if correlation_id and correlation_id in self._pending_futures:
            future, expected_response_type = self._pending_futures[correlation_id]
            if event_type == expected_response_type:
                if not future.done():
                    future.set_result(raw_dict)

        if event_type in self.listeners:
            # Execute all async handlers concurrently in the background
            tasks = []
            for listener in list(self.listeners[event_type]):
                try:
                    tasks.append(listener(envelope))
                except Exception:
                    # Capture initialization exceptions before gathering
                    print("[MessageBus] Exception during listener invocation setup:", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)

            if tasks:
                # Key Finding 2.1: Resolve silent exception swallowing inside asyncio.gather
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        print("[MessageBus Error] Subscriber exception caught during publish:", file=sys.stderr)
                        traceback.print_exception(type(r), r, r.__traceback__, file=sys.stderr)

    async def request(self, request_payload: Dict[str, Any], response_type: str, timeout_sec: float) -> Dict[str, Any]:
        """
        Asynchronous Request-Response pattern using correlation IDs.
        Publishes the request and suspends execution until the response arrives or timeout occurs.
        """
        if not isinstance(request_payload, dict):
            raise TypeError("request_payload must be a dictionary")
        if not isinstance(response_type, str) or not response_type:
            raise ValueError("response_type must be a non-empty string")

        # Reuse existing correlationId if provided (like in guards.py) or generate new one
        correlation_id = request_payload.get("correlationId")
        if not correlation_id:
            correlation_id = str(uuid.uuid7())
            request_payload["correlationId"] = correlation_id
        else:
            correlation_id = str(correlation_id)

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_futures[correlation_id] = (future, response_type)

        try:
            await self.publish(request_payload)
            # Await future with timeout
            return await asyncio.wait_for(future, timeout=timeout_sec)
        finally:
            self._pending_futures.pop(correlation_id, None)
