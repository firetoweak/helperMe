from typing import Protocol


class TurnProgressSink(Protocol):
    def emit(self, text: str) -> None:
        ...


class NullTurnProgressSink:
    def emit(self, text: str) -> None:
        pass
