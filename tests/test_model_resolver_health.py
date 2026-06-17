"""
Tests for model_resolver._pick_newest's endpoint-health gating.

Scenario being verified:
  OpenRouter's /models catalogue keeps listing models even after they've
  been recalled (e.g. anthropic/claude-fable-5). Those zombie models stay
  "newest" forever and the resolver kept silently selecting them, causing
  the agent to ABSTAIN every day. The fix probes the per-model endpoints
  API and skips candidates whose endpoints all report uptime_last_1d == 0.

We mock both HTTP layers (catalogue + per-model endpoints) — no network.
"""

import json
import sys
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).parent.parent
sys.path.insert(0, str(REPO))

import model_resolver  # noqa: E402


def _model(model_id: str, created: int) -> dict:
    """Minimal OpenRouter catalogue entry that passes every schema filter."""
    return {
        "id": model_id,
        "created": created,
        "architecture": {
            "output_modalities": ["text"],
            "input_modalities":  ["text"],
        },
        "pricing":              {"completion": "0.000015"},
        "expiration_date":      None,
        "supported_parameters": ["max_tokens", "temperature"],
    }


def _endpoints_response(uptimes):
    """Build the JSON envelope OpenRouter returns from /models/<id>/endpoints."""
    return {"data": {"endpoints": [{"uptime_last_1d": u} for u in uptimes]}}


class _FakeHTTPResponse:
    """Stands in for urlopen()'s context-managed response object."""

    def __init__(self, payload):
        self._buf = BytesIO(json.dumps(payload).encode())

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _make_urlopen(endpoint_responses):
    """
    Build a urlopen replacement that returns per-model-id endpoint payloads.
    `endpoint_responses` maps model_id -> response payload OR an Exception to raise.
    """
    def _fake_urlopen(req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        # extract model id between /models/ and /endpoints
        marker_a, marker_b = "/models/", "/endpoints"
        model_id = url.split(marker_a, 1)[1].rsplit(marker_b, 1)[0]
        result = endpoint_responses[model_id]
        if isinstance(result, Exception):
            raise result
        return _FakeHTTPResponse(result)
    return _fake_urlopen


CFG = {
    "prefix":         "anthropic/",
    "require":        [],
    "forbid":         [],
    "name_blocklist": [],
}


class PickNewestHealthTests(unittest.TestCase):

    def test_zero_uptime_newest_is_skipped_for_next_healthy(self):
        """
        Reproduces the claude-fable-5 incident: newest model probes as
        zero-uptime; resolver must fall through to the next-newest healthy
        candidate.
        """
        catalogue = [
            _model("anthropic/claude-fable-5", created=2_000_000_000),  # newest, recalled
            _model("anthropic/claude-opus-4.8", created=1_900_000_000),  # healthy
            _model("anthropic/claude-opus-4.7", created=1_800_000_000),
        ]
        urlopen = _make_urlopen({
            "anthropic/claude-fable-5":  _endpoints_response([0, 0, None]),
            "anthropic/claude-opus-4.8": _endpoints_response([99.5, 100.0]),
            "anthropic/claude-opus-4.7": _endpoints_response([100.0]),
        })
        with patch.object(model_resolver.urllib.request, "urlopen", urlopen):
            picked = model_resolver._pick_newest(catalogue, CFG)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "anthropic/claude-opus-4.8")

    def test_health_probe_raises_fails_open_to_newest(self):
        """
        A flaky probe must NOT block resolution: if every probe raises,
        we fall back to the newest schema-passing candidate.
        """
        catalogue = [
            _model("anthropic/claude-opus-4.8", created=1_900_000_000),
            _model("anthropic/claude-fable-5",  created=2_000_000_000),  # newest
        ]
        urlopen = _make_urlopen({
            "anthropic/claude-fable-5":  ConnectionError("probe died"),
            "anthropic/claude-opus-4.8": ConnectionError("probe died"),
        })
        with patch.object(model_resolver.urllib.request, "urlopen", urlopen):
            picked = model_resolver._pick_newest(catalogue, CFG)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "anthropic/claude-fable-5")

    def test_healthy_newest_selected_unchanged(self):
        """
        Baseline: when the newest candidate is healthy, the resolver
        returns it without iterating further.
        """
        catalogue = [
            _model("anthropic/claude-opus-4.8", created=2_000_000_000),
            _model("anthropic/claude-opus-4.7", created=1_900_000_000),
        ]
        urlopen = _make_urlopen({
            "anthropic/claude-opus-4.8": _endpoints_response([98.7, 100.0]),
            "anthropic/claude-opus-4.7": _endpoints_response([100.0]),
        })
        with patch.object(model_resolver.urllib.request, "urlopen", urlopen):
            picked = model_resolver._pick_newest(catalogue, CFG)
        self.assertIsNotNone(picked)
        self.assertEqual(picked["id"], "anthropic/claude-opus-4.8")


class EndpointHealthOkTests(unittest.TestCase):
    """Direct unit coverage of the _endpoint_health_ok helper."""

    def test_zero_uptime_endpoints_unhealthy(self):
        urlopen = _make_urlopen({
            "x/y": _endpoints_response([0, 0, None]),
        })
        with patch.object(model_resolver.urllib.request, "urlopen", urlopen):
            self.assertFalse(model_resolver._endpoint_health_ok("x/y"))

    def test_empty_endpoints_unhealthy(self):
        urlopen = _make_urlopen({"x/y": {"data": {"endpoints": []}}})
        with patch.object(model_resolver.urllib.request, "urlopen", urlopen):
            self.assertFalse(model_resolver._endpoint_health_ok("x/y"))

    def test_http_error_fails_open(self):
        urlopen = _make_urlopen({"x/y": TimeoutError("slow")})
        with patch.object(model_resolver.urllib.request, "urlopen", urlopen):
            self.assertTrue(model_resolver._endpoint_health_ok("x/y"))

    def test_any_healthy_endpoint_is_enough(self):
        urlopen = _make_urlopen({
            "x/y": _endpoints_response([0, 99.9, 0]),
        })
        with patch.object(model_resolver.urllib.request, "urlopen", urlopen):
            self.assertTrue(model_resolver._endpoint_health_ok("x/y"))


if __name__ == "__main__":
    unittest.main()
