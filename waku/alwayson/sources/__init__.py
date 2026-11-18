"""One handler per source. Each checks who sent the message, inserts one row
into `events` and returns (spec 023, decision 1)."""


class MissingExtra(RuntimeError):
    """A source's setting is there and the extra it needs is not installed.
    A source's `start` raises this, and `waku serve` names the source on the
    line that lists the sources."""

    def __init__(self, setting: str, extra: str) -> None:
        super().__init__(f"{setting} is set, but the extra is not installed: "
                         f"pip install 'waku-agent[{extra}]'")
