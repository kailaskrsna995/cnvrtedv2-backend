"""
Tracked Anthropic clients — drop-in replacements for anthropic.Anthropic /
AsyncAnthropic that log token usage on every `.messages.create(...)` call.

Modules just change their import:
    from app.llm import Anthropic          # was: from anthropic import Anthropic
    from app.llm import AsyncAnthropic     # was: from anthropic import AsyncAnthropic
Everything else (instantiation, .messages.create) stays identical.
"""

from anthropic import Anthropic as _Anthropic, AsyncAnthropic as _AsyncAnthropic
from app import usage


def _track(model, resp):
    try:
        usage.log_anthropic(model, getattr(resp, "usage", None))
    except Exception:
        pass
    return resp


class _SyncMessages:
    def __init__(self, inner):
        self._inner = inner

    def create(self, **kw):
        return _track(kw.get("model"), self._inner.create(**kw))

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _AsyncMessages:
    def __init__(self, inner):
        self._inner = inner

    async def create(self, **kw):
        return _track(kw.get("model"), await self._inner.create(**kw))

    def __getattr__(self, name):
        return getattr(self._inner, name)


class Anthropic:
    def __init__(self, *a, **kw):
        self._c = _Anthropic(*a, **kw)
        self.messages = _SyncMessages(self._c.messages)

    def __getattr__(self, name):
        return getattr(self._c, name)


class AsyncAnthropic:
    def __init__(self, *a, **kw):
        self._c = _AsyncAnthropic(*a, **kw)
        self.messages = _AsyncMessages(self._c.messages)

    def __getattr__(self, name):
        return getattr(self._c, name)
