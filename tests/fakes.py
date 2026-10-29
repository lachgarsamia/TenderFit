import re

from tenderfit.llm import JsonResult, ToolTurn, Usage


class ScriptedLLM:
    """Test double: `script(role, user, schema)` returns the model's JSON; usage is fixed."""

    def __init__(self, script, tool_script=None):
        self.script, self.tool_script, self.users = script, tool_script, []

    def json(self, role, system, user, schema):
        self.users.append(user)
        return JsonResult(self.script(role, user, schema), Usage("claude-opus-5-5", 1000, 200))

    def tool_turn(self, role, system, messages, tools):
        return self.tool_script(messages)


def passage_id(user: str, contains: str) -> int:
    for m in re.finditer(r'<tender_passage id="(\d+)"[^>]*>\n(.*?)\n</tender_passage>', user, re.S):
        if contains in m.group(2):
            return int(m.group(1))
    raise AssertionError(f"no passage containing {contains!r}")
