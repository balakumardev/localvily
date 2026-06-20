from adapters.storm import chunk, to_storm


def test_chunk_splits_by_size():
    out = chunk("a" * 2500, size=1000)
    assert len(out) == 3
    assert "".join(out) == "a" * 2500


def test_chunk_empty_and_none_yield_single_empty_chunk():
    assert chunk("") == [""]
    assert chunk(None) == [""]


def test_to_storm_maps_shape_and_skips_empty_text():
    result = {
        "status": "ok",
        "results": [
            {"url": "https://a", "title": "A", "snippet": "sa", "text": "x" * 1500, "fetch_error": None},
            {"url": "https://b", "title": "B", "snippet": "sb", "text": "", "fetch_error": "nav failed"},
        ],
    }
    out = to_storm(result)
    assert len(out) == 1
    assert out[0]["url"] == "https://a"
    assert out[0]["description"] == "sa"
    assert "".join(out[0]["snippets"]) == "x" * 1500


def test_to_storm_returns_empty_on_non_ok():
    assert to_storm({"status": "error", "error": "down"}) == []
