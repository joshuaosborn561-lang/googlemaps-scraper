"""Mocked SerperSearch + backend factory. No live Serper/Apify calls."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from gmscraper.config import Settings
from gmscraper.websearch import (
    BACKENDS,
    MAX_CHARS,
    SERPER_URL,
    ApifySerp,
    NullSearch,
    OpenWebNinja,
    SerperSearch,
    harvest_text,
    make_backend,
)


SERPER_PAYLOAD = {
    "organic": [
        {
            "title": "Acme HVAC — About the Owner",
            "snippet": "Jane Doe founded Acme HVAC in Dallas.",
            "link": "https://acme.example/about",
        },
        {
            "title": "BBB Business Profile: Acme HVAC",
            "snippet": "Principal: Jane Doe.",
            "link": "https://bbb.example/acme",
        },
    ],
    "answerBox": {
        "title": "Who owns Acme HVAC?",
        "answer": "Jane Doe is the owner of Acme HVAC.",
        "snippet": "Acme HVAC is a family-owned contractor.",
    },
    "knowledgeGraph": {
        "title": "Acme HVAC",
        "type": "HVAC contractor",
        "description": "Dallas HVAC company owned by Jane Doe.",
    },
}


def _settings(**kwargs) -> Settings:
    defaults = {
        "serper_api_key": "",
        "apify_token": "",
        "owj_key": "",
        "fallback_source": "serper",
    }
    defaults.update(kwargs)
    return Settings(**defaults)


def _ok(payload=None, status=200) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload if payload is not None else SERPER_PAYLOAD
    r.raise_for_status = MagicMock()
    return r


def test_backends_dict_and_defaults() -> None:
    assert BACKENDS == {
        "serper": SerperSearch,
        "apify": ApifySerp,
        "openwebninja": OpenWebNinja,
        "none": NullSearch,
    }
    assert SerperSearch.cost_per_search == 0.001
    assert MAX_CHARS == 4000
    assert SERPER_URL == "https://google.serper.dev/search"


def test_fallback_source_default_is_serper(monkeypatch) -> None:
    monkeypatch.delenv("FALLBACK_SOURCE", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    s = Settings()
    assert s.fallback_source == "serper"
    assert s.serper_api_key == ""


def test_serper_api_key_from_env(monkeypatch) -> None:
    monkeypatch.setenv("SERPER_API_KEY", "sk-test")
    monkeypatch.delenv("FALLBACK_SOURCE", raising=False)
    s = Settings()
    assert s.serper_api_key == "sk-test"


def test_harvest_text_serper_shape() -> None:
    chunks: list[str] = []
    harvest_text(SERPER_PAYLOAD, chunks)
    text = "\n".join(chunks)
    assert "Acme HVAC — About the Owner" in text
    assert "Jane Doe founded Acme HVAC in Dallas." in text
    assert "Jane Doe is the owner of Acme HVAC." in text
    assert "Dallas HVAC company owned by Jane Doe." in text
    assert "https://acme.example/about" not in text  # links are not TEXT_KEYS


def test_serper_search_text_posts_and_harvests() -> None:
    backend = SerperSearch(_settings(serper_api_key="sk-test"))
    assert backend.enabled is True
    backend.session.post = MagicMock(return_value=_ok())

    text = backend.search_text('who owns "Acme HVAC" in Dallas', limit=8)

    backend.session.post.assert_called_once()
    args, kwargs = backend.session.post.call_args
    assert args[0] == SERPER_URL
    assert kwargs["json"] == {
        "q": 'who owns "Acme HVAC" in Dallas',
        "gl": "us",
        "hl": "en",
        "num": 8,
    }
    assert kwargs["timeout"] == backend.timeout
    assert backend.session.headers["X-API-KEY"] == "sk-test"
    assert "Jane Doe founded Acme HVAC in Dallas." in text
    assert "Jane Doe is the owner of Acme HVAC." in text
    assert "Dallas HVAC company owned by Jane Doe." in text
    assert backend.request_count == 1
    assert len(text) <= MAX_CHARS


def test_serper_caps_max_chars() -> None:
    huge = {
        "organic": [
            {"title": "T" * 80, "snippet": "S" * 400} for _ in range(20)
        ]
    }
    backend = SerperSearch(_settings(serper_api_key="k"))
    backend.session.post = MagicMock(return_value=_ok(huge))
    text = backend.search_text("q", limit=20)
    assert len(text) == MAX_CHARS


def test_serper_401_and_403_return_empty() -> None:
    backend = SerperSearch(_settings(serper_api_key="bad"))
    for status in (401, 403):
        backend.session.post = MagicMock(return_value=_ok(status=status))
        assert backend.search_text("q") == ""


def test_serper_retries_429_then_succeeds() -> None:
    backend = SerperSearch(_settings(serper_api_key="k"), max_retries=2)
    backend.session.post = MagicMock(
        side_effect=[_ok(status=429), _ok(status=503), _ok()]
    )
    with patch("gmscraper.websearch.time.sleep"):
        text = backend.search_text("q")
    assert "Jane Doe founded Acme HVAC in Dallas." in text
    assert backend.request_count == 3


def test_serper_exhausted_retries_return_empty() -> None:
    backend = SerperSearch(_settings(serper_api_key="k"), max_retries=1)
    backend.session.post = MagicMock(return_value=_ok(status=500))
    with patch("gmscraper.websearch.time.sleep"):
        assert backend.search_text("q") == ""
    assert backend.request_count == 2


def test_serper_network_error_retries() -> None:
    backend = SerperSearch(_settings(serper_api_key="k"), max_retries=1)
    backend.session.post = MagicMock(
        side_effect=[requests.ConnectionError("boom"), _ok()]
    )
    with patch("gmscraper.websearch.time.sleep"):
        text = backend.search_text("q")
    assert "Jane Doe is the owner of Acme HVAC." in text
    assert backend.request_count == 2


def test_serper_disabled_without_key() -> None:
    backend = SerperSearch(_settings(serper_api_key=""))
    assert backend.enabled is False
    assert backend.search_text("q") == ""
    assert backend.request_count == 0


def test_make_backend_default_is_serper() -> None:
    b = make_backend(_settings(serper_api_key="k", fallback_source="serper"))
    assert isinstance(b, SerperSearch)
    assert b.enabled is True


def test_make_backend_serper_without_key_is_null_not_apify() -> None:
    s = _settings(
        serper_api_key="",
        apify_token="apify-token-present",
        fallback_source="serper",
    )
    b = make_backend(s)
    assert isinstance(b, NullSearch)
    assert not isinstance(b, ApifySerp)
    assert not isinstance(b, SerperSearch)
    assert b.enabled is False
    assert b.search_text("q") == ""


def test_make_backend_explicit_apify_still_works() -> None:
    b = make_backend(
        _settings(apify_token="tok", fallback_source="apify"),
        "apify",
    )
    assert isinstance(b, ApifySerp)
    assert b.enabled is True


def test_make_backend_none_and_unknown() -> None:
    assert isinstance(make_backend(_settings(), "none"), NullSearch)
    with pytest.raises(SystemExit, match="Unknown fallback source"):
        make_backend(_settings(), "not-a-backend")
