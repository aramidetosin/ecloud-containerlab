#!/usr/bin/env python3
"""Patch vrnetlab paloalto/pan/docker/launch.py for PAN-OS 12.x first boot.
Verified against a live PA-VM 12.1.2 boot: after the forced admin password change
("Password changed"), PAN-OS 12 does NOT emit the CLI prompt on the serial console until it
receives a keystroke, so the stock launcher (which only acts on a matched prompt) deadlocks
waiting for 'admin@PA-VM>' forever (10+ min observed). Fix: after confirming the new password,
send a newline to elicit the prompt; also match the prompt liberally (admin@<anything>>) so a
pre-set hostname / HA-state prompt like 'admin@fw-pri(active-primary)>' is accepted too."""
import sys
p = sys.argv[1]
t = open(p).read()

OLD_PAT = '                b"admin@PA-VM>",'
NEW_PAT = '                b"admin@[^>\\r\\n]*>",   # ecloud fix: PAN-OS 12 / hostname / HA-state prompts'
assert OLD_PAT in t, "prompt pattern anchor not found"
t = t.replace(OLD_PAT, NEW_PAT, 1)

OLD_CONFIRM = '''            elif ridx == 7:
                self.logger.debug(f"confirming 'new' password '{self.password}'")
                self.wait_write(self.password, wait=None)'''
NEW_CONFIRM = '''            elif ridx == 7:
                self.logger.debug(f"confirming 'new' password '{self.password}'")
                self.wait_write(self.password, wait=None)
                # ecloud fix: PAN-OS 12 shows no prompt after "Password changed" until a keystroke
                # arrives; nudge the console so the admin@...> prompt is emitted and matched next spin.
                time.sleep(3)
                self.wait_write("", wait=None)
                time.sleep(2)
                self.wait_write("", wait=None)'''
assert OLD_CONFIRM in t, "confirm-password anchor not found"
t = t.replace(OLD_CONFIRM, NEW_CONFIRM, 1)

# Fix 3: the auto-commit wait loop only sends "show jobs processed" when the console buffer is
# EMPTY (res == b""). PAN-OS 12 keeps echoing prompt/command fragments on the serial console
# ("admin@PA-VM> set admin@PA-VM> set cli ..."), so the buffer is never empty, the kick is never
# sent, FIN/PEND never appear, and the loop spins forever (observed 2+ min stall). Fix: on any
# no-match, sleep and send the kick unconditionally.
# Root cause: the scrapli wrapper's expect() calls a BLOCKING channel.read() (timeout_transport 3600s);
# its 1s "timeout" is only checked after read() returns. Once PAN-OS 12 sits quietly at the prompt in
# scripting-mode, read() never returns -> the loop's expect blocks for an hour -> no kick is ever sent.
# So the kick must be sent BEFORE each expect, guaranteeing output that unblocks the read.
OLD_LOOP = '''        self.wait_write("", None)
        while True:
            (ridx, match, res) = self.tn.expect([b"FIN", b"PEND"], 1)
            if match:
                if ridx == 0:  # login
                    self.logger.debug("auto commit complete, begin configuration")
                    break
                elif ridx == 1:
                    self.logger.debug("auto commit still pending, sleeping...")
                    time.sleep(10)
                    self.wait_write("show jobs processed", wait=None)
            elif res == b"":
                time.sleep(10)
                self.wait_write("show jobs processed", wait=None)'''
NEW_LOOP = '''        self.wait_write("", None)
        while True:
            # ecloud fix: send the kick FIRST so the (blocking) console read always has output to return
            self.wait_write("show jobs processed", wait=None)
            (ridx, match, res) = self.tn.expect([b"FIN", b"PEND"], 15)
            if match:
                if ridx == 0:  # login
                    self.logger.debug("auto commit complete, begin configuration")
                    break
                elif ridx == 1:
                    self.logger.debug("auto commit still pending, sleeping...")
                    time.sleep(10)
            else:
                time.sleep(10)'''
assert OLD_LOOP in t, "auto-commit loop anchor not found"
t = t.replace(OLD_LOOP, NEW_LOOP, 1)

