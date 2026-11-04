"""tracer 的增量能力:按请求排空 + 线程局部请求号,且不破坏旧口径。

两条旧语义必须同时成立:
* 裸 ``with session:`` 下所有事件归属同一请求(rid 全为 0);
* ``request()`` 每次进入分配新 rid,嵌套结束后回到外层请求。
"""

from __future__ import annotations

import threading

from stateforge.runtime.tracer import TraceSession


def body(tag: int) -> None:
    x = tag + 1
    del x


def test_bare_session_keeps_single_request() -> None:
    session = TraceSession(root=__file__ and ".")
    with session:
        body(1)
        body(2)
    assert session.events
    assert {event.request_id for event in session.events} == {0}


def test_each_request_gets_its_own_id() -> None:
    session = TraceSession()
    with session:
        with session.request() as first:
            body(1)
        with session.request() as second:
            body(2)
    assert first != second
    assert {event.request_id for event in session.events} == {first, second}


def test_drain_isolates_one_request() -> None:
    session = TraceSession()
    with session.trace_execution():
        with session.request() as first:
            body(1)
        with session.request() as second:
            body(2)
    first_events = session.drain_request_events(first)
    assert first_events
    assert {event.request_id for event in first_events} == {first}
    # 只排空指定请求:另一请求的事件仍在
    assert {event.request_id for event in session.events} == {second}
    assert session.drain_request_events(first) == []
    assert session.drain_request_events(second)


def test_repeated_drain_is_empty() -> None:
    session = TraceSession()
    with session.trace_execution():
        with session.request() as rid:
            body(1)
    assert session.drain_request_events(rid)
    assert session.drain_request_events(rid) == []


def test_nested_requests_restore_outer_id() -> None:
    session = TraceSession()
    with session.trace_execution():
        with session.request() as outer:
            body(1)
            with session.request() as inner:
                body(2)
            body(3)
    by_request = {rid: [] for rid in (outer, inner)}
    for event in session.events:
        by_request.setdefault(event.request_id, []).append(event)
    assert outer != inner
    assert by_request[outer], "outer request should capture frames"
    assert by_request[inner], "inner request should capture frames"


def test_threads_do_not_share_request_id() -> None:
    """并发请求各归各号:一个线程的排空不得拿走另一个线程的事件。"""
    session = TraceSession()
    seen: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def worker(name: str) -> None:
        with session.trace_execution():
            with session.request() as rid:
                seen[name] = rid
                barrier.wait(timeout=5)
                body(len(name))

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert seen["a"] != seen["b"]
    for name in ("a", "b"):
        events = session.drain_request_events(seen[name])
        assert events, f"no events captured for thread {name}"
        assert {event.request_id for event in events} == {seen[name]}


def test_trace_execution_does_not_allocate_request() -> None:
    """trace_execution 只装卸 settrace,不占号——否则每次 campaign 都漏号。"""
    session = TraceSession()
    with session.trace_execution():
        with session.request() as first:
            body(1)
    with session.trace_execution():
        with session.request() as second:
            body(2)
    assert second == first + 1
