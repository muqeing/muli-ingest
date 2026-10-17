"""Two bounded coordinators, one fair media-I/O turn at a time.

Only one regular batch and one <=128-file batch may load their working state.
Queued callables retain IDs, not file lists. Copy priority and all existing
source/target checks remain in Engine; this module never reads media or resumes
a failed operation.
"""
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

from .safeio import SafetyError


class MetadataScheduler:
    def __init__(self, is_short):
        self.is_short = is_short
        self.regular = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metadata-regular")
        self.short = ThreadPoolExecutor(max_workers=1, thread_name_prefix="metadata-short")
        self.condition = threading.Condition()
        self.local = threading.local()
        self.busy = False
        self.waiting = {False: 0, True: 0}
        self.short_streak = 0
        self.closed = False
        self.owner = None

    def submit(self, function):
        uid = getattr(function, "batch_uid", None)
        small = bool(uid is not None and self.is_short(uid))
        def run():
            self.local.small = small
            self.local.in_worker = True
            return function()
        return (self.short if small else self.regular).submit(run)

    def run_and_wait(self, uid, function):
        if getattr(self.local, "in_worker", False):
            raise SafetyError("metadata_reentrant_wait")
        function.batch_uid = uid
        return self.submit(function).result()

    def acquire(self, small, cancel):
        with self.condition:
            self.waiting[small] += 1
            try:
                while True:
                    if cancel.is_set() or self.closed:
                        raise SafetyError("operator_stop")
                    # Give at most four short turns before a waiting regular
                    # batch. With no competitor, either lane runs immediately.
                    preferred = (self.waiting[True] > 0 and
                                 (self.short_streak < 4 or not self.waiting[False]))
                    if not self.busy and (small == preferred or not self.waiting[not small]):
                        self.busy = True
                        self.owner = threading.get_ident()
                        self.local.held = True
                        self.local.checkpoints = 0
                        self.short_streak = min(self.short_streak + 1, 4) if small else 0
                        break
                    self.condition.wait(timeout=0.25)
            finally:
                self.waiting[small] -= 1

    def release(self):
        with self.condition:
            if self.owner != threading.get_ident() or not getattr(self.local, "held", False):
                raise RuntimeError("metadata turn ownership changed")
            self.busy = False
            self.owner = None
            self.local.held = False
            self.condition.notify_all()

    @contextmanager
    def turn(self, *, cancel):
        small = getattr(self.local, "small", False)
        self.acquire(small, cancel)
        try:
            yield
        finally:
            if getattr(self.local, "held", False):
                self.release()

    def checkpoint(self, *, cancel):
        """Yield only between existing readers, never during a tool invocation."""
        small = getattr(self.local, "small", False)
        with self.condition:
            if self.owner != threading.get_ident():
                return
            first = self.local.checkpoints == 0
            self.local.checkpoints += 1
            needed = not first and self.waiting[not small] > 0
        if needed:
            self.release()
            # A cancellation here must not release another thread's turn.
            self.acquire(small, cancel)

    def shutdown(self, *, wait=True, cancel_futures=False):
        with self.condition:
            self.closed = True
            self.condition.notify_all()
        self.regular.shutdown(wait=wait, cancel_futures=cancel_futures)
        self.short.shutdown(wait=wait, cancel_futures=cancel_futures)
