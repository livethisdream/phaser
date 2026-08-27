"""Remediation text must be correct for the platform it is printed on."""

import pytest

from phaser_deploy import advice as adv

TOPICS = sorted(adv.ADVICE)
CTX = dict(user="analog", host="phaser.local", python="/usr/bin/python3",
           pkgs="msgpack websockets", want="3.9", have="3.8")


@pytest.mark.parametrize("topic", TOPICS)
@pytest.mark.parametrize("key", ["nt", "posix"])
def test_every_topic_renders_on_both_platforms(topic, key):
    text = adv.advice(topic, key=key, **CTX)
    assert text.strip()


@pytest.mark.parametrize("topic", TOPICS)
@pytest.mark.parametrize("key", ["nt", "posix"])
def test_advice_is_ascii(topic, key):
    """A legacy Windows console is cp1252; a stray en-dash raises
    UnicodeEncodeError in the middle of a deploy."""
    assert adv.advice(topic, key=key, **CTX).isascii()


@pytest.mark.parametrize("topic", TOPICS)
def test_windows_advice_never_suggests_posix_only_tools(topic):
    """ssh-copy-id does not exist on Windows, and a PowerShell user cannot run
    a .sh. This bug recurred every time a new message was added -- as data,
    one test covers messages nobody has written yet."""
    text = adv.advice(topic, key="nt", **CTX)
    assert "ssh-copy-id" not in text
    assert ".sh" not in text
    assert "./scripts/" not in text
    # python3.exe on Windows is a Microsoft Store App Execution Alias that may
    # not be a real interpreter, so LOCAL invocations must say "python". A
    # remote path like /usr/bin/python3 is the Pi's interpreter and is fine.
    assert "python3 deploy.py" not in text


@pytest.mark.parametrize("topic", TOPICS)
def test_posix_advice_never_suggests_powershell(topic):
    assert ".ps1" not in adv.advice(topic, key="posix", **CTX)
