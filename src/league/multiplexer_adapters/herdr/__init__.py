from .adapter import HerdrMultiplexerAdapter


def adapter(*, runner=None, binary=None) -> HerdrMultiplexerAdapter:
    return HerdrMultiplexerAdapter(runner, binary=binary)


__all__ = ["HerdrMultiplexerAdapter", "adapter"]
