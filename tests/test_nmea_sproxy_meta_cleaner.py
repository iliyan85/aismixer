import importlib.util
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

MODULE_PATH = REPO_ROOT / "nmea_sproxy" / "meta_cleaner.py"
SPEC = importlib.util.spec_from_file_location("nmea_sproxy_meta_cleaner", MODULE_PATH)
meta_cleaner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(meta_cleaner)

# Load the core scanner's test module to reuse its authoritative TALKERS
# tuple as the drift-guard source of truth, without importing core.nmea_scanner
# into the standalone proxy runtime itself.
_SCANNER_TESTS_PATH = REPO_ROOT / "tests" / "test_nmea_scanner.py"
_SCANNER_TESTS_SPEC = importlib.util.spec_from_file_location(
    "nmea_sproxy_meta_cleaner_core_talkers", _SCANNER_TESTS_PATH
)
_scanner_tests = importlib.util.module_from_spec(_SCANNER_TESTS_SPEC)
_SCANNER_TESTS_SPEC.loader.exec_module(_scanner_tests)
CORE_TALKERS = _scanner_tests.TALKERS


def _sentence(talker="AI", family="VDM", checksum="00"):
    return f"!{talker}{family},1,1,,A,payload,0*{checksum}"


def test_extracts_plain_aivdm():
    sentence = "!AIVDM,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*5C"

    assert meta_cleaner.extract_nmea_sentences(sentence) == [sentence]


def test_extracts_plain_aivdo():
    sentence = "!AIVDO,1,1,,A,15Muq?002>G?svP00<:O?vN60<0,0*42"

    assert meta_cleaner.extract_nmea_sentences(sentence) == [sentence]


def test_extracts_embedded_aivdm_from_prefixed_text():
    sentence = "!AIVDM,1,1,,B,33P@?P5000PD;88MD5MTDwwP0000,0*5D"
    raw = f"vendor metadata before {sentence} trailing text"

    assert meta_cleaner.extract_nmea_sentences(raw) == [sentence]


def test_non_ais_input_returns_no_sentences():
    assert meta_cleaner.extract_nmea_sentences("not an ais sentence") == []


def test_proxy_talker_whitelist_matches_core_authoritative_set():
    """Regression for Point 4: the proxy's talker set must track core's
    whitelist exactly, so it neither drops a supported talker nor accepts
    an unsupported one."""
    assert set(meta_cleaner.SUPPORTED_TALKERS) == set(CORE_TALKERS)
    assert len(meta_cleaner.SUPPORTED_TALKERS) == len(
        set(meta_cleaner.SUPPORTED_TALKERS)
    )


@pytest.mark.parametrize("talker", CORE_TALKERS)
def test_every_authoritative_talker_accepts_vdm(talker):
    sentence = _sentence(talker=talker, family="VDM")

    assert meta_cleaner.extract_nmea_sentences(sentence) == [sentence]


@pytest.mark.parametrize("talker", CORE_TALKERS)
def test_every_authoritative_talker_accepts_vdo(talker):
    sentence = _sentence(talker=talker, family="VDO")

    assert meta_cleaner.extract_nmea_sentences(sentence) == [sentence]


def test_unsupported_talker_vdm_is_rejected():
    assert meta_cleaner.extract_nmea_sentences(_sentence(talker="ZZ", family="VDM")) == []


def test_unsupported_talker_vdo_is_rejected():
    assert meta_cleaner.extract_nmea_sentences(_sentence(talker="ZZ", family="VDO")) == []


@pytest.mark.parametrize("sentence", ["!GPGGA,1,2,3*00", "!GPRMC,1,2,3*00"])
def test_unrelated_nmea_family_is_rejected(sentence):
    assert meta_cleaner.extract_nmea_sentences(sentence) == []


def test_tag_prefixed_sentence_returns_bare_sentence_only():
    sentence = _sentence(talker="BS", family="VDM")
    raw = "\\s:rx,c:123*14\\" + sentence

    assert meta_cleaner.extract_nmea_sentences(raw) == [sentence]


def test_multiple_supported_sentences_are_extracted_in_order():
    first = _sentence(talker="AB", family="VDM", checksum="11")
    second = _sentence(talker="BS", family="VDO", checksum="22")
    raw = f"noise {first} between {second} trailing"

    assert meta_cleaner.extract_nmea_sentences(raw) == [first, second]
