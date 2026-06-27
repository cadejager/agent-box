from agtbox import core
from agtbox.agents.base import Agent


class Claude(Agent):
    name = "claude"
    packages = ["@anthropic-ai/claude-code"]
    env_forward = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                   "ANTHROPIC_API_KEY", "ANTHROPIC_DEFAULT_SONNET_MODEL"]
    env_literal = ["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1"]

    @property
    def binds(self):
        return [
            core.Bind(f"{core.AGENT_CONFIG}/claude", f"{core.HOME}/.claude"),
            core.Bind(f"{core.AGENT_CONFIG}/claude.json", f"{core.HOME}/.claude.json", "file"),
        ]
