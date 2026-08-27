"""Platform-correct remediation text.

Advice is DATA, not branches. Every user-facing "here is how to fix it" string
lives in ADVICE, keyed by topic and then by os.name, and is looked up at runtime.

The reason is a bug we shipped repeatedly: messages hardcoded POSIX remedies --
`ssh-copy-id` (which does not exist on Windows) and `./scripts/setup.sh` (which
a PowerShell user cannot execute). Every time a new message was added, the same
mistake was available again. As data, a single parametrized test asserts that no
"nt" variant mentions a POSIX-only tool and no "posix" variant mentions a .ps1,
which closes the hole for messages nobody has written yet.

Keep every string ASCII-only. A legacy Windows console is cp1252 and a stray
en-dash raises UnicodeEncodeError in the middle of a deploy.
"""

import os

# python3.exe on Windows is a Microsoft Store App Execution Alias that may not
# be a real interpreter, so Windows advice must always say "python".
_PY = {"nt": "python", "posix": "python3"}

ADVICE = {
    "no_key": {
        "posix": "Copy your key so ssh stops asking:\n"
                 "    ssh-copy-id {user}@{host}",
        # Windows OpenSSH ships no ssh-copy-id.
        "nt": "Copy your key so ssh stops asking:\n"
              "    type $HOME\\.ssh\\id_ed25519.pub | ssh {user}@{host} "
              "\"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys\"\n"
              "  This matters more on Windows: OpenSSH here has no ControlMaster,\n"
              "  so without a key you are prompted once per file.",
    },
    "no_dns": {
        "posix": "The name does not resolve. Try the IP address instead, or check\n"
                 "  that you are on the same network as the Pi.",
        "nt": "The name does not resolve. If it ends in .local, Windows needs mDNS\n"
              "  (Bonjour) -- try the IP address instead.",
    },
    "unreachable": {
        "posix": "It resolves but nothing is listening on port 22 -- the Pi is off,\n"
                 "  still booting, or on another network.\n"
                 "    ssh {user}@{host} 'echo ok'",
        "nt": "It resolves but nothing is listening on port 22 -- the Pi is off,\n"
              "  still booting, or on another network.\n"
              "    ssh {user}@{host} \"echo ok\"",
    },
    "no_ssh": {
        "posix": "Install an OpenSSH client (package 'openssh-client').",
        "nt": "Install the OpenSSH Client:\n"
              "  Settings > Apps > Optional features > Add > OpenSSH Client",
    },
    "missing_deps": {
        "posix": "Install them on the Pi:\n"
                 "    ssh {user}@{host} '{python} -m pip install --user {pkgs}'",
        "nt": "Install them on the Pi:\n"
              "    ssh {user}@{host} \"{python} -m pip install --user {pkgs}\"",
    },
    "needs_terminal": {
        "posix": "Re-run from a terminal:\n    {py} deploy.py {host}",
        "nt": "Re-run from a terminal:\n    {py} deploy.py {host}",
    },
    "old_python": {
        "posix": "Python {want}+ is required; this is {have}.\n"
                 "  Try:  {py} --version",
        "nt": "Python {want}+ is required; this is {have}.\n"
              "  Install from python.org (not the Microsoft Store alias).",
    },
    "no_npm": {
        "posix": "Building needs Node + npm on PATH. Drop --build to deploy the\n"
                 "  committed build instead.",
        "nt": "Building needs Node + npm on PATH. Drop --build to deploy the\n"
              "  committed build instead.",
    },
}


def platform_key():
    """'nt' or 'posix'. Indirected so tests can parametrize over both."""
    return "nt" if os.name == "nt" else "posix"


def advice(topic, *, key=None, **ctx):
    """Remediation text for `topic` on this platform, formatted with ctx."""
    variants = ADVICE[topic]
    text = variants[key or platform_key()]
    ctx.setdefault("py", _PY[key or platform_key()])
    return text.format(**ctx)
