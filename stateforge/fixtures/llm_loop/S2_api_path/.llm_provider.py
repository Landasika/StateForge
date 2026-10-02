"""S2 的 LLM provider(mock,预设正确答案)。"""
import json

def _p(kind, key, val):
    return json.dumps({"status": "PROPOSED", "source_kind": kind,
                       "source_key": key, "operation": "REPLACE",
                       "proposed_value": val,
                       "rationale": "mock", "confidence": 0.9})

class Provider:
    def __init__(self):
        self.responses = [_p("QUERY", "version", "v1"),
                          _p("QUERY", "path", "/api/v1/admin/config"),
                          _p("QUERY", "method", "GET")]
        self.calls = 0
    def complete(self, system, user):
        if self.calls < len(self.responses):
            r = self.responses[self.calls]
        else:
            r = json.dumps({"status": "UNSUPPORTED"})
        self.calls += 1
        return r

provider = Provider()
