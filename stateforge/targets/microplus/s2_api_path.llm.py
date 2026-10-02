import json
def _p(k, v):
    return json.dumps({"status":"PROPOSED","source_kind":"QUERY",
                       "source_key":k,"operation":"REPLACE",
                       "proposed_value":v,"rationale":"mock","confidence":0.9})
class P:
    def __init__(self):
        self.rs = [_p("path","/"), _p("version","v1"), _p("method","GET")]
        self.calls = 0
    def complete(self, s, u):
        r = self.rs[self.calls] if self.calls < len(self.rs) else json.dumps({"status":"UNSUPPORTED"})
        self.calls += 1
        return r
provider = P()
