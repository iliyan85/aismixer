from meta_cleaner import extract_nmea_sentences


def _nmea_checksum(body):
    checksum = 0
    for character in body:
        checksum ^= ord(character)
    return f"{checksum:02X}"


def test_supported_aivdm_without_checksum_shape_is_not_extracted():
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0"

    assert extract_nmea_sentences(sentence) == []


def test_current_behavior_accepts_incorrect_checksum_without_rewriting_it():
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*00"
    body, supplied_checksum = sentence[1:].split("*")

    assert _nmea_checksum(body) != supplied_checksum
    # Characterization only: acceptance does not approve the final checksum policy.
    assert extract_nmea_sentences(sentence) == [sentence]


def test_correct_checksum_aivdm_is_extracted_unchanged():
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*31"
    body, supplied_checksum = sentence[1:].split("*")

    assert _nmea_checksum(body) == supplied_checksum
    assert extract_nmea_sentences(sentence) == [sentence]


def test_current_behavior_preserves_lowercase_checksum_text_unchanged():
    sentence = "!AIVDM,1,1,,B,payload,0*4b"
    body, supplied_checksum = sentence[1:].split("*")

    assert _nmea_checksum(body) == supplied_checksum.upper()
    # Characterization only: checksum case handling is not yet an approved contract.
    assert extract_nmea_sentences(sentence) == [sentence]


def test_unsupported_nmea_sentence_is_ignored():
    sentence = "!GPGGA,1,2,3*00"

    assert extract_nmea_sentences(sentence) == []


def test_current_behavior_aivdo_requires_include_vdo():
    sentence = "!AIVDO,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*33"

    assert extract_nmea_sentences(sentence, include_vdo=False) == []
    assert extract_nmea_sentences(sentence, include_vdo=True) == [sentence]


def test_current_behavior_scans_for_sentence_inside_surrounding_text():
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*31"
    raw = f"vendor prefix {sentence} trailing text"

    # Characterization only: embedded scanning is not yet an approved input contract.
    assert extract_nmea_sentences(raw) == [sentence]
