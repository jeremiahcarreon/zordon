from zordon.output.acronyms import expand_acronyms, expand_symbols


def test_expands_whole_words_only():
    assert expand_acronyms("the API returned JSON") == "the A P I returned jason"
    assert expand_acronyms("auth.py") == "auth.py"
    assert expand_acronyms("my_API_var") == "my_API_var"
    assert expand_acronyms("README") == "read me"


def test_symbols():
    assert expand_symbols("a -> b && c") == "a to b and c"
