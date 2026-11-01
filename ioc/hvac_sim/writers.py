"""How the simulator hands values to the EPICS IOC.

The supplied HVAC .db has plain soft-channel records (ai / bi / mbbi, Passive, no device support, no
input link): nothing inside the IOC produces their values. The simulator therefore plays the part of
the field I/O and writes each record's VAL over Channel Access. The VAL field of these records
processes the record on a put (pp(TRUE)), so the IOC itself then evaluates the alarm limits and posts
the monitors that clients such as the monitoring application subscribe to.

Writers:
  caput    runs EPICS Base's `caput` for each changed value (no Python dependencies; uses the tools that
           are already installed with the IOC). One process per value: fine at 1-2 s intervals.
  pyepics  uses the pyepics package (pip install pyepics): one persistent connection, efficient.
  dry-run  writes nothing; keeps the values so tests and `--print` can show what would be sent.

Only the simulator uses these writers. The monitoring application never writes to the IOC.
"""
import subprocess
import sys
import time


class Writer:
    name = "base"

    def put(self, pv, value):
        raise NotImplementedError

    def close(self):
        pass


class DryRunWriter(Writer):
    name = "dry-run"

    def __init__(self, echo=False):
        self.values = {}
        self.echo = echo
        self.writes = 0

    def put(self, pv, value):
        self.values[pv] = value
        self.writes += 1
        if self.echo:
            print("%s = %s" % (pv, value))
        return True


class CaputWriter(Writer):
    name = "caput"

    def __init__(self, caput="caput", timeout=3.0, log=print):
        self.caput, self.timeout, self.log = caput, timeout, log
        self.failures = 0
        self._last_log = 0.0

    def put(self, pv, value):
        try:
            r = subprocess.run([self.caput, "-t", pv, _fmt(value)], capture_output=True, text=True, timeout=self.timeout)
            if r.returncode == 0:
                return True
            err = (r.stderr or r.stdout).strip()
        except FileNotFoundError:
            raise SystemExit("caput not found. Put EPICS Base's bin directory on PATH or use --caput <path>.")
        except subprocess.TimeoutExpired:
            err = "timeout (PV not found? is the IOC running and CA address list set?)"
        self.failures += 1
        now = time.time()
        if now - self._last_log > 10:  # do not flood the console while the IOC is down
            self._last_log = now
            self.log("caput %s failed: %s" % (pv, err))
        return False


class PyEpicsWriter(Writer):
    name = "pyepics"

    def __init__(self, log=print):
        try:
            import epics  # noqa: F401
        except ImportError:
            raise SystemExit("The pyepics writer needs: pip install pyepics   (or use --writer caput)")
        import epics
        self.epics, self.log, self.pvs = epics, log, {}
        self.failures = 0
        self._last_log = 0.0

    def put(self, pv, value):
        p = self.pvs.get(pv)
        if p is None:
            p = self.pvs[pv] = self.epics.PV(pv, auto_monitor=False, connection_timeout=1.0)
        ok = p.connected and p.put(value, wait=False, timeout=1.0) is not False
        if not ok:
            self.failures += 1
            now = time.time()
            if now - self._last_log > 10:
                self._last_log = now
                self.log("pyepics: %s is not connected (IOC down or CA address list wrong); will keep trying" % pv)
        return ok


def _fmt(value):
    if isinstance(value, float):
        return repr(round(value, 6))
    return str(value)


def make_writer(kind, caput="caput"):
    if kind == "caput":
        return CaputWriter(caput)
    if kind == "pyepics":
        return PyEpicsWriter()
    if kind in ("dry-run", "dry"):
        return DryRunWriter(echo=True)
    raise SystemExit("unknown writer %r (caput, pyepics, dry-run)" % kind)
