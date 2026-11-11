from gmscraper.resolve_places import is_premise, pick_best

Q = "1200 Main St, Dallas, TX 75201"


def test_premise_detected():
    assert is_premise({"name": "1200 Main St", "address": Q})
    assert not is_premise({"name": "Acme Holdings", "address": Q, "main_category": "Real estate"})


def test_business_beats_premise():
    rs = [
        {"name": "1200 Main St", "address": Q},
        {"name": "Acme Holdings", "address": Q, "main_category": "Real estate", "website": "acme.com"},
    ]
    best, conf, n = pick_best(Q, rs, min_confidence=0.5)
    assert best["name"] == "Acme Holdings" and n == 1 and conf > 0.8


def test_premise_only_is_no_match():
    best, conf, _ = pick_best(Q, [{"name": "1200 Main St", "address": Q}], min_confidence=0.5)
    assert best is None


def test_business_name_prefixed_address():
    rs = [
        {"name": "1200 Main St", "address": Q},
        {"name": "Acme Holdings", "address": "Acme Holdings, 1200 Main St, Dallas, TX 75201", "website": "acme.com"},
    ]
    best, conf, _ = pick_best(Q, rs, min_confidence=0.5)
    assert best["name"] == "Acme Holdings" and conf > 0.8
