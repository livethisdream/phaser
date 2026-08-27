"""The ssh_argv seam: quoting, platform flags, and the two-shells invariant."""

import re
import shlex

import pytest

from phaser_deploy.remote import Target, scp_argv, ssh_argv


def test_remote_command_is_exactly_one_argv_element(target):
    """The Pi's /bin/sh must receive one quoted string, joined exactly once.

    Passing argv lists to subprocess kills the LOCAL shell, but ssh still
    concatenates its trailing arguments for the remote shell. If a path with a
    space were interpolated into a remote command string, it would split there.
    """
    argv = ssh_argv(target, ["mkdir", "-p", "/home/a b/c"])
    assert argv[-1] == "mkdir -p '/home/a b/c'"
    # Nothing else in the argv may contain a space -- if it did, the caller
    # built a string somewhere instead of passing a list.
    assert not any(" " in part for part in argv[:-1])


@pytest.mark.parametrize("nasty", [
    "/home/a b/c", "/home/a$b/c", "/home/a`b`/c", "/home/a;rm -rf /;/c",
    "/home/a&&b/c", "/home/a'b/c", "/home/a%PATH%/c",
])
def test_remote_paths_survive_quoting(target, nasty):
    """Metacharacters that cmd.exe or /bin/sh would eat must round-trip."""
    argv = ssh_argv(target, ["test", "-e", nasty])
    # shlex.split reverses what the remote shell would do to the joined string.
    assert shlex.split(argv[-1]) == ["test", "-e", nasty]


def test_no_pty_flag_present_for_binary_streams(target):
    """-T is mandatory when stdin carries binary: a pty does CR/LF translation
    and silently corrupts the stream."""
    assert "-T" in ssh_argv(target, ["cat"], no_pty=True)
    assert "-t" not in ssh_argv(target, ["cat"], no_pty=True)


def test_batchmode_only_when_asked(target):
    """BatchMode refuses password auth, so it belongs only on probes whose
    failure is an answer -- never on a path a password-auth user must take."""
    assert "BatchMode=yes" in ssh_argv(target, ["true"], batch=True)
    assert "BatchMode=yes" not in ssh_argv(target, ["true"])


def test_scp_keeps_local_paths_as_separate_elements():
    """Local paths must never be concatenated into one string: a Windows
    C:\\... path in a joined command is parsed by scp as host:path."""
    argv = scp_argv([r"C:\Users\a b\repo\x.py"], "analog@pi:/dst/")
    assert r"C:\Users\a b\repo\x.py" in argv
    assert argv[-1] == "analog@pi:/dst/"


def test_no_glob_metacharacters_are_ever_emitted(target):
    """cmd.exe does not expand globs, so a '*' in a command reaches the program
    literally. This is the bug that shipped a broken frontend copy on Windows."""
    argvs = [
        ssh_argv(target, ["mkdir", "-p", "/a"]),
        scp_argv(["/local/dist"], "analog@pi:/dst/", recursive=True),
    ]
    for argv in argvs:
        assert not any("*" in part or "?" in part for part in argv)


def test_target_renders_as_user_at_host():
    assert str(Target("analog", "phaser.local")) == "analog@phaser.local"
