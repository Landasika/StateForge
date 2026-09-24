class A:
    def get(self, v):
        return v + 1

class B:
    def get(self, v):
        return v * 2

def run(x, which):
    obj = A() if which else B()
    return obj.get(x)
