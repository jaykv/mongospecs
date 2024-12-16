import typing as t

from blinker import signal


class SignalMixin:
    @classmethod
    def listen(cls, event: str, func: t.Callable[..., t.Any]) -> None:
        """Add a callback for a signal against the class"""
        signal(event).connect(func, sender=cls)

    @classmethod
    def stop_listening(cls, event: str, func: t.Callable[..., t.Any]) -> None:
        """Remove a callback for a signal against the class"""
        signal(event).disconnect(func, sender=cls)
