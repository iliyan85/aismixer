import re

# Same closed AIS talker whitelist as core/nmea_scanner.py and the root
# meta_cleaner.py: AI (Mobile), AB (Base), AD, AN (AtoN), AR (Receiving),
# AS (Limited Base), AT (Transmitting), AX (Repeater), BS (legacy Base).
# Kept as a local constant rather than an import so the standalone proxy
# runtime does not depend on the core package; tests cross-check this
# tuple against core.nmea_scanner.TALKERS to guard against drift.
SUPPORTED_TALKERS = ("AI", "AB", "AD", "AN", "AR", "AS", "AT", "AX", "BS")

_TALKER_PATTERN = "(?:" + "|".join(SUPPORTED_TALKERS) + ")"
_SENTENCE_RE = re.compile(rf'!{_TALKER_PATTERN}VD[MO],[^\r\n]*?\*[0-9A-F]{{2}}')


def extract_nmea_sentences(line):
    """
    Extract valid AIS VDM/VDO sentences for talkers in SUPPORTED_TALKERS
    from combined input lines. Sentences must contain a checksum marker in
    the form *HH (uppercase hex).
    """
    return _SENTENCE_RE.findall(line)
