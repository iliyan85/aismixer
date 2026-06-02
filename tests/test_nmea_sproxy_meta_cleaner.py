import importlib.util
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "nmea_sproxy" / "meta_cleaner.py"
SPEC = importlib.util.spec_from_file_location("nmea_sproxy_meta_cleaner", MODULE_PATH)
meta_cleaner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(meta_cleaner)


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
