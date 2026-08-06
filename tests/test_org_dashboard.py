import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

from beaboss.org_dashboard import OrganizationHandler


def test_read_only_dashboard_serves_health_html_and_atomic_snapshot(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    organization = {"version": 1, "projects": [{"id": "checkout"}]}
    (state / "organization.json").write_text(
        json.dumps(organization), encoding="utf-8")
    server = ThreadingHTTPServer(("127.0.0.1", 0), OrganizationHandler)
    server.state_dir = str(state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/healthz") as response:
            assert json.load(response) == {"ok": True}
        with urllib.request.urlopen(base + "/") as response:
            html = response.read().decode("utf-8")
            assert "Live organization" in html
            assert response.headers["Content-Security-Policy"]
        with urllib.request.urlopen(base + "/organization.json") as response:
            assert json.load(response) == organization
            assert response.headers["Cache-Control"] == "no-store"
        hostile = urllib.request.Request(base + "/organization.json")
        hostile.add_unredirected_header("Host", "attacker.example")
        try:
            urllib.request.urlopen(hostile)
        except urllib.error.HTTPError as error:
            assert error.code == 421
        else:
            raise AssertionError("DNS-rebinding Host should be refused")
        try:
            urllib.request.urlopen(base + "/../core.json")
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("unexpected path should not be served")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_vscode_extension_points_at_local_observer():
    manifest = json.loads(open(
        "vscode-extension/package.json", encoding="utf-8").read())
    setting = manifest["contributes"]["configuration"]["properties"]
    assert setting["beaboss.organizationUrl"]["default"].endswith(
        ":8766/organization.json")
    assert manifest["contributes"]["views"]["explorer"][0]["id"] == \
        "beaboss.organization"
    source = open("vscode-extension/extension.js", encoding="utf-8").read()
    assert "showing last known snapshot" in source
    assert "return [...warnings" in source
