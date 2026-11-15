"""Detection spec validation + deterministic evaluation."""
from watcher import evaluate

STOCK_HTML = ("<html><head><script type=\"application/ld+json\">"
              '{"availability":"http://schema.org/OutOfStock"}</script></head>'
              "<body><h1>Reflx Lab Double X</h1><p>Out of stock</p></body></html>")
RESTOCK_HTML = STOCK_HTML.replace("OutOfStock", "InStock").replace(
    "Out of stock", "In stock — add to cart")


def _spec(**kw):
    base = {"mode": "condition", "present": ["In stock", "Add to cart"],
            "absent": ["Out of stock", "Sold out"], "min_present": 1, "notes": ""}
    base.update(kw)
    return base


# ---- validation -------------------------------------------------------------

def test_validate_accepts_good_spec():
    assert evaluate.validate_spec(_spec()) is None
    assert evaluate.validate_spec({"mode": "hash"}) is None


def test_validate_rejects_junk():
    assert evaluate.validate_spec("nope") is not None
    assert evaluate.validate_spec({"mode": "regex"}) is not None
    assert evaluate.validate_spec({"mode": "condition"}) is not None       # no needles
    assert evaluate.validate_spec(_spec(present="In stock")) is not None   # not a list
    assert evaluate.validate_spec(_spec(present=[1, 2])) is not None
    assert evaluate.validate_spec(_spec(absent=[""])) is not None
    assert evaluate.validate_spec(_spec(absent=["x" * 200])) is not None
    assert evaluate.validate_spec(_spec(present=["a"] * 20)) is not None
    assert evaluate.validate_spec(_spec(min_present=0)) is not None
    assert evaluate.validate_spec(_spec(min_present=True)) is not None
    assert evaluate.validate_spec(_spec(present=["a"], min_present=3)) is not None


def test_normalize_strips_and_clamps():
    s = evaluate.normalize_spec(_spec(present=["  In stock ", ""], min_present=0))
    assert s["present"] == ["In stock"] and s["min_present"] == 1


# ---- condition evaluation -------------------------------------------------------

def test_condition_out_of_stock_is_not_triggered():
    ev = evaluate.evaluate(evaluate.normalize_spec(_spec()), STOCK_HTML, None)
    assert ev["status"] == "ok"
    assert "Out of stock" in ev["detail"]
    assert ev["matched"]["absent_hits"] == ["Out of stock"]


def test_condition_restock_triggers():
    ev = evaluate.evaluate(evaluate.normalize_spec(_spec()), RESTOCK_HTML, None)
    assert ev["status"] == "changed"
    assert "In stock" in ev["matched"]["present_hits"]
    assert ev["matched"]["absent_hits"] == []


def test_condition_matches_jsonld_in_raw_html():
    # the visible text says nothing about schema.org; the raw HTML does
    spec = evaluate.normalize_spec({"mode": "condition", "present": ["schema.org/InStock"],
                                    "absent": ["schema.org/OutOfStock"], "min_present": 1})
    assert evaluate.evaluate(spec, STOCK_HTML, None)["status"] == "ok"
    assert evaluate.evaluate(spec, RESTOCK_HTML, None)["status"] == "changed"


def test_condition_case_insensitive():
    spec = evaluate.normalize_spec(_spec(present=["in STOCK"], absent=["sold out"]))
    assert evaluate.evaluate(spec, RESTOCK_HTML, None)["status"] == "changed"


def test_condition_min_present():
    spec = evaluate.normalize_spec(_spec(present=["In stock", "add to cart"],
                                         min_present=2))
    # "Add to cart" appears in "In stock — add to cart" (case-insensitive)
    assert evaluate.evaluate(spec, RESTOCK_HTML, None)["status"] == "changed"
    assert evaluate.evaluate(spec, STOCK_HTML, None)["status"] == "ok"


def test_condition_absent_only_spec():
    spec = evaluate.normalize_spec({"mode": "condition", "present": [],
                                    "absent": ["Out of stock"], "min_present": 1})
    assert evaluate.evaluate(spec, STOCK_HTML, None)["status"] == "ok"
    assert evaluate.evaluate(spec, RESTOCK_HTML, None)["status"] == "changed"


# ---- hash mode -------------------------------------------------------------

def test_hash_mode_needs_baseline():
    spec = {"mode": "hash", "present": [], "absent": [], "min_present": 1, "notes": ""}
    first = evaluate.evaluate(spec, "<p>hello</p>", None)
    assert first["status"] == "ok"                      # baseline recorded
    h = first["snapshot_hash"]
    assert evaluate.evaluate(spec, "<p>hello</p>", h)["status"] == "ok"
    assert evaluate.evaluate(spec, "<p>hello <b>new</b></p>", h)["status"] == "changed"


def test_hash_ignores_invisible_churn():
    spec = {"mode": "hash", "present": [], "absent": [], "min_present": 1, "notes": ""}
    a = evaluate.evaluate(spec, "<p>hi</p><script>var t=1;</script>", None)
    b = evaluate.evaluate(spec, "<div>  hi </div><script>var t=2;</script>", a["snapshot_hash"])
    assert b["status"] == "ok"                          # scripts/whitespace don't count
