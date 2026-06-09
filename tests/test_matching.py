from jrmc_plex_migrate.matching import normalize, score_candidate, similarity


def test_normalize_strips_feat_and_brackets():
    assert normalize("Song (Remastered 2011) [feat. Someone]") == "song"


def test_normalize_accents():
    assert normalize("Beyoncé") == normalize("Beyonce")


def test_similarity_identical():
    assert similarity("Hello World", "hello world") == 1.0


def test_score_prefers_correct_artist():
    good = score_candidate(
        "Yesterday", "The Beatles", "Help!",
        "Yesterday", "The Beatles", "Help!",
    )
    wrong_artist = score_candidate(
        "Yesterday", "The Beatles", "Help!",
        "Yesterday", "Boyz II Men", "Cooleyhighharmony",
    )
    assert good > 0.9
    assert wrong_artist < good
    # Penalized below a typical accept threshold.
    assert wrong_artist < 0.72


def test_score_ignores_album_when_missing():
    s = score_candidate(
        "Track", "Artist", "",
        "Track", "Artist", "Some Album",
        use_album=True,
    )
    assert s > 0.9
