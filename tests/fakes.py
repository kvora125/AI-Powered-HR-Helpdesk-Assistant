"""A scripted stand-in for LLMClient so the pipeline can be tested offline and deterministically."""
import json

from hr_helpdesk.llm import LLMResult, STATUS_SUCCESS


class FakeLLM:
    model = "fake-model"

    def __init__(self, route=None, tool_calls=None, answer="Here is your answer.", classify_outputs=None):
        self.route = route or {"intent": "general_hr", "policy_topics": [], "record_type": "none",
                               "refers_to_other_employee": False, "reason": "test"}
        self.tool_calls = list(tool_calls or [])     # list of lists of {"name","arguments"} per round
        self.answer = answer
        self.classify_outputs = list(classify_outputs or [])
        self.calls = []                              # (purpose, messages, tools)

    def _res(self, content="", tool_calls=None, finish="stop", purpose=""):
        return LLMResult(status=STATUS_SUCCESS if finish in ("stop", "tool_calls") else "incomplete",
                         content=content, tool_calls=tool_calls or [], finish_reason=finish,
                         input_tokens=100, output_tokens=20, model=self.model, purpose=purpose, latency_ms=1)

    def complete(self, messages, tools=None, tool_choice=None, json_mode=False, max_tokens=700,
                 temperature=0.1, purpose=""):
        self.calls.append((purpose, messages, tools))
        if purpose.startswith("route"):
            return self._res(json.dumps(self.route), purpose=purpose)
        if purpose.startswith("classify"):
            content, finish = self.classify_outputs.pop(0) if self.classify_outputs else ("{}", "stop")
            return self._res(content, finish=finish, purpose=purpose)
        if purpose.startswith("tool_round") and self.tool_calls:
            calls = [{"id": f"c{i}", **c} for i, c in enumerate(self.tool_calls.pop(0))]
            return self._res("", calls, "tool_calls", purpose)
        return self._res(self.answer, purpose=purpose)

    def stream(self, messages, on_delta, max_tokens=700, temperature=0.2, purpose="", tools=None, should_cancel=None):
        self.calls.append((purpose, messages, tools))
        for word in self.answer.split(" "):
            on_delta(word + " ")
        return self._res(self.answer, purpose=purpose)
