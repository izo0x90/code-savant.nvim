import asyncio
import uuid
import sys
import traceback
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple, Union
from engine.types import Event, EventEnvelope

# Type alias for asynchronous event listeners
AsyncListener = Callable[[EventEnvelope[Any]], Coroutine[Any, Any, None]]


class MessageBus:
    """
    Asynchronous MessageBus.
    Strictly type-safe: operates exclusively on strongly-typed EventEnvelope instances.
    """

    def __init__(self, name: str = "main", parent: Optional["MessageBus"] = None, session_id: Optional[uuid.UUID] = None):
        self.name: str = name
        self.parent: Optional[MessageBus] = parent
        self.session_id: Optional[uuid.UUID] = session_id
        self.parent_session_id: Optional[uuid.UUID] = parent.session_id if parent else None
        self.agent_name: Optional[str] = None
        self.listeners: Dict[str, List[AsyncListener]] = {}
        # Futures strictly expect and return EventEnvelope objects
        self._pending_futures: Dict[str, Tuple[asyncio.Future[EventEnvelope[Any]], str]] = {}
        self._subscriptions: Dict[str, Tuple[str, AsyncListener]] = {}
        self._children: List["MessageBus"] = []
        if parent:
            parent._children.append(self)

    def derive(self, subagent_name: str, child_session_id: Optional[uuid.UUID] = None) -> "MessageBus":
        """
        Derives a nested subagent message bus.
        """
        if not isinstance(subagent_name, str) or not subagent_name:
            raise ValueError("subagent_name must be a non-empty string")

        resolved_id = child_session_id or uuid.uuid7()
        child_bus = MessageBus(name=f"{self.name}/{subagent_name}", parent=self, session_id=resolved_id)
        child_bus.agent_name = subagent_name
        child_bus.parent_session_id = self.session_id
        return child_bus

    def subscribe(self, event_type: str, listener: AsyncListener) -> Dict[str, str]:
        """
        Registers an asynchronous listener to a specific event type.
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

    def _resolve_future(self, correlation_id: str, event_type_str: str, envelope: EventEnvelope[Any]) -> bool:
        """Finds and resolves a pending future in this bus or any of its children."""
        if correlation_id in self._pending_futures:
            future, expected_response_type = self._pending_futures[correlation_id]
            if event_type_str == expected_response_type:
                if not future.done():
                    future.set_result(envelope)
                    return True
        for child in self._children:
            if child._resolve_future(correlation_id, event_type_str, envelope):
                return True
        return False

    async def publish(self, event: Event[Any]) -> None:
        """
        Public API: Strictly takes a raw Event. Stamps it cleanly and triggers dispatch.
        """
        if not isinstance(event, Event):
            raise TypeError("MessageBus.publish strictly requires an Event instance.")

        envelope = EventEnvelope(
            event_type=event.event_type,
            payload=event.payload,
            sender=self.name,
            correlation_id=event.correlation_id,
            session_id=self.session_id,
            parent_session_id=self.parent_session_id,
            agent_name=self.agent_name or self.name,
        )
        await self._dispatch(envelope)

    async def _dispatch(self, envelope: EventEnvelope[Any]) -> None:
        """
        Internal: Propagates a fully-stamped EventEnvelope up the hierarchy.
        """
        event_type_str = envelope.event_type.value

        # Match outstanding request futures purely using typed fields (recursive traversal)
        if envelope.correlation_id:
            root = self
            while root.parent:
                root = root.parent
            root._resolve_future(envelope.correlation_id, event_type_str, envelope)

        # Notify local listeners with the typed EventEnvelope
        if event_type_str in self.listeners:
            # Execute all async handlers concurrently in the background
            tasks = []
            for listener in list(self.listeners[event_type_str]):
                try:
                    tasks.append(listener(envelope))
                except Exception:
                    # Capture initialization exceptions before gathering
                    print("[MessageBus] Exception during listener invocation setup:", file=sys.stderr)
                    traceback.print_exc(file=sys.stderr)

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        print("[MessageBus Error] Subscriber exception caught during publish:", file=sys.stderr)
                        traceback.print_exception(type(r), r, r.__traceback__, file=sys.stderr)

        # Bubble up to parent without re-stamping or modification
        if self.parent:
            await self.parent._dispatch(envelope)

    async def request(
        self,
        request_event: Event[Any],
        response_type: str,
        timeout_sec: float
    ) -> EventEnvelope[Any]:
        """
        Asynchronous Request-Response pattern using typed Events.
        Requires the request_event to have an explicit correlation_id on entry.
        """
        if not isinstance(request_event, Event):
            raise TypeError("MessageBus.request strictly requires an Event instance.")
        if not isinstance(response_type, str) or not response_type:
            raise ValueError("response_type must be a non-empty string")

        correlation_id = request_event.correlation_id
        if not correlation_id:
            raise ValueError("MessageBus.request: request_event must have a valid correlation_id.")

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_futures[correlation_id] = (future, response_type)

        try:
            await self.publish(request_event)
            return await asyncio.wait_for(future, timeout=timeout_sec)
        finally:
            self._pending_futures.pop(correlation_id, None)
