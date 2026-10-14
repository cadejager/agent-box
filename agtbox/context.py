from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agtbox.agents.base import Agent


@dataclass
class RunContext:
    agent: "Agent"
    binds: list
    env: list                      # list[tuple[str, str]], mutable (prepare may append)
    app_dir: str
    volumes: list = field(default_factory=list)
    ro_volumes: list = field(default_factory=list)
    extra_args: list = field(default_factory=list)
