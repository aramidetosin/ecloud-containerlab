#!/usr/bin/env python3
"""Patch vrnetlab nvidia/cumulus-vx/docker/launch.py to APPLY /config/startup-config.cfg
at first boot (the way vJunos/cEOS kinds do). containerlab's VRNode already delivers the
node's `startup-config:` file to <labdir>/<node>/config/startup-config.cfg (bind-mounted at
/config); the stock Cumulus launcher just never consumed it. This adds that step:
after switchd is active, SSH into the VM (127.0.0.1, known creds), replay the nv-set
lines, `nv config apply -y`, then mark running. Idempotent: skipped if no file, and a
marker prevents re-apply on overlay reuse."""
import sys, re
p = sys.argv[1]
t = open(p).read()

HOOK = """        if self._bootstrap_done:
            if not self._switchd_is_ready():
                self.spins += 1
                return
            self.running = True"""
NEW = """        if self._bootstrap_done:
            if not self._switchd_is_ready():
                self.spins += 1
                return
            # ecloud: apply the containerlab startup-config (if provided) BEFORE declaring running
            try:
                self._apply_startup_config()
            except Exception as exc:  # never block boot on a config problem; log it
                self.logger.error("startup-config apply failed: %s", exc)
            self.running = True"""
assert HOOK in t, "hook point not found - launcher changed upstream?"
t = t.replace(HOOK, NEW, 1)

METHOD = '''
    # ── ecloud addition: startup-config apply over SSH ─────────────────────────
    STARTUP_CONFIG_FILE = "/config/startup-config.cfg"
    STARTUP_APPLIED_MARK = "/config/.startup-config.applied"

    def _apply_startup_config(self):
        import os, subprocess
        if not os.path.exists(self.STARTUP_CONFIG_FILE):
            self.logger.info("No startup-config at %s, skipping", self.STARTUP_CONFIG_FILE)
            return
        if os.path.exists(self.STARTUP_APPLIED_MARK):
            self.logger.info("startup-config already applied (marker present), skipping")
            return
        self.logger.info("Applying startup-config %s over SSH ...", self.STARTUP_CONFIG_FILE)
        ssh = ["sshpass", "-p", "Clab123!", "ssh",
               "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
               "-o", "ConnectTimeout=10", "-o", "LogLevel=ERROR", "cumulus@127.0.0.1"]
        # 1) wait for the VM's sshd (switchd is up, but sshd may lag a few seconds)
        for i in range(30):
            r = subprocess.run(ssh + ["true"], capture_output=True, timeout=20)
            if r.returncode == 0:
                break
            import time as _t; _t.sleep(3)
        else:
            raise RuntimeError("VM sshd not reachable for startup-config apply")
        # 2) replay the nv-set lines (file is plain lines; skip blanks/comments)
        with open(self.STARTUP_CONFIG_FILE) as f:
            body = "".join(l for l in f if l.strip() and not l.lstrip().startswith("#"))
        r = subprocess.run(ssh + ["bash -s"], input=body.encode(), capture_output=True, timeout=600)
        self.logger.info("nv-set replay rc=%s stderr=%s", r.returncode, r.stderr.decode()[-300:])
        # 3) apply
        r = subprocess.run(ssh + ["nv config apply -y 2>&1 | tail -3"], capture_output=True, timeout=600)
        out = r.stdout.decode()
        self.logger.info("nv config apply: %s", out.strip()[-300:])
        if "applied" in out:
            open(self.STARTUP_APPLIED_MARK, "w").write("ok\\n")
            self.logger.info("startup-config applied and marked")
            # ecloud: passwordless operator SSH from first boot. If the deploy bind-mounted an
            # authorized_keys file (bootstrap/hosts -> /bootstrap-keys), install it for cumulus
            # over the same SSH channel. The VM disk persists, so this survives the MH reboot.
            akf = "/bootstrap-keys/authorized_keys"
            if os.path.isfile(akf):
                pub = open(akf).read().strip()
                kr = subprocess.run(ssh + ["mkdir -p ~/.ssh && chmod 700 ~/.ssh && grep -qF '" + pub + "' ~/.ssh/authorized_keys 2>/dev/null || echo '" + pub + "' >> ~/.ssh/authorized_keys; chmod 600 ~/.ssh/authorized_keys"], capture_output=True, timeout=60)
                self.logger.info("operator authorized_keys install rc=%s", kr.returncode)
            # ecloud: the containerlab VSCode extension SSHes as 'admin' by default; give the
            # switches the same lab convention as the hosts (admin/admin + the operator key).
            mk = ("sudo useradd -m -s /bin/bash admin 2>/dev/null; "
                  # chpasswd is vetoed by Cumulus password-quality PAM; write the crypt hash directly
                  "sudo usermod -p '$6$ecloudlab$Uf7ZjAwEVT13Doo.zoNI7OGx0zIUzy0SkiKeiiRIuKQxAnZk7TY39el3vMb0zcxQ4YblqLJscirCFqe3HkTU./' admin; "
                  "sudo usermod -aG sudo admin 2>/dev/null; "
                  "if [ -f ~/.ssh/authorized_keys ]; then sudo mkdir -p /home/admin/.ssh && "
                  "sudo cp ~/.ssh/authorized_keys /home/admin/.ssh/authorized_keys && "
                  "sudo chown -R admin:admin /home/admin/.ssh && sudo chmod 700 /home/admin/.ssh && "
                  "sudo chmod 600 /home/admin/.ssh/authorized_keys; fi")
            ar = subprocess.run(ssh + [mk], capture_output=True, timeout=60)
            self.logger.info("admin account setup rc=%s", ar.returncode)
        else:
            self.logger.warning("nv config apply did not report 'applied': %s", out[-200:])
            return
        # 4) VX EVPN-MH quirk: ES host bonds stay carrier-down after `nv config apply` until the VM
        #    REBOOTS (documented in the EVE lab; identical here). If the config has multihoming, do the
        #    reboot as part of first boot, wait for switchd, then apply `evpn mh redirect-off` (vtysh-
        #    only, does not persist in NVUE, must be re-applied per boot) and write memory.
        if "evpn multihoming" in body:
            import time as _t2
            self.logger.info("EVPN-MH config detected: rebooting VM once so the ES bonds converge ...")
            subprocess.run(ssh + ["sudo systemctl reboot"], capture_output=True, timeout=30)
            _t2.sleep(45)
            for i in range(60):
                r = subprocess.run(ssh + ["systemctl is-active switchd"], capture_output=True, timeout=20)
                if r.returncode == 0 and b"active" in r.stdout and b"inactive" not in r.stdout:
                    break
                _t2.sleep(5)
            else:
                self.logger.warning("switchd not active after post-config reboot (continuing)")
            r = subprocess.run(ssh + ["sudo vtysh -c 'configure terminal' -c 'evpn mh redirect-off' -c 'end' -c 'write memory' 2>&1 | tail -1"], capture_output=True, timeout=60)
            self.logger.info("post-reboot: evpn mh redirect-off applied (%s)", r.stdout.decode().strip()[-80:])

    def bootstrap_spin(self):'''
