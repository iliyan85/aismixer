import re


def extract_nmea_sentences(line):
    """
    Извлича валидни !AIVDM съобщения от комбинирани редове.
    Съобщението трябва да започва с !AIVDM и да съдържа валиден CRC (*hh).
    """
    pattern = r'!AIVDM,[^\r\n]*?\*[0-9A-F]{2}'
    return re.findall(pattern, line)
