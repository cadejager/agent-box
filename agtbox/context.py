from dataclasses import dataclass, field


@dataclass
class RunContext:
    agent: object
    binds: list
    env: list                      # list[tuple[str, str]], mutable (prepare may append)
    app_dir: str
    volumes: list = field(default_factory=list)
    ro_volumes: list = field(default_factory=list)
    extra_args: list = field(default_factory=list)
