import os
import shutil
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from contextlib import nullcontext


CAPTURE_TIMEOUT_CODE = 124  # like timeout(1)


class SignalGuard:
    """The launcher's signal handling for a sandbox lifetime that spans several
    children (setup, credential commands, the agent).

    SIGTERM/SIGHUP go to the attached child. Unless that child is the agent
    (attached with deliver_pending=True) they are also recorded in `pending`, and
    the agent receives every recorded signal when it is attached: a SIGTERM that
    stops setup or a credential command must still stop the agent that would
    start next. Credential commands never receive signals sent before they ran.
    A terminal hangup can signal only the launcher as session leader, so SIGHUP
    must be forwarded. SIGINT runs a Python no-op: terminal Ctrl-C already reaches
    the foreground process group, and unlike SIG_IGN a Python handler resets on
    exec, so children keep default Ctrl-C handling.
    """

    def __init__(self):
        self.child = None
        self.pending = []
        self._child_is_agent = False
        self._previous = {}

    def _forward(self, signum, frame):
        if self.child is not None:
            self.child.send_signal(signum)
        if self.child is None or not self._child_is_agent:
            self.pending.append(signum)

    @staticmethod
    def _ignore(signum, frame):
        pass

    def __enter__(self):
        handlers = {signal.SIGTERM: self._forward, signal.SIGINT: self._ignore,
                    signal.SIGHUP: self._forward}
        self._previous = {s: signal.signal(s, h) for s, h in handlers.items()}
        return self

    def __exit__(self, *exc):
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)
        return False

    def attach(self, child, deliver_pending):
        # Mark the child's role before publishing it, so a signal landing between
        # the two assignments is not both forwarded to the agent and recorded.
        self._child_is_agent = deliver_pending
        self.child = child
        if deliver_pending:
            pending, self.pending = self.pending, []
            for signum in pending:
                child.send_signal(signum)

    def detach(self):
        # Unpublish the child first: from then on every signal is recorded.
        self.child = None
        self._child_is_agent = False


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
    def build_run_argv(self, ctx, cmd=None, tty=None): ...
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
            args += self.fmt_bind(b.src, b.dst, b.kind == "ro")
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

    def wait(self, ctx, guard=None):
        """Run without exec so the caller can clean up; returns the exit status.
        A caller whose cleanup also runs children passes its own SignalGuard."""
        with (SignalGuard() if guard is None else nullcontext(guard)) as active:
            with subprocess.Popen(self.build_run_argv(ctx)) as child:
                active.attach(child, deliver_pending=True)
                try:
                    # Poll: a signal landing just before a blocking waitpid would
                    # defer its Python handler (the forward) until the child exits.
                    while True:
                        try:
                            code = child.wait(timeout=0.5)
                            break
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    active.detach()
        return code if code >= 0 else 128 - code

    def capture(self, ctx, cmd, stdin=None, timeout=None, guard=None):
        """Run `cmd` in the sandbox without a TTY; return (returncode, stdout).

        `stdin` (str) is piped in, else /dev/null; stderr passes through. The child
        gets its own session so terminal Ctrl-C cannot interrupt it; with `guard`,
        SIGTERM/SIGHUP received while it runs are forwarded to it. A timeout kills
        it and returns (CAPTURE_TIMEOUT_CODE, "").
        """
        argv = self.build_run_argv(ctx, cmd, tty=False)
        with subprocess.Popen(argv, encoding="utf-8", errors="replace",
                              start_new_session=True,
                              stdin=subprocess.DEVNULL if stdin is None else subprocess.PIPE,
                              stdout=subprocess.PIPE) as child:
            if guard is not None:
                guard.attach(child, deliver_pending=False)
            try:
                out, _ = child.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired:
                # Kill the whole session (the child leads its own): under podman
                # the client is only one of several processes holding the pipe.
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.kill()
                try:
                    child.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    pass  # something outside the session still holds the pipe; give up
                return CAPTURE_TIMEOUT_CODE, ""
            finally:
                if guard is not None:
                    guard.detach()
        return child.returncode, out

    def run_setup(self, ctx):
        if not ctx.agent.setup_args:
            return
        argv = self.build_run_argv(ctx, [ctx.agent.bin, *ctx.agent.setup_args])
        if subprocess.run(argv, check=False).returncode != 0:
            print(f"Agent Box: warning: `{ctx.agent.name} {' '.join(ctx.agent.setup_args)}` "
                  "failed; launching anyway.", file=sys.stderr)
