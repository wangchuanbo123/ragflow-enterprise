"""Shared model capacity with reserved foreground headroom and reentrant calls."""
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from functools import wraps
import inspect
import threading
import time

_depth = ContextVar("model_budget_depth", default=0)
_local_lock = threading.Lock()
_local_slots = {}


class _LocalLease:
    def __init__(self, name, limit):
        with _local_lock:
            self.slot = _local_slots.setdefault((name, limit), threading.BoundedSemaphore(limit))
    def acquire(self, timeout):
        return self.slot.acquire(timeout=max(0, timeout))
    def assert_held(self):
        return None
    def close(self):
        self.slot.release()


@contextmanager
def model_slot(*, background=None, cancelled=None, timeout=None):
    from app.core import config
    from rag.errors import RAGBusyError
    from app.services.observability import observe
    if _depth.get() or not getattr(config, "MODEL_BUDGET_ENABLED", config.APP_PROFILE == "server"):
        yield None
        return
    background = config.APP_ROLE in {"worker", "scheduler"} if background is None else background
    total = max(1, int(getattr(config, "MODEL_MAX_CONCURRENCY", 8)))
    background_max = max(1, min(total, int(getattr(config, "MODEL_BACKGROUND_CONCURRENCY", 2))))
    budget = max(0, float(getattr(config, "MODEL_QUEUE_TIMEOUT_SECONDS", 30) if timeout is None else timeout))
    deadline = time.monotonic() + budget
    started, leases = time.monotonic(), []
    token = None
    try:
        for name, limit in ([('model-background', background_max)] if background else []) + [('model-total', total)]:
            if config.CACHE_PROVIDER == "redis":
                from app.adapters.redis import RedisSemaphoreLease
                lease = RedisSemaphoreLease(name, limit, 60)
            elif config.APP_PROFILE in {"local", "test"}:
                lease = _LocalLease(name, limit)
            else:
                from app.adapters.redis import CoordinationUnavailableError
                raise CoordinationUnavailableError("共享模型配额需要Redis")
            while True:
                if cancelled and cancelled():
                    from app.agents.contracts import AgentCancelledError
                    raise AgentCancelledError("等待模型配额时任务已取消")
                if lease.acquire(min(.2, max(0, deadline - time.monotonic()))):
                    leases.append(lease)
                    break
                if time.monotonic() >= deadline:
                    observe("model_queue_rejected")
                    raise RAGBusyError("模型服务当前繁忙，请稍后重试")
            for held in leases:
                held.assert_held()
        observe("model_queue_wait_seconds", time.monotonic() - started)
        token = _depth.set(_depth.get() + 1)
        yield leases[-1]
        for held in leases:
            held.assert_held()
    finally:
        if token is not None:
            _depth.reset(token)
        failure = None
        for held in reversed(leases):
            try:
                held.close()
            except Exception as exc:
                failure = failure or exc
        if failure is not None:
            raise failure


def measured_model_call(function):
    """Hold a lease for the entire stream, including generator close/error paths."""
    if inspect.isgeneratorfunction(function):
        @wraps(function)
        def stream(*args, **kwargs):
            from app.services.observability import observe
            # StreamingResponse may resume successive next() calls in separate
            # copied thread contexts. Keep lease tokens in one private Context.
            context = copy_context()
            def produce():
                started, first = time.monotonic(), True
                observe("model_calls")
                try:
                    with model_slot() as lease:
                        for value in function(*args, **kwargs):
                            if first:
                                observe("model_first_token_seconds", time.monotonic() - started)
                                first = False
                            if lease:
                                lease.assert_held()
                            yield value
                except GeneratorExit:
                    observe("model_stream_cancelled")
                    raise
                except Exception:
                    observe("model_errors")
                    raise
                finally:
                    observe("model_stream_seconds", time.monotonic() - started)
            iterator = produce()
            try:
                while True:
                    try:
                        value = context.run(next, iterator)
                    except StopIteration:
                        return
                    yield value
            finally:
                context.run(iterator.close)
        return stream
    @wraps(function)
    def generate(*args, **kwargs):
        from app.services.observability import observe
        started = time.monotonic()
        observe("model_calls")
        try:
            with model_slot():
                return function(*args, **kwargs)
        except Exception:
            observe("model_errors")
            raise
        finally:
            observe("model_generate_seconds", time.monotonic() - started)
    return generate


class BudgetedAgentBackend:
    def __init__(self, backend):
        self.backend = backend
    def __getattr__(self, name):
        return getattr(self.backend, name)
    def run(self, request, *, cancelled=None, on_event=None):
        from app.services.observability import observe
        started = time.monotonic()
        observe("agent_model_calls")
        try:
            timeout = None
            due = request.context.get("deadline_at")
            if due:
                from datetime import datetime, timezone
                timeout = max(0, (datetime.fromisoformat(due.replace("Z", "+00:00")) - datetime.now(timezone.utc)).total_seconds())
            with model_slot(cancelled=cancelled, timeout=timeout):
                result = self.backend.run(request, cancelled=cancelled, on_event=on_event)
                record_usage(result.usage, prefix="agent_model")
                return result
        except Exception:
            observe("agent_model_errors")
            raise
        finally:
            observe("agent_model_seconds", time.monotonic() - started)


def budgeted_backend(backend):
    from app.core import config
    if getattr(config, "MODEL_BUDGET_ENABLED", config.APP_PROFILE == "server"):
        return BudgetedAgentBackend(backend)
    return backend


def record_usage(usage, *, prefix="model"):
    """Record only provider-reported usage; unknown prices never become zero."""
    from app.services.observability import observe
    if not isinstance(usage, dict):
        return False
    observed = False
    for label, aliases in {"input_tokens": ("input_tokens", "prompt_tokens"),
                           "output_tokens": ("output_tokens", "completion_tokens")}.items():
        value = next((usage[key] for key in aliases if key in usage), None)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value < 10**12:
            observe(prefix + "_" + label, value)
            observed = True
    if observed:
        observe(prefix + "_usage_reported")
    cost = usage.get("cost")
    if usage.get("currency") == "USD" and isinstance(cost, (int, float)) and not isinstance(cost, bool) and 0 < cost < 10**9:
        observe(prefix + "_reported_cost_usd", cost)
    else:
        observe(prefix + "_cost_unknown")
    return observed
