from zordon.output.sentences import SentenceBuffer, split_sentences


def test_basic_split():
    assert split_sentences("Done. Tests pass! Anything else?") == [
        "Done.",
        "Tests pass!",
        "Anything else?",
    ]


def test_filenames_and_decimals_do_not_split():
    out = split_sentences("I edited auth.py and bumped it to v1.2. Then I ran it.")
    assert out == ["I edited auth.py and bumped it to v1.2.", "Then I ran it."]


def test_abbreviations():
    assert split_sentences("Use a cache, e.g. Redis. It is fast.") == [
        "Use a cache, e.g. Redis.",
        "It is fast.",
    ]


def test_incremental_push_waits_for_boundary():
    b = SentenceBuffer()
    assert b.push("Adding the retry") == []
    assert b.push("logic to the upload handler.") == []  # trailing '.' waits for continuation
    assert b.push("Next I will") == ["Adding the retry logic to the upload handler."]
    assert b.flush() == ["Next I will"]
    assert b.flush() == []


def test_question_mark_at_end_is_final():
    b = SentenceBuffer()
    assert b.push("Should I continue?") == ["Should I continue?"]


def test_long_runon_is_cut():
    text = "word " * 100
    out = SentenceBuffer().push(text)
    assert out and all(len(s) <= 300 for s in out)


def test_whitespace_normalized():
    assert split_sentences("a   b\n\nc.") == ["a b c."]
