import re


def extract_nmea_sentences(line):
    """
    Extract valid !AIVDM and !AIVDO sentences from combined input lines.
    Sentences must contain a checksum marker in the form *hh.
    """
    pattern = r'!AIVD[MO],[^\r\n]*?\*[0-9A-F]{2}'
    return re.findall(pattern, line)
