from typing import Protocol


class RunProgressSink(Protocol):
    def emit(self, text: str) -> None:
        ...


class NullRunProgressSink:
    def emit(self, text: str) -> None:
        pass
