"""Static checks on the provisioning scripts.

None of this can run the real thing -- provision.sh only makes sense on a Pi
with sudo, apt and a Phaser board. So these guard the specific mistakes that
would otherwise only surface on a bench, mid-workshop, ten cards in.

The upstream script this replaces
(https://github.com/thorenscientific/rpi_setup_stuff) is not safe to run twice:
it backs up /boot/config.txt on every run, so the second run's "original" is
the first run's edited copy. Several tests below exist to keep that class of
bug from coming back.
"""

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROVISION = ROOT / "scripts" / "provision.sh"
PI_DIR = ROOT / "scripts" / "pi"

# Files provision.sh installs onto the Pi out of scripts/pi/.
VENDORED = [
    "89-pluto.rules",
    "iiod-usb@.service",
    "pluto_update_ad9361.sh",
    "phaser-clock",
    "phaser-clock.service",
    "phaser-firstboot",
    "phaser-firstboot.service",
]

SHELL_SCRIPTS = [
    PROVISION,
    PI_DIR / "phaser-clock",
    PI_DIR / "phaser-firstboot",
    PI_DIR / "pluto_update_ad9361.sh",
]


@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shell_syntax(script):
    """`bash -n` catches the unbalanced quote before a kit does."""
    assert script.exists(), f"{script} is missing"
    r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
    assert r.returncode == 0, f"{script.name} has a syntax error:\n{r.stderr}"


@pytest.mark.parametrize("name", VENDORED)
def test_vendored_file_exists(name):
    """provision.sh copies these by name; a missing one fails mid-provision."""
    assert (PI_DIR / name).exists(), f"scripts/pi/{name} is missing"


def test_provision_references_only_files_that_exist():
    """Every scripts/pi/<file> path in provision.sh resolves.

    A typo here is invisible until a kit is halfway through provisioning.
    """
    text = PROVISION.read_text(encoding="utf-8")
    referenced = set(re.findall(r'\$SRC/scripts/pi/([A-Za-z0-9_.@-]+)', text))
    assert referenced, "expected provision.sh to install files from scripts/pi/"
    missing = sorted(n for n in referenced if not (PI_DIR / n).exists())
    assert missing == [], f"provision.sh installs nonexistent file(s): {missing}"


@pytest.mark.parametrize("name", ["phaser-clock", "phaser-firstboot",
                                  "pluto_update_ad9361.sh"])
def test_executable_bit(name):
    """Committed without +x, these install as non-executable and the unit fails."""
    mode = (PI_DIR / name).stat().st_mode
    assert mode & stat.S_IXUSR, f"scripts/pi/{name} is not executable in git"


def test_config_txt_is_merged_not_replaced():
    """The overlay lines are merged into the image's config.txt, never over it.

    Upstream ships a whole stock bullseye config.txt and copies it over
    /boot/config.txt, silently reverting whatever the running Kuiper image had.
    """
    text = PROVISION.read_text(encoding="utf-8")
    assert "phaser provision >>>" in text, "expected a delimited managed block"
    # The tell for the wholesale-replace approach.
    assert "config_phaser.txt" not in text
    assert not re.search(r'mv\s+["\']?\$?\{?BOOTDIR', text), \
        "provision.sh must not mv the boot config out of the way"


def test_config_backup_is_taken_only_once():
    """Re-running must not overwrite the pristine backup with an edited copy.

    This is the exact bug in the upstream script.
    """
    text = PROVISION.read_text(encoding="utf-8")
    assert re.search(r'if \[ ! -f "\$CFG\.phaser-orig" \]', text), \
        "the config.txt backup must be guarded by a not-exists check"


def test_managed_block_opens_with_all_section():
    """config.txt is sectioned; the block must reset to [all].

    Appended after a trailing [pi4] or [cm4] filter, the overlay would apply to
    only one board revision and the Phaser would not enumerate on the others.
    """
    text = PROVISION.read_text(encoding="utf-8")
    # The heredoc that builds the block, not the BEGIN/END variable
    # declarations that happen to contain the same marker text.
    m = re.search(r'BLOCK="\$\(cat <<EOF\n(.*?)\nEOF', text, re.DOTALL)
    assert m, "could not find the managed-block heredoc in provision.sh"
    block = m.group(1)
    lines = [ln.strip() for ln in block.splitlines()
             if ln.strip() and not ln.strip().startswith("#")
             and not ln.strip().startswith("$")]
    assert lines and lines[0] == "[all]", \
        f"managed block must start with [all], got {lines[:1]}"
    assert "dtoverlay=rpi-cn0566" in lines, "the CN0566 overlay must be in the block"


