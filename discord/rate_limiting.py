"""
The MIT License (MIT)

Copyright (c) 2021-present Pycord Development

Permission is hereby granted, free of charge, to any person obtaining a
copy of this software and associated documentation files (the "Software"),
to deal in the Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, sublicense,
and/or sell copies of the Software, and to permit persons to whom the
Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import asyncio
import gc
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Literal, cast

from .errors import DiscordException


class RateLimitException(DiscordException):
    ...


class GlobalRateLimit:
    """A rate limit class specifically for the global bucket.

    Parameters
    ----------
    concurrency: :class:`int`
        The concurrency to reset for every `per` reset.
    per: :class:`int` | :class:`float`
        Time to wait until resetting `concurrency.`

    Attributes
    ----------
    current: :class:`int`
        The current concurrency.
    pending_reset: :class:`bool`
        The class is pending a reset of `concurrency`.
    reset_at :class:`int` | :class:`float` | `None`
        When this class will next reset.
    """

    def __init__(self, concurrency: int, per: float | int) -> None:
        self.concurrency: int = concurrency
        self.per: float | int = per

        self.current: int = self.concurrency
        self._processing: list[asyncio.Future] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.pending_reset: bool = False
        self.reset_at: int | float | None = None

    async def __aenter__(self) -> GlobalRateLimit:
        if not self.loop:
            self.loop = asyncio.get_running_loop()

        while self.current == 0:
            future = self.loop.create_future()
            self._processing.append(future)
            await future

        self.current -= 1

        if not self.pending_reset:
            self.pending_reset = True
            self.loop.call_later(self.per, self.reset)

        return self

    async def __aexit__(self, *_) -> None:
        ...

    def reset(self) -> None:
        current_time = time.time()
        self.reset_at = current_time + self.per
        self.current = self.concurrency

        for _ in range(self.concurrency):
            try:
                self._processing.pop().set_result(None)
            except IndexError:
                break

        if len(self._processing):
            self.pending_reset = True
            self.loop.call_later(self.per, self.reset)
        else:
            self.pending_reset = False


class PriorityFuture(asyncio.Future):
    """A future with priority features added to it."""

    def __init__(
        self, *, priority: int = 0, loop: asyncio.AbstractEventLoop | None = None
    ) -> None:
        super().__init__(loop=loop)
        self.priority = priority

    def __gt__(self, other: PriorityFuture) -> bool:
        return other.priority < self.priority


class Bucket:
    """Represents a bucket in the Discord API.

    Attributes
    ----------
    metadata_unknown: :class:`bool`
        Whether the bucket's metadata is known.
    remaining: Union[:class:`int`, None]
        The remaining number of requests available on this bucket.
    limit: Union[:class:`int`, None]
        The maximum limit of requests per reset.
    """

    def __init__(self) -> None:
        self._pending: asyncio.PriorityQueue[
            PriorityFuture[None]
        ] = asyncio.PriorityQueue()
        self._reserved: list[PriorityFuture] = []
        self._reset_after_set: bool = False
        self.metadata_unknown: bool = False

        self.remaining: int | None = None
        self.limit: int | None = None
        self._fetch_metadata = asyncio.Event()
        self._fetch_metadata.set()

    @asynccontextmanager
    async def reserve(self, priority: int = 0) -> AsyncIterator[None]:
        if self.metadata_unknown:
            yield
            return

        if self.remaining is None and self.limit is not None:
            self.remaining = self.limit

        if self.remaining is not None:
            prediction = self.remaining - len(self._reserved)

            fut = PriorityFuture(priority=priority)

            if prediction <= 0:
                self._pending.put_nowait(fut)
                await fut

            self._reserved.append(fut)

            try:
                yield
            except:
                if self._pending.qsize() >= 1:
                    self.release(1)

                raise
            finally:
                self._reserved.remove(fut)

            return

        if self._fetch_metadata.is_set():
            self._fetch_metadata.clear()

            fut = PriorityFuture()

            self._reserved.append(fut)

            try:
                yield
            except:
                if self._pending.qsize() >= 1:
                    self.release(1)

                raise
            else:
                if self.remaining is not None:
                    self.release(self.remaining)
                elif self.metadata_unknown:
                    self.release()
                else:
                    if self._pending.qsize() >= 1:
                        self.release(1)
            finally:
                self._reserved.remove(fut)
                self._fetch_metadata.set()

            return

        await self._fetch_metadata.wait()

        async with self.reserve(priority=priority):
            yield

    def release(self, count: int | None = None) -> None:
        """Release *count* amount of requests.

        .. warning:: This should not be used directly.

        Parameters
        ----------
        count: :class:`int`
            The number of requests to release.
        """

        if count is not None:
            num = min(count, self._pending.qsize())
        else:
            num = self._pending.qsize()

        for _ in range(num):
            fut = self._pending.get_nowait()

            self._pending.task_done()

            fut.set_result(None)

    @property
    def garbage(self) -> bool:
        """Whether this bucket should be collected by the garbage collector."""

        # this bucket has futures reserved, do not collect yet
        if self._reserved:
            return False

        # this bucket has no limit, it is garbage
        if self.metadata_unknown:
            return True

        # this bucket has yet to expire
        if self.remaining is not None:
            return False

        return False

    async def stop(self) -> None:
        """Cancel all reserved futures from use."""

        for fut in self._reserved:
            fut.set_exception(asyncio.CancelledError)

    def set_metadata(
        self,
        remaining: int | None = None,
        reset_after: int | None = None,
    ) -> None:
        """Set the metadata for this Bucket.
        If both `remaining` and `reset_after` are set to `None`
        `metadata_unknown` is set to true, a garbage state is initialized,
        and all pending requests are released.

        Parameters
        ----------
        remaining: :class:`int` | `None`
            The remaining number of requests which can be made
            to this bucket.
        reset_after: :class:`int` | `None`
            When this bucket will next reset.
        """

        if remaining is None and reset_after is None:
            self.metadata_unknown = True
            self.release()
        else:
            self.remaining = remaining

            if self._reset_after_set:
                return

            self._reset_after_set = True

            loop = asyncio.get_running_loop()
            loop.call_later(cast(float, reset_after), self._reset)

    def _reset(self) -> None:
        self._reset_after_set = False
        self.remaining = None

        self.release(self.limit)


class BucketStorage:
    """A customizable, optionally replacable storage medium for buckets.

    Parameters
    ----------
    concurrency: :class:`int`
        The concurrency to reset for every `per` reset.
    per: :class:`int` | :class:`float`
        Time to wait until resetting `concurrency.`
    """

    def __init__(self, per: int = 1, concurrency: int = 50) -> None:
        self._buckets: dict[str, Bucket] = {}
        self.global_concurrency = GlobalRateLimit(concurrency, per)
        self.webhook_global_concurrency = GlobalRateLimit(30, 60)

        gc.callbacks.append(self._collect_buckets)

        # temporary buckets, different from normal "permanent" buckets
        # which cannot be tracked via remaining or reset_after in headers
        self._temp_buckets: dict[str, DynamicBucket] = {}

    def _collect_buckets(
        self, phase: Literal["start", "stop"], info: dict[str, int]
    ) -> None:
        del info

        if phase == "stop":
            return

        for id, bucket in self._buckets.copy().items():
            if bucket.garbage:
                del self._buckets[id]

    async def append(self, id: str, bucket: Bucket) -> None:
        """Append a permanent bucket.

        Parameters
        ----------
        id: :class:`str`
            This bucket's identifier.
        bucket: :class:`.Bucket`
            The bucket to append.
        """

        self._buckets[id] = bucket

    async def get(self, id: str) -> Bucket | None:
        """Get a permanent bucket.

        Parameters
        ----------
        id: :class:`str`
            This bucket's identifier.

        Returns
        -------
        :class:`.Bucket` or `None`
        """

        return self._buckets.get(id)

    async def get_or_create(self, id: str) -> Bucket:
        """Get or create a permanent bucket.

        Parameters
        ----------
        id: :class:`str`
            This bucket's identifier.

        Returns
        -------
        :class:`.Bucket`
        """

        buc = await self.get(id)

        if buc:
            return buc
        else:
            buc = Bucket()
            await self.append(id, buc)
            return buc

    async def temp_bucket(self, id: str) -> DynamicBucket | None:
        """Fetch a temporary bucket.

        Parameters
        ----------
        id: :class:`str`
            This bucket's identifier.

        Returns
        -------
        :class:`.Bucket` or `None`
        """

        return self._temp_buckets.get(id)

    async def push_temp_bucket(self, id: str, bucket: Bucket) -> None:
        """Push a temporary bucket to storage.

        Parameters
        ----------
        id: :class:`str`
            This bucket's identifier.
        """

        self._temp_buckets[id] = bucket

    async def pop_temp_bucket(self, id: str) -> None:
        """Pop a temporary bucket which *may* be in storage.

        Parameters
        ----------
        id: :class:`str`
            This bucket's identifier.
        """

        self._temp_buckets.pop(id, None)


class DynamicBucket:
    """A dynamic bucket for on-the-fly rate limits. Should not be used inside a bot directly!"""

    def __init__(self) -> None:
        self.is_global: bool | None = None
        self._request_queue: asyncio.Queue[asyncio.Event] | None = None
        self.rate_limited: bool = True

    async def executed(
        self, reset_after: int | float, limit: int, is_global: bool
    ) -> None:
        self.is_global = is_global
        self._reset_after = reset_after
        self._request_queue = asyncio.Queue()

        await asyncio.sleep(reset_after)

        self.is_global = False

        # NOTE: This could break if someone did a second rate limit somehow
        requests_passed: int = 0
        for _ in range(self._request_queue.qsize() - 1):
            if requests_passed == limit:
                requests_passed = 0
                if not is_global:
                    await asyncio.sleep(reset_after)
                else:
                    await asyncio.sleep(5)

            requests_passed += 1
            e = await self._request_queue.get()
            e.set()
        self.rate_limited = False

    async def wait(self) -> None:
        if not self.rate_limited:
            return

        event = asyncio.Event()

        if self._request_queue:
            self._request_queue.put_nowait(event)
        else:
            raise RateLimitException(
                "Request queue does not exist, rate limit may have been solved."
            )
        await event.wait()