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
        # C-7: 订阅者上限，防无界增长耗内存
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
        # P-5: 锁内只快照订阅者列表，释放锁后再 put，避免 put 阻塞期间持锁
        with self._lock:
            if self._closed:
                return
            snapshot = list(self._subscribers)
        dead = []
        for q in snapshot:
            try:
                q.put_nowait({"event_type": event_type, **data})
            except queue.Full:
                # A-3: 队列满删除前先投递 __dropped__ 信号，让 SSE handler 关流促客户端重连。
                # P0-6: 队列已满时 put_nowait(EVENT_DROPPED) 也必 Full——先 get_nowait 腾槽再投，
                # 否则信号静默丢失，SSE 永不关流。
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
        # A-4: 优雅停机——向所有订阅者投递 __closed__ 信号，SSE handler 收到即关流
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
        # 运维5: /readyz 探针依赖——EventBus 关闭后判定 not ready
        with self._lock:
            return self._closed
