import ast
import importlib.util
import os
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

API_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/pairdown"


def _load_plugin_api():
    # Mirrors how Hermes imports a user plugin's backend: by file path, under a
    # synthetic module name registered in sys.modules before execution.
    name = "hermes_dashboard_plugin_pairdown"
    spec = importlib.util.spec_from_file_location(name, API_FILE)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def client():
    app = FastAPI()
    app.include_router(_load_plugin_api().router, prefix=PREFIX)
    return TestClient(app)


@pytest.fixture()
def api(client):
    def call(route, **params):
        return client.get(f"{PREFIX}{route}", params=params)

    return call


@pytest.fixture()
def vault(tmp_path):
    """A temporary Obsidian-style vault plus a secret file next to it."""
    root = tmp_path / "vault"
    files = {
        ".obsidian/app.json": b"{}",
        ".obsidian/notes-in-config.md": b"config note",
        ".trash/deleted.md": b"deleted",
        "node_modules/pkg/readme.md": b"dependency readme",
        "Welcome.md": b"# Welcome\n",
        "Daily/2026-01-01.md": b"daily",
        "Projects/Alpha/Plan.md": b"plan",
        "Projects/Alpha/data.txt": b"not a note",
        "Projects/Beta/Deep/Nested note.md": b"nested",
        "UPPER.MD": b"upper-case suffix",
        "Accents.md": "Caf\u00e9 \u00e0 la cr\u00e8me \u2615 \U0001f600 \u65e5\u672c\u8a9e\n".encode("utf-8"),
        "Windows endings.md": b"line one\r\nline two\r\n",
        "Bob's notes & ideas.md": b"apostrophe and spaces",
        "bad-encoding.md": b"\xff\xfe\x00bad",
    }
    for rel, data in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (tmp_path / "secret.md").write_bytes(b"TOP SECRET")
    return root


def _symlink_or_skip(link: Path, target: Path, is_dir: bool):
    try:
        os.symlink(target, link, target_is_directory=is_dir)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


def test_health(api):
    response = api("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["plugin"] == "pairdown"
    assert body["platform"] == sys.platform


def test_validate_vault(api, vault):
    body = api("/validate", root=str(vault)).json()
    assert body == {"root": str(vault), "exists": True, "valid": True}


def test_validate_directory_without_obsidian(api, vault):
    body = api("/validate", root=str(vault / "Projects")).json()
    assert body["exists"] is True
    assert body["valid"] is False


def test_validate_obsidian_is_a_file_not_a_directory(api, tmp_path):
    root = tmp_path / "fake"
    root.mkdir()
    (root / ".obsidian").write_bytes(b"i am a file")
    assert api("/validate", root=str(root)).json()["valid"] is False


def test_validate_missing_root(api, tmp_path):
    body = api("/validate", root=str(tmp_path / "missing")).json()
    assert body["exists"] is False
    assert body["valid"] is False


def test_validate_requires_absolute_root(api):
    assert api("/validate", root="relative/dir").status_code == 400
    assert api("/validate", root="").status_code == 400


def test_validate_missing_parameter(client):
    assert client.get(f"{PREFIX}/validate").status_code == 422


def test_list_returns_sorted_posix_notes_and_skips_ignored_dirs(api, vault):
    body = api("/list", root=str(vault)).json()
    assert body["files"] == sorted(body["files"])
    assert body["count"] == len(body["files"])
    assert body["files"] == [
        "Accents.md",
        "Bob's notes & ideas.md",
        "Daily/2026-01-01.md",
        "Projects/Alpha/Plan.md",
        "Projects/Beta/Deep/Nested note.md",
        "UPPER.MD",
        "Welcome.md",
        "Windows endings.md",
        "bad-encoding.md",
    ]
    assert all("\\" not in rel for rel in body["files"])


def test_list_rejects_bad_roots(api, vault):
    assert api("/list", root=str(vault / "missing")).status_code == 404
    assert api("/list", root=str(vault / "Welcome.md")).status_code == 400
    assert api("/list", root="relative").status_code == 400


def test_read_accents_and_emoji(api, vault):
    response = api("/read", root=str(vault), path="Accents.md")
    assert response.status_code == 200
    assert (
        response.json()["content"] == "Caf\u00e9 \u00e0 la cr\u00e8me \u2615 \U0001f600 \u65e5\u672c\u8a9e\n"
    )


def test_read_preserves_crlf(api, vault):
    body = api("/read", root=str(vault), path="Windows endings.md").json()
    assert body["content"] == "line one\r\nline two\r\n"
    assert body["size"] == len(b"line one\r\nline two\r\n")


def test_read_name_with_spaces_and_apostrophe(api, vault):
    body = api("/read", root=str(vault), path="Bob's notes & ideas.md").json()
    assert body["content"] == "apostrophe and spaces"
    assert body["path"] == "Bob's notes & ideas.md"


def test_read_nested_note_with_spaces_and_backslash_separators(api, vault):
    expected = "nested"
    for path in ("Projects/Beta/Deep/Nested note.md", "Projects\\Beta\\Deep\\Nested note.md"):
        response = api("/read", root=str(vault), path=path)
        assert response.status_code == 200, path
        assert response.json()["content"] == expected


def test_read_invalid_utf8_is_415(api, vault):
    assert api("/read", root=str(vault), path="bad-encoding.md").status_code == 415


def test_read_missing_note_is_404(api, vault):
    assert api("/read", root=str(vault), path="nope.md").status_code == 404
    assert api("/read", root=str(vault), path="Projects").status_code == 400


def test_read_non_markdown_and_ignored_folders_are_refused(api, vault):
    assert api("/read", root=str(vault), path="Projects/Alpha/data.txt").status_code == 400
    assert api("/read", root=str(vault), path=".obsidian/app.json").status_code == 403
    assert api("/read", root=str(vault), path=".obsidian/notes-in-config.md").status_code == 403
    assert api("/read", root=str(vault), path=".trash/deleted.md").status_code == 403
    assert api("/read", root=str(vault), path="node_modules/pkg/readme.md").status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "../secret.md",
        "../../secret.md",
        "Projects/../../secret.md",
        "Projects/Alpha/../../../secret.md",
        "..\\secret.md",
        "Projects\\..\\..\\secret.md",
        "..%2fsecret.md",
        "%2e%2e/secret.md",
        "./Welcome.md",
        "Projects//Alpha/Plan.md",
        "/etc/passwd.md",
        "/secret.md",
        "\\secret.md",
        "\\\\server\\share\\secret.md",
        "//server/share/secret.md",
        "C:\\Windows\\win.ini.md",
        "C:/Windows/win.ini.md",
        "c:secret.md",
        "Welcome.md::$DATA",
        "Welcome.md\x00.txt",
        "Welcome.md ",
        "Welcome.md.",
        "",
    ],
)
def test_read_rejects_traversal_and_absolute_paths(api, vault, path):
    response = api("/read", root=str(vault), path=path)
    assert 400 <= response.status_code < 500, (path, response.status_code)
    assert "TOP SECRET" not in response.text