def test_pyadi_is_not_rebuilt_unconditionally():
    """Only build pyadi-iio when adi.CN0566 actually fails to import.

    Upstream uninstalls and rebuilds from the tip of main on every run, which
    moves a working workshop kit onto whatever landed upstream that morning.
    """
    text = PROVISION.read_text(encoding="utf-8")
    assert "import adi; adi.CN0566" in text, \
        "expected a capability check before touching pyadi-iio"
    uninstall = text.index("pip uninstall -y pyadi-iio")
    check = text.index("import adi; adi.CN0566")
    assert check < uninstall, "the import check must gate the uninstall, not follow it"


def test_clock_seeds_over_plain_http():
    """The clock bootstrap must not depend on TLS.

    Kuiper ships no NTP client, so a fresh kit has a wrong date; a wrong date
    breaks certificate validation and apt, which is what would otherwise
    install the NTP client. The seed therefore has to be plain HTTP.
    """
    for path in (PROVISION, PI_DIR / "phaser-clock"):
        text = path.read_text(encoding="utf-8")
        hosts = re.findall(r'(https?)://[a-z0-9.-]+/', text)
        seeds = [scheme for scheme in hosts if scheme in ("http", "https")]
        assert "http" in seeds, f"{path.name} has no plain-http time source"
        # The seed list itself must be http; https appears only in repo URLs.
        seed_block = text[text.index("deb.debian.org") - 200:
                          text.index("deb.debian.org") + 200]
        assert "https://deb.debian.org" not in seed_block


def test_clock_bounds_do_not_reference_current_time():
    """Sanity bounds must be fixed constants.

    The premise is that the current clock is wrong, so it cannot be the
    reference for deciding whether a fetched time is plausible.
    """
    text = (PI_DIR / "phaser-clock").read_text(encoding="utf-8")
    assert "1704067200" in text and "4102444800" in text, \
        "expected fixed epoch bounds on the HTTP-seeded time"


def test_firstboot_regenerates_identity():
    """Cloned cards must not share host keys or machine-id."""
    text = (PI_DIR / "phaser-firstboot").read_text(encoding="utf-8")
    assert "ssh-keygen -A" in text, "must regenerate SSH host keys"
    assert "systemd-machine-id-setup" in text, "must regenerate machine-id"
    unit = (PI_DIR / "phaser-firstboot.service").read_text(encoding="utf-8")
    assert "Before=" in unit and "ssh.service" in unit, \
        "host keys must be replaced before sshd offers the golden image's"


def test_boot_partition_path_is_detected():
    """/boot moved to /boot/firmware in bookworm; pinning either breaks the other."""
    for path in (PROVISION, PI_DIR / "phaser-firstboot"):
        text = path.read_text(encoding="utf-8")
        assert "/boot/firmware" in text and "/boot" in text, \
            f"{path.name} must handle both boot partition locations"


def test_hostname_is_validated():
    """An invalid hostname leaves a kit unreachable by name; refuse it instead."""
    for path in (PROVISION, PI_DIR / "phaser-firstboot"):
        text = path.read_text(encoding="utf-8")
        assert "a-zA-Z0-9-" in text, f"{path.name} does not validate the hostname"


def test_install_sh_is_reused_not_reimplemented():
    """provision.sh must chain into install.sh, not carry a second copy of it.

    Two deploy paths drift. install.sh stays the documented update path.
    """
    text = PROVISION.read_text(encoding="utf-8")
    assert 'bash "$SRC/install.sh"' in text, "provision.sh must call install.sh"
    assert "phaser-headless.service.template" not in text, \
        "provision.sh must not render the unit itself; install.sh owns that"


