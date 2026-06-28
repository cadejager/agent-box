import os
import shutil
from abc import ABC, abstractmethod


class Sandbox(ABC):
    name = ""
    priority = 0
    install_full_env = False     # True -> install gets the full run env allowlist

    @classmethod
    def is_available(cls):
        return bool(shutil.which(cls.name))

    @abstractmethod
    def fmt_env(self, pairs): ...
    @abstractmethod
    def fmt_bind(self, src, dst, ro): ...
    @abstractmethod
    def build_run_argv(self, ctx): ...
    @abstractmethod
    def install_machine(self): ...
    @abstractmethod
    def install(self, script, pairs): ...

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