# Fix 4: XML startup-config via the API. Replaying a `show config` set-format export line by line
# fails on PAN-OS (lines are validated as they are entered and the export is not in dependency
# order: zones/HA/BGP peers reference interfaces / peer-AS defined later -> "constraints failed",
# "not a valid reference", "Invalid syntax" on multi-word values -> commit fails -> EMPTY config).
# A full <config> XML tree loaded via the API (import -> load config from -> commit) is validated
# as a whole and has no ordering problem. If /config/startup-config.xml exists, use that path
# (reusing the launcher's own import/commit/poll helpers) instead of the set-line replay.
OLD_SC = '''    def startup_config(self):
        """Load additional config provided by user."""

        if not os.path.exists(STARTUP_CONFIG_FILE):'''
NEW_SC = '''    STARTUP_CONFIG_XML = "/config/startup-config.xml"

    def _api_op(self, api_key, cmd, timeout=120):
        return requests.post(f"https://127.0.0.1/api/?type=op&key={api_key}", data={"cmd": cmd}, verify=False, timeout=timeout).text

    def _api_wait_job(self, api_key, job, what, tries=60):
        for attempt in range(tries):
            time.sleep(10)
            root = ET.fromstring(self.check_config_commit_status(api_key, job))
            st = root.find(".//job/status"); res = root.find(".//job/result"); det = root.find(".//job/details")
            if st is not None and st.text == "FIN":
                r = res.text if res is not None else "?"
                self.logger.info("%s job %s finished: %s %s", what, job, r,
                                 ("".join(det.itertext())[:300] if det is not None else ""))
                return r
        self.logger.warning("%s job %s did not finish", what, job)
        return None

    def _enable_jumbo_and_reboot(self, api_key):
        """PAN-OS validates interface MTUs against the ACTIVE jumbo-frame mode, and jumbo-frame
        mode only takes effect after a reboot. A fresh PA-VM is jumbo=off, so a config with
        9216 interfaces fails validation ("device is not in jumbo-frame mode but interface
        ethernet1/1 mtu is greater than 1500") until the box has rebooted in jumbo mode.
        Phase 1 of the XML bootstrap: set jumbo, commit, reboot, wait for the API to return."""
        self.logger.info("Phase 1: enabling jumbo-frame mode (mtu 9216), committing, rebooting")
        # The config element <deviceconfig><setting><jumbo-frame><mtu> only sets the SIZE. The MODE is an
        # OPERATIONAL toggle: `set system setting jumbo-frame on` (returns "Device is now in Jumbo-Frame
        # mode, please reboot device"). Verified: config-only + reboot (hard or graceful) left mode=off;
        # the op command flipped it to on immediately (pending reboot). Do both, then reboot.
        r = self._api_op(api_key, "<set><system><setting><jumbo-frame>on</jumbo-frame></setting></system></set>", timeout=60)
        self.logger.info("op set jumbo-frame on: %s", re.sub(r"<[^>]+>", " ", r).strip()[:120])
        requests.post(f"https://127.0.0.1/api/?type=config&action=set&key={api_key}",
                      data={"xpath": "/config/devices/entry[@name='localhost.localdomain']/deviceconfig/setting/jumbo-frame",
                            "element": "<mtu>9216</mtu>"}, verify=False, timeout=60)
        job = self.panos_commit_configuration(api_key, description="ecloud jumbo-frame")
        self._api_wait_job(api_key, job, "jumbo commit")
        # The jumbo mode only becomes ACTIVE when the dataplane re-initialises on a GRACEFUL restart.
        # A QEMU hard reset right after the commit leaves the config element persisted but the mode
        # off (verified: config 9216, uptime 4 min, mode off). And <request><restart><system/> over the
        # API is silently ignored WHILE the first-boot auto-commit is still running, but works once
        # the box is settled (verified: went down 5 s after the call). So: wait for auto-commit to be
        # clear, then graceful API restart; fall back to QEMU system_reset only if it does not go down.
        self._wait_autocommit(api_key)
        time.sleep(15)
        self.logger.info("Phase 1: graceful restart via the API (post auto-commit)")
        self._api_op(api_key, "<request><restart><system></system></restart></request>", timeout=30)
        went_down = False
        for attempt in range(24):
            time.sleep(5)
            try:
                requests.get("https://127.0.0.1/api/?type=keygen&user=x&password=x", verify=False, timeout=5)
            except Exception:
                went_down = True
                break
        if not went_down:
            self.logger.warning("API restart did not take the box down; falling back to QEMU system_reset")
            try:
                self.qm.write(b"system_reset\\r\\n")
            except Exception as exc:
                self.logger.warning("qemu monitor system_reset failed: %s", exc)
            for attempt in range(60):
                time.sleep(5)
                try:
                    requests.get("https://127.0.0.1/api/?type=keygen&user=x&password=x", verify=False, timeout=5)
                except Exception:
                    went_down = True
                    break
        self.logger.info("box down=%s; waiting for the API to come back (PAN-OS 12: several minutes)", went_down)
        time.sleep(120)
        for attempt in range(90):
            try:
                k = self.panos_api_login()
                if k:
                    r = self._api_op(k, "<show><system><info></info></system></show>", timeout=30)
                    if "<hostname>" in r:
                        up = re.search(r"<uptime>([^<]+)", r)
                        self.logger.info("PAN back after jumbo reboot (uptime %s)", up.group(1) if up else "?")
                        # after a reboot PAN-OS runs its own auto-commit and REJECTS user commits
                        # ("Commit job was not queued since auto-commit not yet finished") until it is
                        # done. Wait for the AutoCom job to reach FIN before returning.
                        self._wait_autocommit(k)
                        return k
            except Exception:
                pass
            time.sleep(10)
        raise RuntimeError("PAN did not come back after the jumbo-frame reboot")

    def _wait_autocommit(self, api_key, tries=60):
        for attempt in range(tries):
            try:
                j = self._api_op(api_key, "<show><jobs><all></all></jobs></show>", timeout=30)
                # any AutoCom job that is not FIN => still running
                jobs = re.findall(r"<job>(.*?)</job>", j, re.S)
                ac = [x for x in jobs if "<type>AutoCom</type>" in x]
                if ac and all("<status>FIN</status>" in x for x in ac):
                    self.logger.info("post-reboot auto-commit finished")
                    return
                if not ac and attempt > 3:
                    self.logger.info("no AutoCom job listed; proceeding")
                    return
            except Exception:
                pass
            time.sleep(10)
        self.logger.warning("auto-commit wait timed out; proceeding anyway")

    def startup_config_xml(self):
        """ecloud: load a full <config> XML tree via the API (import -> load -> commit -> poll).
        If the config uses jumbo frames, do the jumbo-enable + reboot phase first."""
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.logger.info(f"Applying XML startup config {self.STARTUP_CONFIG_XML} via the API")
        api_key = self.panos_api_login()
        with open(self.STARTUP_CONFIG_XML, "rb") as f:
            blob = f.read()
        if b"<jumbo-frame>" in blob:
            api_key = self._enable_jumbo_and_reboot(api_key)
        fname = os.path.basename(self.STARTUP_CONFIG_XML)
        # 1) import as a configuration file
        url = f"https://127.0.0.1/api/?type=import&category=configuration&key={api_key}"
        with open(self.STARTUP_CONFIG_XML, "rb") as f:
            resp = requests.post(url, files={"file": (fname, f, "application/xml")}, verify=False, timeout=120)
        self.logger.info("import configuration: %s", ET.fromstring(resp.content).get("status"))
        # 2) merge it into the candidate section by section (PARTIAL load, mode merge). A whole-tree
        #    `load config from` replaces the candidate and hides validation reasons; partial merge onto
        #    the box's baseline validated OK and gave real error text during debugging.
        dev = "/config/devices/entry[@name='localhost.localdomain']"
        for from_x, to_x in [(f"{dev}/deviceconfig", f"{dev}/deviceconfig"), (f"{dev}/network", f"{dev}/network"),
                             (f"{dev}/vsys", f"{dev}/vsys"), ("/config/shared", "/config/shared")]:
            cmd = (f"<load><config><partial><mode>merge</mode><from-xpath>{from_x}</from-xpath>"
                   f"<to-xpath>{to_x}</to-xpath><from>{fname}</from></partial></config></load>")
            resp = requests.post(f"https://127.0.0.1/api/?type=op&key={api_key}", data={"cmd": cmd}, verify=False, timeout=120)
            self.logger.info("partial load %s: %s", to_x.split("/")[-1], ET.fromstring(resp.content).get("status"))
        # validate first so a bad config is reported with its reason, not a bare FAIL
        v = self._api_op(api_key, "<validate><full></full></validate>", timeout=120)
        vj = re.search(r"<job>(\d+)</job>", v)
        if vj:
            vr = self._api_wait_job(api_key, vj.group(1), "validate")
            if vr != "OK":
                self.logger.error("startup-config did NOT validate; skipping commit")
                return
        # 3) commit + poll (reuse the launcher's helpers)
        job = self.panos_commit_configuration(api_key, description="ecloud startup-config.xml")
        self.logger.info("commit job %s submitted", job)
        for attempt in range(60):
            time.sleep(10)
            root = ET.fromstring(self.check_config_commit_status(api_key, job))
            st = root.find(".//job/status"); pr = root.find(".//job/progress"); res = root.find(".//job/result")
            st = st.text if st is not None else None
            if st == "FIN":
                self.logger.info("XML startup config commit finished: result=%s", res.text if res is not None else "?")
                det = root.find(".//job/details")
                if det is not None and det.text:
                    self.logger.info("commit details: %s", "".join(det.itertext())[:400])
                return
            self.logger.info("commit status %s progress %s", st, pr.text if pr is not None else "?")
        self.logger.warning("XML startup config commit did not finish in time")

    def startup_config(self):
        """Load additional config provided by user."""

        # ecloud: containerlab always delivers the startup-config as /config/startup-config.cfg.
        # If that file is an XML <config> tree, load it via the API path; else fall through to
        # the stock set-line replay.
        if os.path.exists(STARTUP_CONFIG_FILE):
            with open(STARTUP_CONFIG_FILE, "rb") as f:
                head = f.read(200)
            if b"<config" in head:
                import shutil
                shutil.copy(STARTUP_CONFIG_FILE, self.STARTUP_CONFIG_XML)
                try:
                    self.startup_config_xml()
                except Exception as exc:
                    self.logger.error("XML startup config failed: %s", exc)
                return

        if not os.path.exists(STARTUP_CONFIG_FILE):'''
