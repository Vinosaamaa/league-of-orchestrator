from .adapter import TmuxMultiplexerAdapter


def adapter() -> TmuxMultiplexerAdapter:
    return TmuxMultiplexerAdapter()


__all__ = ["TmuxMultiplexerAdapter", "adapter"]
