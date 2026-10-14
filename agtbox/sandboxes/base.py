import os
import shutil
import signal
import subprocess
import sys
from abc import ABC, abstractmethod


class Sandbox(ABC):
    name = ""
    priority = 0

    @classmethod
    def is_available(cls):
        return bool(shutil.which(cls.name))

    @abstractmethod
    def fmt_env(self, pairs): ...
    @abstractmethod
    def fmt_bind(self, src, dst, ro): ...
    @abstractmethod
    def build_run_argv(self, ctx, cmd=None): ...
    @abstractmethod
    def install_machine(self): ...
    @abstractmethod
    def install(self, script, pairs): ...

    def command(self, ctx):
        return [ctx.agent.bin, *ctx.agent.default_args, *ctx.extra_args]

    def prepare(self, ctx):
        pass

    def rebuild(self):
        pass

    def bind_args(self, ctx):
        args = []
        for b in ctx.binds:
            if b.kind == "seed":
                continue
            args += self.fmt_bind(b.src, b.dst, False)
        for m in ctx.volumes:
            args += self.fmt_bind(m, m, False)
        for m in ctx.ro_volumes:
            args += self.fmt_bind(m, m, True)
        return args

    def env_args(self, ctx):
        return self.fmt_env(ctx.env)

    def run(self, ctx):
        argv = self.build_run_argv(ctx)
        os.execvp(argv[0], argv)

    def wait(self, ctx):
        """Run with inherited terminal I/O, keeping the launcher alive for cleanup.

        Forward termination signals and retain ownership until the sandbox has
        exited. SIGKILL/host failure deliberately leaves a recoverable stale lock.
        """
        child = None
        pending = []

        def forward(signum, frame):
            if child is None:
                pending.append(signum)
            else:
                child.send_signal(signum)

        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        previous = {s: signal.signal(s, forward) for s in signals}
        try:
            with subprocess.Popen(self.build_run_argv(ctx)) as child:
                for signum in pending:
                    child.send_signal(signum)
                code = child.wait()
            return code if code >= 0 else 128 - code
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def run_setup(self, ctx):
        if not ctx.agent.setup_args:
            return
        argv = self.build_run_argv(ctx, [ctx.agent.bin, *ctx.agent.setup_args])
        if subprocess.run(argv, check=False).returncode != 0:
            print(f"Agent Box: warning: `{ctx.agent.name} {' '.join(ctx.agent.setup_args)}` "
                  "failed; launching anyway.", file=sys.stderr)