@pytest.mark.skipif(shutil.which("shellcheck") is None,
                    reason="shellcheck not installed")
@pytest.mark.parametrize("script", SHELL_SCRIPTS, ids=lambda p: p.name)
def test_shellcheck(script):
    r = subprocess.run(
        ["shellcheck", "-S", "warning", "-e", "SC1091", str(script)],
        capture_output=True, text=True)
    assert r.returncode == 0, f"shellcheck on {script.name}:\n{r.stdout}"


# ---------------------------------------------------------------------------
# SD card prep (tools/prep_sdcard.py + scripts/pi/firstrun.sh)
#
# This is the one piece that runs on a laptop rather than the Pi. It is allowed
# to exist because it runs no logic against the Pi and opens no connection --
# it writes files to a FAT partition. The tests below are mostly about the two
# ways it could brick a card: a malformed cmdline.txt, and a first-boot hook
# that never disarms itself.
# ---------------------------------------------------------------------------

import importlib.util

PREP = ROOT / "tools" / "prep_sdcard.py"
FIRSTRUN = PI_DIR / "firstrun.sh"


def _load_prep():
    spec = importlib.util.spec_from_file_location("prep_sdcard", PREP)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_card(tmp_path):
    """A directory shaped like a Raspberry Pi FAT boot partition."""
    card = tmp_path / "boot"
    card.mkdir()
    (card / "cmdline.txt").write_text(
        "console=serial0,115200 console=tty1 root=PARTUUID=abcd-02 "
        "rootfstype=ext4 fsck.repair=yes rootwait\n", encoding="utf-8")
    (card / "config.txt").write_text("dtparam=audio=on\n[all]\n", encoding="utf-8")
    return card


def test_prep_sdcard_writes_expected_files(fake_card):
    prep = _load_prep()
    assert prep.main(["--boot", str(fake_card), "--hostname", "phaser-01",
                      "--ip", "192.168.7.11"]) == 0
    for name in ("phaser-hostname", "phaser-ip", "phaser-netalias",
                 "phaser-netalias.service", "firstrun.sh"):
        assert (fake_card / name).is_file(), f"{name} was not written"
    assert (fake_card / "phaser-hostname").read_text().strip() == "phaser-01"
    assert "192.168.7.11/24" in (fake_card / "phaser-ip").read_text()


def test_cmdline_stays_exactly_one_line(fake_card):
    """A cmdline.txt with a stray newline makes the Pi refuse to boot silently."""
    prep = _load_prep()
    prep.main(["--boot", str(fake_card), "--ip", "none"])
    raw = (fake_card / "cmdline.txt").read_bytes()
    assert raw.count(b"\n") <= 1, "cmdline.txt must be a single line"
    assert b"\r" not in raw, "cmdline.txt must not contain carriage returns"
    assert raw.endswith(b"\n")


def test_cmdline_backup_is_taken_and_not_clobbered(fake_card):
    prep = _load_prep()
    original = (fake_card / "cmdline.txt").read_text()
    prep.main(["--boot", str(fake_card), "--ip", "none"])
    backup = fake_card / "cmdline.txt.phaser-orig"
    assert backup.is_file()
    assert backup.read_text() == original
    # A second run must not overwrite the pristine backup with the patched file.
    prep.main(["--boot", str(fake_card), "--ip", "none"])
    assert backup.read_text() == original


def test_prep_is_idempotent(fake_card):
    """Re-running must not stack a second systemd.run onto the cmdline."""
    prep = _load_prep()
    prep.main(["--boot", str(fake_card), "--ip", "none"])
    first = (fake_card / "cmdline.txt").read_text()
    prep.main(["--boot", str(fake_card), "--ip", "none"])
    second = (fake_card / "cmdline.txt").read_text()
    assert first == second
    assert second.count("systemd.run=") == 1


