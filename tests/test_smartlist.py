import pytest

from jrmc_plex_migrate.smartlist import TranslationError, translate


def test_simple_genre():
    sf = translate("[Genre]=[Rock]")
    assert sf.filters == {"genre": "Rock"}
    assert sf.libtype == "track"


def test_and_of_two_fields():
    sf = translate("[Genre]=[Rock] [Date (year)]=2001")
    assert sf.filters == {"genre": "Rock", "year": 2001}


def test_or_list_values():
    sf = translate("[Genre]=[Rock],[Metal]")
    assert sf.filters == {"genre": ["Rock", "Metal"]}


def test_negation():
    sf = translate("-[Genre]=[Christmas]")
    assert sf.filters == {"genre!": "Christmas"}


def test_numeric_comparison_and_rating_scale():
    sf = translate("[Rating]=>=4")
    # 4 stars -> userRating 8; Plex has no '>=', so ">= 8" becomes ">> 7".
    assert sf.filters == {"userRating>>": 7}


def test_plays_greater_than():
    sf = translate("[Number Plays]=>5")
    assert sf.filters == {"track.viewCount>>": 5}


def test_numeric_range_uses_strict_operators():
    sf = translate("[Date (year)]=1997-2006")
    # 1997..2006 inclusive -> year >> 1996 and year << 2007.
    assert sf.filters == {"year>>": 1996, "year<<": 2007}


def test_negated_equality():
    sf = translate("-[Number Plays]=0")
    assert sf.filters == {"track.viewCount!": 0}


def test_sort_and_limit_modifiers():
    sf = translate("[Genre]=[Jazz] ~sort=[Date (year)]- ~n=25")
    assert sf.filters == {"genre": "Jazz"}
    assert sf.sort == "track.year:desc"
    assert sf.limit == 25


def test_seq_alias_sort_resolves_to_viewcount():
    # JRiver "Top Hits": ~seq aliases Number Plays, then sorts by [seq] desc.
    sf = translate("[Number Plays]=>0 ~seq=[Number Plays] ~sort=[seq]-d ~n=100")
    assert sf.filters == {"track.viewCount>>": 0}
    assert sf.sort == "track.viewCount:desc"
    assert sf.limit == 100


def test_unsupported_field_raises():
    with pytest.raises(TranslationError):
        translate("[Bitrate]=[320]")


def test_comparison_with_or_list_raises():
    with pytest.raises(TranslationError):
        translate("[Number Plays]=>[5],[6]")


def test_empty_rule_raises():
    with pytest.raises(TranslationError):
        translate("   ")
