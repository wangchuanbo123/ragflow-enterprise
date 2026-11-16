"""Bounded in-process caches; every use requires a fresh authorized DB version.

Only immutable complete snapshots are shared. Plans are deep-copied because
validation resolves user references in place. No credential or raw SQL is cached.
"""

from collections import OrderedDict
from copy import deepcopy
import threading
import time
import sys


class VersionedCache:
    def __init__(self, max_entries=8, max_weight=50000, ttl_seconds=120):
        self.max_entries, self.max_weight, self.ttl_seconds = max_entries, max_weight, ttl_seconds
        self._values, self._weight, self._lock = OrderedDict(), 0, threading.RLock()
        self.hits = self.misses = 0

    def get(self, key, *, copy=False):
        with self._lock:
            value = self._values.get(key)
            if value is None or value[0] < time.monotonic():
                if value is not None:
                    self._weight -= self._values.pop(key)[1]
                self.misses += 1
                return None
            self._values.move_to_end(key)
            self.hits += 1
            result = value[2]
        return deepcopy(result) if copy else result

    def put(self, key, value, *, weight=1, copy=False):
        if weight > self.max_weight or weight < 0:
            return
        value = deepcopy(value) if copy else value
        with self._lock:
            previous = self._values.pop(key, None)
            if previous:
                self._weight -= previous[1]
            self._values[key] = (time.monotonic() + self.ttl_seconds, weight, value)
            self._weight += weight
            while len(self._values) > self.max_entries or self._weight > self.max_weight:
                self._weight -= self._values.popitem(last=False)[1][1]

    def clear(self):
        with self._lock:
            self._values.clear()
            self._weight = 0


def estimate_bytes(value, maximum):
    """Count shared Python objects once, stop once the admission budget fails."""
    stack, seen, size = [value], set(), 0
    while stack:
        item = stack.pop()
        if id(item) in seen:
            continue
        seen.add(id(item))
        size += sys.getsizeof(item)
        if size > maximum:
            return size
        if isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, (list, tuple, set, frozenset)):
            stack.extend(item)
        elif hasattr(item, "__dict__"):
            stack.append(vars(item))
    return size


snapshots = VersionedCache(max_entries=4, max_weight=32 * 1024 * 1024)
plans = VersionedCache(max_entries=128, max_weight=8 * 1024 * 1024, ttl_seconds=300)
