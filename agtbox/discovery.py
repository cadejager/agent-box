"""Runtime discovery of the in-repo sandbox/agent plugins."""
import importlib
import pkgutil
import sys
from agtbox import agents as agents_pkg
from agtbox import sandboxes as sandboxes_pkg
from agtbox.agents.base import Agent
from agtbox.sandboxes.base import Sandbox


def _discover(package, base):
    found = {}
    for info in pkgutil.iter_modules(package.__path__):
        if info.name == "base":
            continue
        mod = importlib.import_module(f"{package.__name__}.{info.name}")
        for obj in vars(mod).values():
            # `obj.__module__ == mod.__name__` so a plugin that *imports* another
            # plugin's class (e.g. to subclass it) doesn't re-register it here.
            if not (isinstance(obj, type) and issubclass(obj, base) and obj is not base
                    and obj.__module__ == mod.__name__):
                continue
            if not obj.name:
                sys.exit(f"Error: plugin {info.name} ({obj.__name__}) has an empty name.")
            if obj.name in found:
                sys.exit(f"Error: two plugins claim the name '{obj.name}' "
                         f"({found[obj.name].__module__}, {obj.__module__}).")
            found[obj.name] = obj
    return dict(sorted(found.items()))


def discover_agents():
    return _discover(agents_pkg, Agent)


def discover_sandboxes():
    return _discover(sandboxes_pkg, Sandbox)
