"""A scripted stand-in for anthropic.Anthropic, so tests never call the API."""
from types import SimpleNamespace


def usage(input_tokens=100, output_tokens=50, cache_read=0, cache_write=0):
    return SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_write,
    )


def text_response(text, model="claude-sonnet-5", usage_=None, rid="msg_text", stop_reason="end_turn"):
    return SimpleNamespace(
        id=rid,
        model=model,
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        usage=usage_ or usage(),
    )


def tool_response(calls, model="claude-sonnet-5", usage_=None, rid="msg_tool"):
    """calls: list of (tool_use_id, name, input_dict)."""
    return SimpleNamespace(
        id=rid,
        model=model,
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id=i, name=n, input=a) for i, n, a in calls],
        usage=usage_ or usage(),
    )


class FakeMessages:
    def __init__(self, script):
        self._script = list(script)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeClient:
    def __init__(self, script):
        self.messages = FakeMessages(script)
