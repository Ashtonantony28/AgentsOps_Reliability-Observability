from app.policy.ast import Policy


class PolicyParseError(ValueError):
    def __init__(self, line: int, col: int, message: str):
        super().__init__(f"{line}:{col}: {message}")
        self.line = line
        self.col = col
        self.message = message


def parse(src: str) -> list[Policy]:
    raise NotImplementedError
