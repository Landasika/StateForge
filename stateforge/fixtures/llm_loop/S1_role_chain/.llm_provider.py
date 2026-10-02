import json

def _p(kind, key, val):
    return json.dumps({"status": "PROPOSED", "source_kind": kind,
                       "source_key": key, "operation": "REPLACE",
                       "proposed_value": val,
                       "rationale": "mock", "confidence": 0.9})

class Provider:
    def __init__(self):
        self.responses = [_p("QUERY", "username", "administrator"),
                          _p("QUERY", "action", "delete"),
                          _p("QUERY", "token", "adm_secret")]
        self.calls = 0
    def complete(self, system, user):
        if self.calls < len(self.responses):
            r = self.responses[self.calls]
        else:
            r = json.dumps({"status": "UNSUPPORTED"})
        self.calls += 1
        return r

provider = Provider()
