from agtbox import core
from agtbox.agents.base import Agent


class Opencode(Agent):
    name = "opencode"
    packages = ["opencode-ai"]
    env_forward = ["OPENCODE_ENABLE_EXA"]
    env_literal = ["OPENCODE_EXPERIMENTAL_LSP_TOOL=true"]

    @property
    def binds(self):
        return [
            core.Bind(f"{core.AGENT_CONFIG}/opencode", f"{core.HOME}/.config/opencode"),
            core.Bind(f"{core.AGENT_TOOLS}/opencode", f"{core.HOME}/.local/share/opencode"),
            core.Bind(f"{core.AGENT_STATE}/opencode", f"{core.HOME}/.local/state/opencode"),
            core.Bind(f"{core.AGENT_CACHE}/opencode", f"{core.HOME}/.cache/opencode"),
        ]
