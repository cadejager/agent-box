from abc import ABC
from agtbox import core


class Agent(ABC):
    """One AI coding CLI. Subclasses set `name` and override what they need.
    The sequence defaults are tuples (immutable) so the shared class-level default
    can't be mutated in place and leak between agents."""
    name = ""
    packages = ()
    env_forward = ()
    env_literal = ()

    @property
    def bin(self):
        return f"{core.AGENT_TOOLS}/bin/{self.name}"

    @property
    def binds(self):
        return []