def test_firstrun_disarm_restores_the_original_cmdline(fake_card):
    """The hook must strip itself, or the Pi runs it on every boot forever.

    Applies firstrun.sh's own sed to a cmdline prep_sdcard.py produced and
    checks it round-trips exactly back to the backup. If these two ever drift
    apart, a kit boot-loops through its first-run script.
    """
    prep = _load_prep()
    prep.main(["--boot", str(fake_card), "--ip", "none"])
    patched = (fake_card / "cmdline.txt").read_text()
    original = (fake_card / "cmdline.txt.phaser-orig").read_text()

    sed_expr = re.search(r"sed -i '([^']+)'", FIRSTRUN.read_text(encoding="utf-8"))
    assert sed_expr, "could not find the disarm sed in firstrun.sh"

    tmp = fake_card / "roundtrip.txt"
    tmp.write_text(patched, encoding="utf-8")
    r = subprocess.run(["sed", "-i", sed_expr.group(1), str(tmp)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert tmp.read_text().strip() == original.strip(), (
        "firstrun.sh's disarm does not restore the cmdline prep_sdcard.py wrote"
    )


@pytest.mark.parametrize("bad", ["phaser_01", "-bad", "bad-", "", "a" * 64])
def test_invalid_hostnames_rejected(bad):
    prep = _load_prep()
    with pytest.raises(prep.Failure):
        prep.validate_hostname(bad)


@pytest.mark.parametrize("bad", ["999.1.1.1", "127.0.0.1", "192.168.7.2/32",
                                 "not-an-ip", "0.0.0.0"])
def test_invalid_addresses_rejected(bad):
    prep = _load_prep()
    with pytest.raises(prep.Failure):
        prep.validate_cidr(bad)


def test_bare_address_becomes_slash_24():
    prep = _load_prep()
    assert prep.validate_cidr("192.168.7.55") == "192.168.7.55/24"


def test_refuses_a_directory_that_is_not_a_boot_partition(tmp_path):
    """Writing to the wrong mounted volume is the expensive mistake here."""
    prep = _load_prep()
    (tmp_path / "config.txt").write_text("x")   # only one of the two markers
    with pytest.raises(prep.Failure, match="does not look like"):
        prep.main(["--boot", str(tmp_path)])


def test_dry_run_writes_nothing(fake_card):
    prep = _load_prep()
    before = sorted(p.name for p in fake_card.iterdir())
    prep.main(["--boot", str(fake_card), "--dry-run"])
    assert sorted(p.name for p in fake_card.iterdir()) == before


def test_boot_mount_choice_reaches_the_cmdline(fake_card):
    """bookworm moved the FAT mount; a wrong path means the hook never runs."""
    prep = _load_prep()
    prep.main(["--boot", str(fake_card), "--ip", "none",
               "--boot-mount", "/boot/firmware"])
    assert "systemd.run=/boot/firmware/firstrun.sh" in \
        (fake_card / "cmdline.txt").read_text()


def test_autoprovision_revokes_its_own_sudo_grant():
    """The unattended path grants analog passwordless root; it must hand it back.

    ExecStopPost as well as ExecStartPost, so a provision that dies partway
    does not leave a kit with NOPASSWD sudo and the stock analog/analog
    password sitting on a workshop LAN.
    """
    text = FIRSTRUN.read_text(encoding="utf-8")
    assert "NOPASSWD" in text, "expected the unattended path to grant sudo"
    sudoers = "/etc/sudoers.d/010-phaser-autoprovision"
    assert f"ExecStartPost=/bin/rm -f {sudoers}" in text
    assert f"ExecStopPost=/bin/rm -f {sudoers}" in text


def test_netalias_is_an_alias_not_a_static_config():
    """Adding an address must not rewrite the network stack's own config.

    An alias works on dhcpcd, NetworkManager and systemd-networkd alike, and
    survives Kuiper changing stacks underneath us. Editing dhcpcd.conf or
    NetworkManager profiles would not.
    """
    raw = (PI_DIR / "phaser-netalias").read_text(encoding="utf-8")
    # Strip comments: the script *names* these stacks to explain why it does
    # not touch them, and matching prose would make this test meaningless.
    code = "\n".join(ln for ln in raw.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "ip addr add" in code
    for forbidden in ("dhcpcd.conf", "NetworkManager", "systemd/network",
                      "nmcli", "/etc/network/interfaces"):
        assert forbidden not in code, \
            f"netalias must not touch {forbidden}; it adds an alias instead"
