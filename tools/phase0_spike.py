#!/usr/bin/env python3
"""Phase 0 spike: does streaming binary into ssh's stdin survive on Windows?

The whole one-connection tar design rests on one assumption: when ssh's stdin
is a file we are feeding it, the PASSWORD PROMPT still reaches the terminal
rather than reading our binary as the password. That is untestable without a
real password-auth Pi and a real Windows console, so it gets tested by hand,
once, here.

Run:  python phase0_spike.py <pi-host>          (Windows: use "python")
      python3 phase0_spike.py <pi-host>         (WSL/Linux/macOS)

Read-only on the Pi apart from one file in /tmp, which it deletes.
Expect several password prompts: Windows OpenSSH has no connection sharing.
"""

import hashlib
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

USER = "analog"
REMOTE_TMP = "/tmp/phaser_spike.tar"
results = []


def note(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"\n  ==> {name}: {'PASS' if ok else 'FAIL'}")
    if detail:
        for line in detail.splitlines():
            print(f"      {line}")


def make_tar(path, payload_mb=4):
    """A tar roughly the size of the real payload, with incompressible data so
    a truncated or CR/LF-mangled stream cannot accidentally still match."""
    blob = path.parent / "blob.bin"
    blob.write_bytes(os.urandom(payload_mb * 1024 * 1024))
    with tarfile.open(path, "w") as tf:
        tf.add(blob, arcname="blob.bin")
    blob.unlink()
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else input("Pi host or IP: ").strip()
    target = f"{USER}@{host}"
    ssh = "ssh"

    print("=" * 68)
    print(f"  Phase 0 spike against {target}")
    print(f"  platform: {sys.platform}  os.name: {os.name}  python: "
          f"{'.'.join(str(n) for n in sys.version_info[:3])}")
    print("=" * 68)
    print("\n  You will be asked for the Pi's password several times.")
    print("  That is the point: each prompt is a thing we need to see work.\n")

    tmp = Path(tempfile.mkdtemp(prefix="phaser-spike-"))
    try:
        tar_path = tmp / "payload.tar"
        want = make_tar(tar_path)
        size = tar_path.stat().st_size
        print(f"  Built {size} byte test tar, sha256 {want[:16]}...")

        # --- A: the load-bearing test -----------------------------------------
        print("\n--- A. push binary with stdin redirected (-T, no pty) ---")
        print("    If a password prompt appears now and works, the design holds.")
        with open(tar_path, "rb") as fh:
            a = subprocess.run(
                [ssh, "-T", "-o", "ConnectTimeout=15", target,
                 f"cat > {REMOTE_TMP}"],
                stdin=fh)
        if a.returncode != 0:
            note("A push with redirected stdin", False,
                 f"ssh exited {a.returncode}.\n"
                 "If it never prompted, ssh read the tar as the password.\n"
                 "=> fall back to staging-dir + one `scp -r .` for transport.")
        else:
            got = subprocess.run(
                [ssh, "-o", "ConnectTimeout=15", target,
                 f"sha256sum {REMOTE_TMP} | cut -d' ' -f1"],
                capture_output=True, text=True)
            remote = (got.stdout or "").strip()
            ok = remote == want
            note("A push with redirected stdin", ok,
                 f"local  {want}\nremote {remote}\n" +
                 ("bytes identical -- tar over ssh stdin is safe here"
                  if ok else
                  "MISMATCH: the stream was altered in flight.\n"
                  "A pty doing CR/LF translation is the usual cause; -T was "
                  "passed, so check for RequestTTY in your ssh config.\n"
                  "=> fall back to staging-dir + one `scp -r .`"))

        # --- B: does a host-key prompt eat the stream? ------------------------
        print("\n--- B. same push, but as a first-ever connection ---")
        print("    Uses a throwaway known_hosts; your real one is untouched.")
        kh = tmp / "known_hosts_empty"
        kh.write_text("")
        with open(tar_path, "rb") as fh:
            b = subprocess.run(
                [ssh, "-T", "-o", f"UserKnownHostsFile={kh}",
                 "-o", "StrictHostKeyChecking=ask", "-o", "ConnectTimeout=15",
                 target, f"cat > {REMOTE_TMP}"],
                stdin=fh)
        note("B first-connection fingerprint prompt", b.returncode == 0,
             "Prompt appeared and was answerable."
             if b.returncode == 0 else
             f"ssh exited {b.returncode}. If you never saw a fingerprint\n"
             "question, it was answered by our binary stream.\n"
             "=> probe-before-push ordering is mandatory (already planned),\n"
             "   and StrictHostKeyChecking=accept-new should be kept.")

        # --- C: sudo under a pty ----------------------------------------------
        print("\n--- C. sudo through ssh -t (ConPTY on Windows) ---")
        c = subprocess.run([ssh, "-t", "-o", "ConnectTimeout=15", target,
                            "sudo true && echo SUDO_OK"])
        note("C sudo prompt under ssh -t", c.returncode == 0,
             "The sudo password prompt renders and accepts input."
             if c.returncode == 0 else
             f"ssh exited {c.returncode}. Phase 4's sudoers drop-in removes\n"
             "this from the steady state entirely.")

        # --- D: the Pi's side ---------------------------------------------------
        print("\n--- D. Pi capabilities ---")
        d = subprocess.run(
            [ssh, "-o", "ConnectTimeout=15", target,
             "tar --version | head -1; df -h /home/analog | tail -1; "
             f"rm -f {REMOTE_TMP}"],
            capture_output=True, text=True)
        note("D tar and disk space", d.returncode == 0,
             (d.stdout or "").strip() or (d.stderr or "").strip())

    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 68)
    for name, ok, _ in results:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("=" * 68)
    critical = next((ok for name, ok, _ in results if name.startswith("A")), False)
    print("\n  VERDICT: tar-over-ssh-stdin is " +
          ("viable on this platform." if critical else
           "NOT viable here -- use staging-dir + one `scp -r .` instead."))
    return 0 if critical else 1


if __name__ == "__main__":
    sys.exit(main())
