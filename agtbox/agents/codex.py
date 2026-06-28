from agtbox import core
from agtbox.agents.base import Agent


class Codex(Agent):
    name = "codex"
    packages = ("@openai/codex",)
    env_forward = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "CODEX_API_KEY")

    @property
    def binds(self):
        return [core.Bind(f"{core.AGENT_CONFIG}/codex", f"{core.HOME}/.codex")]
