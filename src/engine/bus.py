import asyncio
import uuid
import sys
import traceback
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple, Union
from engine.types import EventEnvelope

# Type alias for asynchronous event listeners
AsyncListener = Callable[[EventEnvelope[Any]], Coroutine[Any, Any, None]]


class MessageBus:
    """
    Asynchronous MessageBus.
    Strictly type-safe: operates exclusively on strongly-typed EventEnvelope instances.
    """

    def __init__(self, name: str = "main", parent: Optional["MessageBus"] = None):
        self.name: str = name
        self.parent: Optional[MessageBus] = parent
        self.listeners: Dict[str, List[AsyncListener]] = {}
        # Futures strictly expect and return EventEnvelope objects
        self._pending_futures: Dict[str, Tuple[asyncio.Future[EventEnvelope[Any]], str]] = {}
        self._subscriptions: Dict[str, Tuple[str, AsyncListener]] = {}

    def derive(self, subagent_name: str) -> "MessageBus":
        """
        Derives a nested subagent message bus.
        Binds publish actions back to the parent using pure EventEnvelopes.
        """
        if not isinstance(subagent_name, str) or not subagent_name:
            raise ValueError("subagent_name must be a non-empty string")

        child_bus = MessageBus(name=f"{self.name}/{subagent_name}", parent=self)

        async def subagent_publish(envelope: EventEnvelope[Any]) -> None:
            if not isinstance(envelope, EventEnvelope):
                raise TypeError("subagent_publish strictly requires an EventEnvelope instance.")

            # Set sender cleanly to child_bus.name using pure explicit class reconstruction
            delegated_envelope = EventEnvelope(
                event_type=envelope.event_type,
                payload=envelope.payload,
                sender=child_bus.name,
                correlation_id=envelope.correlation_id,
                timestamp=envelope.timestamp
            )
            await self.publish(delegated_envelope)

        child_bus.publish = subagent_publish
        child_bus.subscribe = self.subscribe
        child_bus.unsubscribe = self.unsubscribe
        child_bus.request = self.request
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

    async def publish(self, envelope: EventEnvelope[Any]) -> None:
        """
        Dispatches a typed EventEnvelope concurrently to all listeners.
        """
        if not isinstance(envelope, EventEnvelope):
            raise TypeError("MessageBus.publish strictly requires an EventEnvelope instance.")

        event_type_str = envelope.event_type.value
        correlation_id = envelope.correlation_id

        # Match outstanding request futures purely using typed fields
        if correlation_id and correlation_id in self._pending_futures:
            future, expected_response_type = self._pending_futures[correlation_id]
            if event_type_str == expected_response_type:
                if not future.done():
                    # Request strictly returns the strongly-typed EventEnvelope!
                    future.set_result(envelope)

        # Notify listeners with the typed EventEnvelope
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

    async def request(
        self,
        request_envelope: EventEnvelope[Any],
        response_type: str,
        timeout_sec: float
    ) -> EventEnvelope[Any]:
        """
        Asynchronous Request-Response pattern using typed envelopes.
        Requires the request_envelope to have an explicit correlation_id on entry.
        """
        if not isinstance(request_envelope, EventEnvelope):
            raise TypeError("MessageBus.request strictly requires an EventEnvelope instance.")
        if not isinstance(response_type, str) or not response_type:
            raise ValueError("response_type must be a non-empty string")

        correlation_id = request_envelope.correlation_id
        if not correlation_id:
            raise ValueError("MessageBus.request: request_envelope must have a valid correlation_id.")

        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending_futures[correlation_id] = (future, response_type)

        try:
            await self.publish(request_envelope)
            return await asyncio.wait_for(future, timeout=timeout_sec)
        finally:
            self._pending_futures.pop(correlation_id, None)
