import json

from server.db import now
from server.keywords import Keyword, Matcher, normalize, normalize_with_map, parse_variants, refresh_hits


def kw(term, *variants, id=1):
    return Keyword(id, term, tuple(variants))


def test_normalize_folds_case_accents_repeats_and_spaces():
    assert normalize("COLORUMMM!") == "colorum!"
    assert normalize("Pañero") == "panero"
    assert normalize("tigil\n\n  pasada") == "tigil pasada"
    assert normalize("kolorrum") == "kolorum"


def test_normalize_map_points_at_original_characters():
    text = "Ang  COLORUMM"
    norm, origin = normalize_with_map(text)
    assert norm == "ang colorum"
    assert len(origin) == len(norm)
    assert text[origin[norm.index("c")]] == "C"


def test_matches_whole_words_and_variants():
    m = Matcher([kw("colorum", "kolorum", "culorom")])
    assert m.hits(normalize("Hulihin ang mga KOLORUM!"))
    assert m.hits(normalize("dami ng culorom dito"))
    assert m.hits(normalize("colorummm"))
    assert not m.hits(normalize("colorumbo"))
    assert not m.hits(normalize("acolorum"))


def test_short_word_does_not_hit_longer_words():
    m = Matcher([kw("gas")])
    assert m.hits(normalize("mahal ang gas"))
    assert not m.hits(normalize("dagdag gastos"))


def test_wildcard_matches_longer_words():
    m = Matcher([kw("pahirap", "pahirap*")])
    assert m.hits(normalize("lalong pahirapan"))
    assert m.hits(normalize("puro pahirap"))
    # A wildcard extends the end of a word, not the start.
    assert not m.hits(normalize("pinapahirapan"))


def test_multi_word_keyword_spans_any_whitespace():
    m = Matcher([kw("tigil pasada")])
    assert m.hits(normalize("Mag TIGIL\n  pasada kayo"))
    assert not m.hits(normalize("tigilpasada"))


def test_highlight_escapes_html_and_marks_original_text():
    m = Matcher([kw("colorum", "kolorum")])
    html = str(m.highlight("<b>Hulihin</b> ang KOLORUMMM & iba"))
    assert "<b>" not in html
    assert "&lt;b&gt;Hulihin&lt;/b&gt;" in html
    assert "<mark>KOLORUMMM</mark>" in html
    assert "&amp; iba" in html


def test_highlight_without_hits_is_escaped_text():
    assert str(Matcher([kw("baha")]).highlight("<script>x</script>")) == "&lt;script&gt;x&lt;/script&gt;"


def test_highlight_merges_overlapping_matches():
    m = Matcher([kw("body number", id=1), kw("number", id=2)])
    assert str(m.highlight("Body Number po")) == "<mark>Body Number</mark> po"


def test_parse_variants_cleans_and_dedupes():
    assert parse_variants(" kolorum, Kolorum ,\nculorom,, ,colorum* ") == ["kolorum", "culorom", "colorum*"]


def test_refresh_hits_tracks_keyword_changes(conn):
    conn.execute("DELETE FROM keywords")
    conn.execute("INSERT INTO posts (id, url, source_url, added_at) VALUES (1, 'u', 'u', ?)", (now(),))
    for cid, text in [("a", "Dami ng kolorum"), ("b", "Baha na naman"), ("c", "Walang keyword")]:
        conn.execute(
            "INSERT INTO comments (id, post_id, text, text_norm, first_seen_at, last_seen_at) "
            "VALUES (?, 1, ?, ?, 0, 0)",
            (cid, text, normalize(text)),
        )
    conn.execute("INSERT INTO keywords (id, term, variants, created_at) VALUES (1, 'colorum', ?, 0)",
                 (json.dumps(["kolorum"]),))
    refresh_hits(conn)
    assert [r[0] for r in conn.execute("SELECT comment_id FROM keyword_hits")] == ["a"]

    conn.execute("INSERT INTO keywords (id, term, variants, created_at) VALUES (2, 'baha', '[]', 0)")
    refresh_hits(conn)
    assert sorted(r[0] for r in conn.execute("SELECT comment_id FROM keyword_hits")) == ["a", "b"]

    conn.execute("UPDATE keywords SET variants = '[]' WHERE id = 1")
    refresh_hits(conn, ["a"])
    assert [r[0] for r in conn.execute("SELECT comment_id FROM keyword_hits")] == ["b"]
