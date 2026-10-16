"""Scripted protocol fixture, NOT a natural-language parser or real LLM."""

import copy
import json


def call(name, **arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_" + name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
            }
        ],
    }


class ScriptedModel:
    def __init__(self, *steps):
        self.steps, self.messages = list(steps), []

    async def complete(self, messages, tools):
        self.messages.append(copy.deepcopy(messages))
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step):
            return step(messages)
        return step
