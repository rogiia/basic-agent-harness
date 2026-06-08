class Scratchpad:
    """Read and write from a in-memory scratchpad"""

    def __init__(self):
        self._content = ""

    def read(self) -> str:
        if self._content == "":
            return "(empty)"
        return self._content

    def write(self, content: str) -> str:
        self._content = str(content).strip()
        return self._content


scratchpad = Scratchpad()


def read_scratchpad():
    """Read the contents of the scratchpad"""
    return scratchpad.read()


def write_scratchpad(content: str):
    """
    Write into the scratchpad. The previous content
    will be overwritten.
    """
    scratchpad.write(content)
    return "Successfully written content into scratchpad"
