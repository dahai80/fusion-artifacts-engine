import logging
import queue
import threading

logger = logging.getLogger(__name__)

EVENT_DROPPED = "__dropped__"
EVENT_CLOSED = "__closed__"
_DEFAULT_MAX_SUBSCRIBERS = 64


class EventBus:
    def __init__(self, maxsize: int = 256, max_subscribers: int = _DEFAULT_MAX_SUBSCRIBERS):
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self._maxsize = maxsize
        self._max_subscribers = max_subscribers
        self._closed = False

    def subscribe(self) -> queue.Queue:
        # C-7: subscriber cap, prevents unbounded memory growth
        q = queue.Queue(maxsize=self._maxsize)
        with self._lock:
            if self._closed:
                q.put_nowait({"event_type": EVENT_CLOSED})
                return q
            if len(self._subscribers) >= self._max_subscribers:
                logger.warning(
                    "EventBus subscribe rejected: %d >= max %d",
                    len(self._subscribers), self._max_subscribers,
                )
                q.put_nowait({"event_type": EVENT_DROPPED})
                return q
            self._subscribers.append(q)
        logger.info("EventBus subscriber added, total=%d", len(self._subscribers))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)
        logger.info("EventBus subscriber removed, total=%d", len(self._subscribers))

    def publish(self, event_type: str, data: dict) -> None:
        # P-5: snapshot the subscriber list inside the lock only; put after releasing the lock so a blocking put never holds the lock
        with self._lock:
            if self._closed:
                return
            snapshot = list(self._subscribers)
        dead = []
        for q in snapshot:
            try:
                q.put_nowait({"event_type": event_type, **data})
            except queue.Full:
                # A-3: before dropping a full queue, deliver the __dropped__ signal so the SSE handler closes the stream and the client reconnects.
                # P0-6: with the queue full, put_nowait(EVENT_DROPPED) would hit Full too — get_nowait first to free a slot,
                # otherwise the signal is silently lost and the SSE stream never closes.
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait({"event_type": EVENT_DROPPED})
                except queue.Full:
                    pass
                dead.append(q)
        if dead:
            with self._lock:
                for q in dead:
                    if q in self._subscribers:
                        self._subscribers.remove(q)
            logger.warning(
                "EventBus dropped %d full subscriber(s), remaining=%d",
                len(dead), len(self._subscribers),
            )

    def shutdown(self) -> None:
        # A-4: graceful shutdown — deliver the __closed__ signal to all subscribers; the SSE handler closes the stream on receipt
        with self._lock:
            if self._closed:
                return
            self._closed = True
            snapshot = list(self._subscribers)
            self._subscribers.clear()
        for q in snapshot:
            try:
                q.put_nowait({"event_type": EVENT_CLOSED})
            except queue.Full:
                pass
        logger.info("EventBus shutdown: notified %d subscriber(s)", len(snapshot))

    def is_closed(self) -> bool:
        # Ops-5: /readyz probe dependency — reports not ready once the EventBus is closed
        with self._lock:
            return self._closed
