from omonflow.storage.history import History


def test_add_list_search_delete_clear(tmp_path):
    h = History(tmp_path / "h.sqlite3")
    a = h.add("Halo, deploy ke server Docker", "TextEdit", "pending", 2.1, "small")
    h.add("100% done_with tests", "Safari", "inserted", 1.0, "small")
    h.set_outcome(a, "inserted")
    assert [e.text for e in h.list()] == ["100% done_with tests", "Halo, deploy ke server Docker"]
    assert [e.text for e in h.list("docker")] == ["Halo, deploy ke server Docker"]
    assert [e.text for e in h.list("100%")] == ["100% done_with tests"]  # LIKE wildcards escaped
    assert h.list("_with")[0].text.startswith("100%")
    assert h.list("0%d") == []
    h.delete(a)
    assert h.count() == 1
    h.clear()
    assert h.count() == 0
    h.close()


def test_clear_leaves_no_text_on_disk(tmp_path):
    p = tmp_path / "h.sqlite3"
    h = History(p)
    h.add("rahasia-unik-12345", None, "inserted", None, None)
    h.clear()
    h.close()
    assert b"rahasia-unik-12345" not in p.read_bytes()
