"""A model of how an EPICS record turns a value into alarm status/severity.

THIS IS NOT THE ALARM SOURCE. The real alarms come from the IOC. This module exists so that the
simulator (dry run) and its tests can state which native alarm a scenario is EXPECTED to raise, and so
a test run can compare what the IOC reported with what the record configuration implies.

Rules (aiRecord/biRecord/mbbiRecord of EPICS Base 7): for analog records the limits are checked in the
order HIHI, LOLO, HIGH, LOW; a limit only counts if its severity (HHSV, LLSV, HSV, LSV) is not NO_ALARM;
an active limit is only left when the value has moved HYST beyond it. Binary/enum records use the
severity configured for the current state (ZSV/OSV, ZRSV...). NO_ALARM = 0, MINOR = 1, MAJOR = 2.
"""

SEV = {"NO_ALARM": 0, "MINOR": 1, "MAJOR": 2, "INVALID": 3}
SEV_NAME = {v: k for k, v in SEV.items()}


class AlarmState:
    """Per-record memory (the last alarm limit, needed for hysteresis)."""

    def __init__(self):
        self.last = None  # "HIHI" | "HIGH" | "LOLO" | "LOW" | None


def _sev(rec, key):
    return SEV.get(rec.fields.get(key, "NO_ALARM"), 0)


def evaluate(rec, value, state=None):
    """Return (status_name, severity_int) the record would report for this value."""
    state = state or AlarmState()
    if rec.is_analog:
        hyst = rec.num("HYST", 0.0)
        lim = {k: rec.num(k) for k in ("HIHI", "HIGH", "LOW", "LOLO")}
        sv = {"HIHI": _sev(rec, "HHSV"), "HIGH": _sev(rec, "HSV"), "LOW": _sev(rec, "LSV"), "LOLO": _sev(rec, "LLSV")}
        last = state.last

        def over(name):
            return lim[name] is not None and sv[name] > 0 and (value >= lim[name] or (last == name and value >= lim[name] - hyst))

        def under(name):
            return lim[name] is not None and sv[name] > 0 and (value <= lim[name] or (last == name and value <= lim[name] + hyst))

        if over("HIHI"):
            state.last = "HIHI"
        elif under("LOLO"):
            state.last = "LOLO"
        elif over("HIGH"):
            state.last = "HIGH"
        elif under("LOW"):
            state.last = "LOW"
        else:
            state.last = None
        if state.last is None:
            return "NO_ALARM", 0
        return state.last, sv[state.last]
    if rec.is_binary:
        idx = 1 if value >= 0.5 else 0
        sev = _sev(rec, ("ZSV", "OSV")[idx])
        return ("STATE", sev) if sev else ("NO_ALARM", 0)
    if rec.is_enum:
        names = ["ZR", "ON", "TW", "TH", "FR", "FV", "SX", "SV", "EI", "NI", "TE", "EL", "TV", "TT", "FT", "FF"]
        idx = int(round(value))
        if 0 <= idx < len(names):
            sev = _sev(rec, names[idx] + "SV")
            if sev:
                return "STATE", sev
        return "NO_ALARM", 0
    return "NO_ALARM", 0
