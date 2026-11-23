from agtbox import core
from agtbox.agents.opencode import Opencode

class OMOS(Opencode):
    name = "omos"
    packages = ("@opencode/cli","oh-my-opencode-slim",)

    @property
    def bin(self):
        # oh-my-opencode-slim is a plugin; the executable is still opencode.
        return f"{core.AGENT_TOOLS}/bin/opencode"

    @property
    def binds(self):
        return [
            core.Bind(f"{core.AGENT_CONFIG}/omos", f"{core.HOME}/.config/opencode"),
            core.Bind(f"{core.AGENT_TOOLS}/omos", f"{core.HOME}/.local/share/opencode"),
            core.Bind(f"{core.AGENT_STATE}/omos", f"{core.HOME}/.local/state/opencode"),
            core.Bind(f"{core.AGENT_CACHE}/omos", f"{core.HOME}/.cache/opencode"),
        ]