def test_read_rejects_absolute_path_pointing_at_a_real_file(api, vault, tmp_path):
    secret = tmp_path / "secret.md"
    assert secret.is_file()
    response = api("/read", root=str(vault), path=str(secret))
    assert 400 <= response.status_code < 500
    assert "TOP SECRET" not in response.text


def test_read_url_encoded_traversal_sent_raw(client, vault):
    response = client.get(f"{PREFIX}/read?root={vault}&path=..%2f..%2fsecret.md")
    assert 400 <= response.status_code < 500
    assert "TOP SECRET" not in response.text


def test_read_rejects_symlinked_file_escaping_root(api, vault, tmp_path):
    _symlink_or_skip(vault / "escape.md", tmp_path / "secret.md", is_dir=False)
    response = api("/read", root=str(vault), path="escape.md")
    assert response.status_code == 403
    assert "TOP SECRET" not in response.text


def test_read_rejects_symlinked_directory_escaping_root(api, vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_bytes(b"TOP SECRET")
    _symlink_or_skip(vault / "linked", outside, is_dir=True)
    response = api("/read", root=str(vault), path="linked/secret.md")
    assert response.status_code == 403
    assert "TOP SECRET" not in response.text


def test_list_skips_escaping_symlinks(api, vault, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_bytes(b"TOP SECRET")
    _symlink_or_skip(vault / "linked", outside, is_dir=True)
    _symlink_or_skip(vault / "escape.md", tmp_path / "secret.md", is_dir=False)
    files = api("/list", root=str(vault)).json()["files"]
    assert "escape.md" not in files
    assert not any(f.startswith("linked/") for f in files)


def test_read_allows_symlink_staying_inside_root(api, vault):
    _symlink_or_skip(vault / "alias.md", vault / "Welcome.md", is_dir=False)
    response = api("/read", root=str(vault), path="alias.md")
    assert response.status_code == 200
    assert response.json()["content"] == "# Welcome\n"
    assert "alias.md" in api("/list", root=str(vault)).json()["files"]


def test_read_requires_both_parameters(client, vault):
    assert client.get(f"{PREFIX}/read", params={"root": str(vault)}).status_code == 422
    assert client.get(f"{PREFIX}/read", params={"path": "Welcome.md"}).status_code == 422


def test_backend_source_has_no_shell_or_subprocess_use():
    tree = ast.parse(API_FILE.read_text(encoding="utf-8"))
    imported = set()
    attribute_calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            attribute_calls.add(f"{node.value.id}.{node.attr}")
    assert not imported & {"subprocess", "shlex", "pty", "ctypes"}
    assert not attribute_calls & {"os.system", "os.popen", "os.exec", "os.spawn"}


@pytest.mark.parametrize("name", ["CON.md", "NUL.md", "COM1.md", "LPT1.md", "aux.md"])
def test_read_windows_reserved_device_names_never_return_content(api, vault, name):
    response = api("/read", root=str(vault), path=name)
    assert 400 <= response.status_code < 500


def test_root_accepts_forward_slashes_and_trailing_separator(api, vault):
    spellings = [str(vault).replace("\\", "/"), str(vault) + os.sep, str(vault).replace("\\", "/") + "/"]
    for root in spellings:
        assert api("/validate", root=root).json()["valid"] is True, root
        assert "Welcome.md" in api("/list", root=root).json()["files"], root
        assert api("/read", root=root, path="Welcome.md").json()["content"] == "# Welcome\n", root
