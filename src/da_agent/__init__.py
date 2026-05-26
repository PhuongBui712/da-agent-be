"""DA-Agent: an Excel data-analyst agent built on the Claude Agent SDK."""
from .agent.core import AgentRunner
from .config import Settings
from .ui.console import ConsoleAgentUI

__version__ = "0.1.0"
__all__ = ["AgentRunner", "Settings", "ConsoleAgentUI"]