assert "\n    def bootstrap_spin(self):" in t
t = t.replace("\n    def bootstrap_spin(self):", METHOD, 1)

# ---- fix 2: upstream bug in _switchd_is_ready() ----
# vrnetlab's scrapli "telnetlib-compatible" wrapper returns STR regex matches, but the stock code
# compared match.group(0) to the BYTES literal b"active" -> always False -> the VM never reached
# running/healthy and no post-boot hook could ever fire. Also anchor to a result LINE so the echoed
# command text "is-active" cannot satisfy the match (verified against real serial bytes).
OLD_RDY = '''            (_, match, _) = self.tn.expect([b"active", b"inactive", b"failed"], 3)
            if match:
                return match.group(0) == b"active"'''
NEW_RDY = '''            # ecloud fix: str-vs-bytes + line-anchored patterns (see cvx_startup_config.patch.py)
            (_, match, _) = self.tn.expect([rb"[\\r\\n]inactive[\\r\\n]", rb"[\\r\\n]failed[\\r\\n]", rb"[\\r\\n]active[\\r\\n]"], 3)
            if match:
                m0 = match.group(0); m0 = m0.decode() if isinstance(m0, bytes) else m0
                return m0.strip() == "active"'''
assert OLD_RDY in t, "_switchd_is_ready anchor not found - launcher changed upstream?"
t = t.replace(OLD_RDY, NEW_RDY, 1)

# ---- fix 3: overlay-reuse login ----
# On a REUSED persistent overlay the password was already changed to Clab123! on a previous boot,
# but the stock first-boot always sends the factory 'cumulus' password first -> "Login incorrect" ->
# 60s login timeout -> Step 3 hangs on a dead console -> node never becomes healthy and no config
# applies. (100% of ripple-recreated nodes hit this.) Fix: log in with the NEW password when the
# overlay is reused, and only take the factory/expired-password path on a truly fresh disk.
OLD_LOGIN = '''        # Step 1 — log in
        self.wait_write(VM_USER, None)
        self.wait_write(VM_PASS, "Password:")'''
NEW_LOGIN = '''        # Step 1 — log in. ecloud fix: a reused overlay already has NEW_PASS; sending the factory
        # password there fails ("Login incorrect") and wedges the bootstrap. Detect reuse via the
        # marker the previous boot left in /config, and pick the right password.
        import os as _os
        _reused = _os.path.exists("/config/.startup-config.applied") or _os.path.exists("/config/.first-boot.done")
        self.wait_write(VM_USER, None)
        self.wait_write(NEW_PASS if _reused else VM_PASS, "Password:")'''
assert OLD_LOGIN in t, "first-boot login anchor not found"
t = t.replace(OLD_LOGIN, NEW_LOGIN, 1)
# leave a marker once first-boot completes, so the next boot on this overlay knows the password state
OLD_DONE = '''        self.logger.info("First-boot setup complete")'''
NEW_DONE = '''        try:
            open("/config/.first-boot.done", "w").write("ok\\n")
        except Exception:
            pass
        self.logger.info("First-boot setup complete")'''
assert OLD_DONE in t, "first-boot-complete anchor not found"
t = t.replace(OLD_DONE, NEW_DONE, 1)

open(p, "w").write(t)
print("patched:", p, "(startup-config hook + EVPN-MH reboot + switchd-ready fix + overlay-reuse login)")
