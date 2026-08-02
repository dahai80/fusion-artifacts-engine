import queue
import logging
import threading

logger = logging.getLogger(__name__)


class EventBus:
    def __init__(self, maxsize: int = 256):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._maxsize = maxsize

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            self._subscribers.append(q)
        logger.info("EventBus subscriber added, total=%d", len(self._subscribers))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
        logger.info("EventBus subscriber removed, total=%d", len(self._subscribers))

    def publish(self, event_type: str, data: dict) -> None:
        with self._lock:
            dead = []
            for q in self._subscribers:
                try:
                    q.put_nowait({"event_type": event_type, **data})
                except queue.Full:
                    dead.append(q)
            for q in dead:
                self._subscribers.remove(q)
                logger.warning(
                    "EventBus dropped full subscriber, remaining=%d",
                    len(self._subscribers),
                )


event_bus = EventBus()
