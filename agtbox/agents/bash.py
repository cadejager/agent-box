from agtbox.agents.base import Agent


class Bash(Agent):
    """An audit shell inside the sandbox: the system bash, no toolchain binary,
    no config, no packages."""
    name = "bash"

    @property
    def bin(self):
        return "/usr/bin/bash"
