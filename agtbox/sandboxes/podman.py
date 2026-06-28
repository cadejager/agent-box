import os
import shutil
import subprocess
import sys
from pathlib import Path
from agtbox import core
from agtbox.sandboxes.base import Sandbox

PROJ_DIR = str(Path(__file__).resolve().parents[2])   # .../agtbox/sandboxes/podman.py -> repo root


class Podman(Sandbox):
    name = "podman"
    priority = 10
    install_full_env = False     # podman install: AGENT_ENV only (matches old behavior)

    def fmt_env(self, pairs):
        out = []
        for k, v in pairs:
            out += ["-e", f"{k}={v}"]
        return out

    def fmt_bind(self, src, dst, ro):
        return ["-v", f"{src}:{dst}:ro" if ro else f"{src}:{dst}"]

    def prepare(self, ctx):
        self.build_image()
        self.derive_tz(ctx)

    def derive_tz(self, ctx):
        try:
            link = os.readlink("/etc/localtime")
        except OSError:
            return
        _, sep, zone = link.partition("/zoneinfo/")
        if sep and zone:
            ctx.env.append(("TZ", zone))

    def rebuild(self):
        subprocess.run(["podman", "image", "rm", core.IMAGE],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def build_image(self):
        if subprocess.run(["podman", "image", "exists", core.IMAGE]).returncode == 0:
            return
        print(f"Agent Box: building the {core.IMAGE} image (one-time)...", file=sys.stderr)
        self.refresh_certs()
        subprocess.run(["podman", "build", "-t", core.IMAGE, "-f",
                        f"{PROJ_DIR}/container/Containerfile", f"{PROJ_DIR}/container"], check=True)

    def refresh_certs(self):
        """Copy the host's company CA certs into container/certs/ so the image build can
        bake them into its trust store -- needed behind a TLS-intercepting proxy, since
        the podman image starts from a fresh Debian trust store that lacks them. Source
        defaults to ~/.local/share/certs, overridable via AGENT_CERTS_DIR; missing/empty
        is fine (the cp is best-effort). podman engine only -- bwrap reuses the host's
        /etc trust store via its --ro-bind /etc, so it needs no cert logic."""
        src = os.environ.get("AGENT_CERTS_DIR") or f"{core.HOME}/.local/share/certs"
        dst = f"{PROJ_DIR}/container/certs"
        shutil.rmtree(dst, ignore_errors=True)
        os.makedirs(dst, exist_ok=True)
        # Best-effort `cp "${src}"/* "${dst}/"`: missing/empty source is fine.
        try:
            for name in os.listdir(src):
                # Match bash `cp "${src}"/* "${dst}/"`: no dotglob (skip dotfiles) and
                # a non-recursive cp (skip dirs) -- certs are flat *.crt files.
                if name.startswith("."):
                    continue
                s = os.path.join(src, name)
                if not os.path.isfile(s):
                    continue
                try:
                    shutil.copy2(s, dst)
                except OSError:
                    pass
        except OSError:
            pass

    def build_run_argv(self, ctx):
        pd = ["run", "-it", "--rm", "--security-opt", "label=disable"]
        pd += self.env_args(ctx)
        pd += ["-v", f"{core.AGENT_TOOLS}:{core.AGENT_TOOLS}",
               "-v", f"{core.AGENT_CACHE}:{core.AGENT_CACHE}"]
        pd += ["-v", f"{ctx.app_dir}:{ctx.app_dir}", "-w", ctx.app_dir]
        pd += self.bind_args(ctx)
        return ["podman", *pd, "--", core.IMAGE, ctx.agent.bin, *ctx.extra_args]

    def install_machine(self):
        # Arch comes from the IMAGE, not the host: a macOS host differs from the
        # Linux container that actually runs the toolchain.
        return subprocess.run(["podman", "run", "--rm", core.IMAGE, "uname", "-m"],
                              check=True, capture_output=True, text=True).stdout.strip()

    def install(self, script, pairs):
        # `pairs` already resolved by ensure_tools (AGENT_ENV-only for podman).
        pd = ["run", "--rm", "--security-opt", "label=disable",
              "-v", f"{core.AGENT_TOOLS}:{core.AGENT_TOOLS}",
              "-v", f"{core.AGENT_CACHE}:{core.AGENT_CACHE}"]
        pd += self.fmt_env(pairs)
        subprocess.run(["podman", *pd, "--", core.IMAGE, "/usr/bin/bash", "-c", script], check=True)
