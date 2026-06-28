import argparse
import os
import sys
from agtbox import core
from agtbox.context import RunContext
from agtbox.discovery import discover_agents, discover_sandboxes
from agtbox.install import ensure_tools


def build_parser(sandbox_names, agent_names):
    p = argparse.ArgumentParser(
        prog="agtbox.py", allow_abbrev=False,
        description="Run an AI coding agent in an unprivileged sandbox.",
        epilog="Pass agent args after `--`, e.g. `%(prog)s claude -- --resume`.")
    p.add_argument("-a", dest="app_dir", default=os.getcwd(), metavar="DIR",
                   help="project directory, bound at the same path inside (default: cwd)")
    p.add_argument("-w", dest="volumes", action="append", default=[], metavar="VOL",
                   help="extra dir, bound read-write at the same path (repeatable)")
    p.add_argument("-r", dest="ro_volumes", action="append", default=[], metavar="VOL",
                   help="extra dir, bound read-only at the same path (repeatable)")
    p.add_argument("-s", dest="sandbox", choices=tuple(sandbox_names),
                   help="sandbox (default: auto -- highest priority available)")
    p.add_argument("-b", dest="rebuild", action="store_true",
                   help="rebuild the sandbox image (no-op for sandboxes without one)")
    p.add_argument("-u", "--update", dest="update", action="store_true",
                   help="refresh the toolchain + the agent's packages, then run")
    p.add_argument("agent", choices=tuple(agent_names),
                   help="the agent to run (or `bash` for an audit shell)")
    return p


def resolve_sandbox(name, sandboxes):
    if name:
        cls = sandboxes[name]
        if not cls.is_available():
            print(f"Error: sandbox '{name}' is selected but not installed.", file=sys.stderr)
            sys.exit(1)
        return cls()
    for _name, cls in sorted(sandboxes.items(), key=lambda kv: kv[1].priority, reverse=True):
        if cls.is_available():
            return cls()
    print("Error: no sandbox found (need bwrap or podman).", file=sys.stderr)
    sys.exit(1)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        sep = argv.index("--")
        left, extra = argv[:sep], argv[sep + 1:]
    else:
        left, extra = argv, []

    sandboxes = discover_sandboxes()
    agents = discover_agents()
    ns = build_parser(list(sandboxes), list(agents)).parse_args(left)

    sandbox = resolve_sandbox(ns.sandbox, sandboxes)
    agent = agents[ns.agent]()

    app_dir, volumes, ro_volumes = core.normalize_paths(ns.app_dir, ns.volumes, ns.ro_volumes)
    binds = [*core.SHARED_BINDS, *agent.binds]
    core.ensure_sources(binds)
    env = core.resolve_env(agent.env_forward, agent.env_literal)
    ctx = RunContext(agent=agent, binds=binds, env=env, app_dir=app_dir,
                     volumes=volumes, ro_volumes=ro_volumes, extra_args=extra)

    if ns.rebuild:
        sandbox.rebuild()
    sandbox.prepare(ctx)
    ensure_tools(agent, sandbox, force=ns.update)
    sandbox.run(ctx)
