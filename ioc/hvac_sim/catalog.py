"""PV catalog read from EPICS .db files.

The .db files are the single source of truth for the PV inventory: names, record types, units, display
ranges, alarm limits and enum/binary state texts. The simulator only generates values for PVs that exist
here, and the monitoring application's inventory is exported from the same files (export_inventory.py),
so nothing is typed in twice.

Only the small part of the .db syntax the test databases use is parsed:
    record(<type>, "<name>") { field(<NAME>, "<value>") ... }
Macros ($(P)) are not expanded; the HVAC databases do not use them.
"""
import re
from dataclasses import dataclass, field

_RECORD = re.compile(r'record\(\s*(\w+)\s*,\s*"([^"]+)"\s*\)\s*\{(.*?)\n\}', re.S)
_FIELD = re.compile(r'field\(\s*(\w+)\s*,\s*"([^"]*)"\s*\)')
_COMMENT = re.compile(r'^\s*#.*$', re.M)


@dataclass
class Record:
    name: str
    rtype: str
    fields: dict = field(default_factory=dict)
    source: str = ""

    # -- helpers -----------------------------------------------------------------------------
    def num(self, key, default=None):
        v = self.fields.get(key)
        if v is None or v == "":
            return default
        try:
            return float(v)
        except ValueError:
            return default

    @property
    def initial(self):
        return self.num("VAL", 0.0)

    @property
    def egu(self):
        return self.fields.get("EGU", "")

    @property
    def prec(self):
        return int(self.num("PREC", 2) or 0)

    @property
    def is_analog(self):
        return self.rtype in ("ai", "ao", "calc")

    @property
    def is_binary(self):
        return self.rtype in ("bi", "bo")

    @property
    def is_enum(self):
        return self.rtype in ("mbbi", "mbbo")

    @property
    def states(self):
        """State texts of a bi/bo (2) or mbbi/mbbo (up to 16) record, in order of state number."""
        if self.is_binary:
            return [self.fields.get("ZNAM", ""), self.fields.get("ONAM", "")]
        if self.is_enum:
            names = ["ZR", "ON", "TW", "TH", "FR", "FV", "SX", "SV", "EI", "NI", "TE", "EL", "TV", "TT", "FT", "FF"]
            out = []
            for n in names:
                t = self.fields.get(n + "ST")
                if t is None:
                    break
                out.append(t)
            return out
        return []

    @property
    def writable_range(self):
        """(low, high) display range of an analog record, or None."""
        lo, hi = self.num("LOPR"), self.num("HOPR")
        if lo is None or hi is None or hi <= lo:
            return None
        return lo, hi

    @property
    def equipment(self):
        """'AHU1' for 'HVAC:AHU1:SupplyAirTemp' (the second name part), else ''."""
        parts = self.name.split(":")
        return parts[1] if len(parts) >= 3 else ""

    @property
    def point(self):
        """'SupplyAirTemp' for 'HVAC:AHU1:SupplyAirTemp' (the last name part)."""
        return self.name.split(":")[-1]

    @property
    def prefix(self):
        parts = self.name.split(":")
        return parts[0] if len(parts) >= 3 else ""


class Catalog:
    def __init__(self):
        self.records = {}

    def add_file(self, path):
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        text = _COMMENT.sub("", text)
        for m in _RECORD.finditer(text):
            rtype, name, body = m.groups()
            if name in self.records:
                raise ValueError("record %s defined twice (%s and %s)" % (name, self.records[name].source, path))
            self.records[name] = Record(name, rtype, dict(_FIELD.findall(body)), path)
        return self

    @classmethod
    def load(cls, *paths):
        c = cls()
        for p in paths:
            c.add_file(p)
        return c

    def __len__(self):
        return len(self.records)

    def __iter__(self):
        return iter(self.records.values())

    def __getitem__(self, name):
        return self.records[name]

    def get(self, name):
        return self.records.get(name)

    def by_equipment(self):
        """{(prefix, equipment): {point: Record}}"""
        out = {}
        for r in self:
            if r.equipment:
                out.setdefault((r.prefix, r.equipment), {})[r.point] = r
        return out
