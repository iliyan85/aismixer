from assembler import AIVDMAssembler


def test_feed_returns_none_for_non_numeric_total():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,x,1,,A,payload,0*00") is None


def test_feed_returns_none_for_non_numeric_current():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,2,x,,A,payload,0*00") is None


def test_feed_returns_none_for_too_few_fields():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,1,1,,A,payload") is None


def test_feed_returns_none_for_zero_total():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,0,1,,A,payload,0*00") is None


def test_feed_returns_none_for_zero_current():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,2,0,,A,payload,0*00") is None


def test_feed_returns_none_when_current_exceeds_total():
    assembler = AIVDMAssembler()

    assert assembler.feed("src", "!AIVDM,2,3,,A,payload,0*00") is None


def test_feed_returns_single_part_sentence():
    assembler = AIVDMAssembler()
    sentence = "!AIVDM,1,1,,A,payload,0*00"

    assert assembler.feed("src", sentence) == [sentence]


def test_feed_assembles_valid_multipart_sentences():
    assembler = AIVDMAssembler()
    first = "!AIVDM,2,1,7,A,payload1,0*00"
    second = "!AIVDM,2,2,7,A,payload2,0*00"

    assert assembler.feed("src", first) is None
    assert assembler.feed("src", second) == [first, second]
