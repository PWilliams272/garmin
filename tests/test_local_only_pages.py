"""Tests for the local-only page gate.

`/modeling` is deliberately not published on garmin.peterwilliams.dev. The
whole risk here is a *silent* failure: a gate that stops working publishes the
page, and nothing about the local experience would look any different. So
these tests assert the closed state as carefully as the open one.

The gate is fail-closed and needs two independent conditions to serve the
page, so no single misconfiguration can publish it.
"""

from __future__ import annotations

import pytest

from garmin.app.app import create_app
from garmin.app import routes as routes_module


@pytest.fixture
def client():
    app = create_app()
    app.config.update(TESTING=True)
    with app.test_client() as c:
        yield c


def test_modeling_is_hidden_when_the_flag_is_unset(client, monkeypatch):
    """The default. An unset variable must hide the page, never publish it."""
    monkeypatch.delenv('GARMIN_ENABLE_LOCAL_PAGES', raising=False)
    assert client.get('/modeling').status_code == 404


@pytest.mark.parametrize('value', ['', '0', 'true', 'True', 'yes', 'on'])
def test_only_the_exact_value_1_opens_the_gate(client, monkeypatch, value):
    """A mistyped or truthy-looking value must not be treated as enabled."""
    monkeypatch.setenv('GARMIN_ENABLE_LOCAL_PAGES', value)
    assert client.get('/modeling').status_code == 404


def test_modeling_is_served_when_explicitly_enabled(client, monkeypatch):
    monkeypatch.setenv('GARMIN_ENABLE_LOCAL_PAGES', '1')
    monkeypatch.delenv('GARMIN_VIEWER_SOURCE', raising=False)
    assert client.get('/modeling').status_code == 200


def test_the_deployment_marker_keeps_the_page_hidden_even_with_the_flag_set(client, monkeypatch):
    """The second, independent guard: the deployed viewer's systemd unit sets
    GARMIN_VIEWER_SOURCE, so even a leaked flag cannot publish the page."""
    monkeypatch.setenv('GARMIN_ENABLE_LOCAL_PAGES', '1')
    monkeypatch.setenv('GARMIN_VIEWER_SOURCE', 's3')
    assert client.get('/modeling').status_code == 404


def test_the_guard_keys_off_presence_not_value(client, monkeypatch):
    """Regression: the guard used to compare the resolved source against 's3'.
    When S3 became the default everywhere, local dev read S3 too and the page
    vanished locally. Any value of the deployment marker must hide it."""
    monkeypatch.setenv('GARMIN_ENABLE_LOCAL_PAGES', '1')
    monkeypatch.setenv('GARMIN_VIEWER_SOURCE', 'local')
    assert client.get('/modeling').status_code == 404


def test_the_api_is_gated_too_not_just_the_page(client, monkeypatch):
    """Gating only the HTML would publish the same content as JSON."""
    monkeypatch.delenv('GARMIN_ENABLE_LOCAL_PAGES', raising=False)
    assert client.get('/api/model_report_data?source=local').status_code == 404


def test_nav_does_not_link_to_modeling_when_it_is_hidden(client, monkeypatch):
    """A link to a 404 is worse than no link."""
    monkeypatch.delenv('GARMIN_ENABLE_LOCAL_PAGES', raising=False)
    body = client.get('/data_status').get_data(as_text=True)
    assert 'Modeling' not in body
    assert '/modeling' not in body


def test_nav_links_to_modeling_when_it_is_available(client, monkeypatch):
    monkeypatch.setenv('GARMIN_ENABLE_LOCAL_PAGES', '1')
    monkeypatch.delenv('GARMIN_VIEWER_SOURCE', raising=False)
    body = client.get('/data_status').get_data(as_text=True)
    assert '/modeling' in body


def test_the_removed_explorer_page_is_gone_for_good(client):
    """Removed on request 2026-08-18. Not gated -- deleted."""
    assert client.get('/muscle_explorer').status_code == 404


def test_no_template_still_links_to_the_explorer():
    """A dead nav link would survive route deletion silently."""
    from pathlib import Path
    templates = Path(routes_module.__file__).parent / 'templates'
    offenders = [p.name for p in templates.glob('*.html')
                 if 'muscle_explorer' in p.read_text()]
    assert offenders == []


def test_local_dev_entrypoint_opens_the_gate(monkeypatch):
    """`python -m garmin.app.app` must serve /modeling with no extra setup,
    while create_app (what gunicorn serves) must not."""
    monkeypatch.delenv('GARMIN_ENABLE_LOCAL_PAGES', raising=False)
    import garmin.app.app as app_module

    started = {}
    monkeypatch.setattr(app_module.Flask, 'run', lambda self, **kw: started.update(kw or {'ran': True}))
    app_module.main()

    assert routes_module.local_pages_enabled() is True
