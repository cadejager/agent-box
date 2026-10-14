"""The toolchain's downloadable tools, as data.

One entry per tool installed from a release tarball. `install.py` renders these into
the bash install script; nothing in this module runs a command, opens a socket, or
touches the disk.

Two placeholder dialects meet here and must not be confused:

  {narch} {goarch} {uvarch}  Python str.format -- substituted HERE, from
                             core.arch_names(), before the script is built.
  @VER@ @VERNOV@             Left for bash -- substituted inside the sandbox once
                             the version has been resolved. @VERNOV@ is the version
                             with a leading 'v' stripped (gh and glab put the bare
                             number in their asset names and paths).
"""
from dataclasses import dataclass

from agtbox import core


@dataclass(frozen=True)
class Release:
    """One downloadable tool.

    `api` is where the version comes from, read one of three ways:
      - empty: nothing to resolve; `url` is a version-free `releases/latest/download/`
        link (uv).
      - with `tag_field` empty: a `releases/latest` page whose redirect target ends
        in the tag (gh, glab). This goes to the web host, so there is no separate API
        host to allowlist and no api.github.com 60/hour anonymous rate limit.
      - with `tag_field` set: a JSON API, read by key (node). `where` names a key a
        row must have truthy to be chosen (node's "lts"); empty means the first row.
    `dest` selects tree mode (extract in place, --strip-components=1); `bins` selects
    bins mode ("src-in-tarball:dest-name"). Exactly one of the two is set.
    """
    name: str
    url: str
    api: str = ""
    tag_field: str = ""
    where: str = ""
    required: bool = False
    dest: str = ""
    bins: tuple = ()


TOOLS = (
    Release(
        name="node",
        required=True,
        api="https://nodejs.org/dist/index.json",
        where="lts",
        tag_field="version",
        url="https://nodejs.org/dist/@VER@/node-@VER@-linux-{narch}.tar.xz",
        dest="${AGT_TOOLS}/node",
    ),
    Release(
        name="uv",
        url="https://github.com/astral-sh/uv/releases/latest/download/"
            "uv-{uvarch}-unknown-linux-gnu.tar.gz",
        bins=("uv-{uvarch}-unknown-linux-gnu/uv:uv",
              "uv-{uvarch}-unknown-linux-gnu/uvx:uvx"),
    ),
    Release(
        name="gh",
        api="https://github.com/cli/cli/releases/latest",
        url="https://github.com/cli/cli/releases/download/@VER@/"
            "gh_@VERNOV@_linux_{goarch}.tar.gz",
        bins=("gh_@VERNOV@_linux_{goarch}/bin/gh:gh",),
    ),
    Release(
        name="glab",
        api="https://gitlab.com/gitlab-org/cli/-/releases/permalink/latest",
        url="https://gitlab.com/gitlab-org/cli/-/releases/@VER@/downloads/"
            "glab_@VERNOV@_linux_{goarch}.tar.gz",
        bins=("bin/glab:glab",),
    ),
)


def render(machine):
    """(constants block, call lines) for `machine`, arch already substituted.

    Constants keep the long URL templates off the call lines so the generated
    script stays readable when dumped or eyeballed.
    """
    names = core.arch_names(machine)
    consts, calls = [], []
    for t in TOOLS:
        up = t.name.upper()
        consts.append(f"readonly {up}_API='{t.api}'")
        consts.append(f"readonly {up}_URL='{t.url.format(**names)}'")
        req = "required" if t.required else "optional"
        head = (f'{t.name} {req} "${up}_API" "{t.where}" "{t.tag_field}" "${up}_URL"')
        if t.dest:
            calls.append(f'install_tree {head} "{t.dest}"')
        else:
            bins = " ".join(f'"{b.format(**names)}"' for b in t.bins)
            calls.append(f"install_bins {head} {bins}")
    return "\n".join(consts), "\n".join(calls)