assert OLD_SC in t, "startup_config anchor not found"
t = t.replace(OLD_SC, NEW_SC, 1)

# Fix 5: bootstrap_spin deadlocks if a login-flow prompt is split across expect windows.
# telnetlib's expect() DISCARDS unmatched data on timeout, so a prompt that arrives split across
# two 1-second windows ("En" in one read, "ter old password : " in the next) can NEVER match.
# PAN-OS then waits silently for input forever, and the trickle of kernel messages keeps resetting
# self.spins, so the 300-spin restart never fires either. Verified live: BOTH A/A units deadlocked
# at "Enter old password :" during a cold-deploy boot-storm (23+ min silent). Fix: track no-MATCH
# progress separately and nudge the console with a bare newline every 20 stalled spins; the prompt
# re-emits as a single burst and matches on the next spin. A stray newline is benign at every stage
# of the login flow (empty input just re-prompts).
OLD_SPIN = '''        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        self.spins += 1'''
NEW_SPIN = '''        # no match, if we saw some output from the router it's probably
        # booting, so let's give it some more time
        if res != b"":
            self.logger.trace("OUTPUT: %s" % res.decode())
            # reset spins if we saw some output
            self.spins = 0

        # ecloud fix: recover from a prompt split across expect windows (telnetlib discards
        # unmatched data on timeout, so the pattern can never match once fragmented). Nudge the
        # console after 20 spins without a pattern match; the prompt re-emits in one burst.
        if match:
            self._stall_spins = 0
        else:
            self._stall_spins = getattr(self, "_stall_spins", 0) + 1
            if self._stall_spins >= 20:
                self.logger.debug("no prompt match for %d spins; nudging the serial console", self._stall_spins)
                self.wait_write("", wait=None)
                self._stall_spins = 0

        self.spins += 1'''
assert OLD_SPIN in t, "bootstrap_spin no-match anchor not found"
t = t.replace(OLD_SPIN, NEW_SPIN, 1)

open(p, "w").write(t)
print("patched:", p, "(PAN-OS 12: prompt nudge + liberal prompt + kick-first auto-commit + XML startup-config via API + stall-nudge)")
