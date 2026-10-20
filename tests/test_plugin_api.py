import ast
import importlib.util
import os
import sys
import threading
import time
from collections import Counter
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

API_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/pairdown"
IS_WINDOWS = sys.platform == "win32"
# Windows and macOS volumes are case-insensitive by default; Linux is not.
CASE_INSENSITIVE = sys.platform in ("win32", "darwin")
OUTSIDE = b"OUTSIDE ROOT SENTINEL"
INSIDE = b"INSIDE ROOT CONTENT"


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
def plugin_api():
    return _load_plugin_api()


@pytest.fixture(scope="session")
def client(plugin_api):
    app = FastAPI()
    app.include_router(plugin_api.router, prefix=PREFIX)
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
    assert not imported & {"subprocess", "shlex", "pty"}
    assert not attribute_calls & {"os.system", "os.popen", "os.exec", "os.spawn"}
    # ctypes and msvcrt are stdlib and are used only to ask Windows which file a
    # handle really refers to; they must stay confined to that helper.
    module_level = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            module_level.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            module_level.add(node.module.split(".")[0])
    assert not module_level & {"ctypes", "msvcrt"}


@pytest.mark.parametrize("name", ["CON.md", "NUL.md", "COM1.md", "LPT1.md", "aux.md"])
def test_read_windows_reserved_device_names_never_return_content(api, vault, name):
    response = api("/read", root=str(vault), path=name)
    assert 400 <= response.status_code < 500


@pytest.mark.parametrize("name", ["COM¹.md", "lpt²", "Com³.txt", "COM0", "nul.tar.gz", "conin$"])
def test_device_name_pattern_matches_reserved_names(plugin_api, name):
    assert plugin_api._WINDOWS_DEVICE_NAME.match(name)


@pytest.mark.parametrize("name", ["COM10.md", "console.md", "COM⁴.md", "com.md", "aux2.md", "notes.md"])
def test_device_name_pattern_ignores_ordinary_names(plugin_api, name):
    assert not plugin_api._WINDOWS_DEVICE_NAME.match(name)


def test_root_accepts_forward_slashes_and_trailing_separator(api, vault):
    spellings = [str(vault).replace("\\", "/"), str(vault) + os.sep, str(vault).replace("\\", "/") + "/"]
    for root in spellings:
        assert api("/validate", root=root).json()["valid"] is True, root
        assert "Welcome.md" in api("/list", root=root).json()["files"], root
        assert api("/read", root=root, path="Welcome.md").json()["content"] == "# Welcome\n", root


# --- Case-insensitive filesystems: ignored folders match regardless of spelling ---


def _short_name(path: Path) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return buffer.value


def test_ignored_folder_names_follow_the_filesystem_case_rules(api, tmp_path):
    root = tmp_path / "casevault"
    spellings = ["Node_Modules", ".Obsidian", ".TRASH"]
    for spelling in spellings:
        (root / spelling).mkdir(parents=True)
        (root / spelling / "hidden.md").write_bytes(b"hidden")
    (root / "visible.md").write_bytes(b"visible")

    files = api("/list", root=str(root)).json()["files"]
    if CASE_INSENSITIVE:
        assert files == ["visible.md"]
    else:
        # Linux: these are ordinary folders that merely look like ignored ones.
        assert files == sorted(["visible.md"] + [f"{s}/hidden.md" for s in spellings])

    for spelling in spellings:
        for variant in (spelling, spelling.upper(), spelling.lower()):
            response = api("/read", root=str(root), path=f"{variant}/hidden.md")
            if CASE_INSENSITIVE:
                assert response.status_code == 403, variant
                assert b"hidden" not in response.content
            elif variant == spelling:
                assert response.status_code == 200, variant


def test_read_refuses_mixed_case_spelling_of_existing_ignored_folders(api, vault):
    # The fixture created lower-case .obsidian, .trash and node_modules.
    for path in (".OBSIDIAN/notes-in-config.md", ".Trash/deleted.md", "NODE_MODULES/pkg/readme.md"):
        response = api("/read", root=str(vault), path=path)
        if CASE_INSENSITIVE:
            assert response.status_code == 403, path
            assert b"config note" not in response.content
        else:
            assert response.status_code == 404, path


@pytest.mark.skipif(not IS_WINDOWS, reason="8.3 short names exist only on NTFS")
def test_read_refuses_short_name_alias_of_ignored_folder(api, vault):
    short = _short_name(vault / "node_modules")
    alias = Path(short).name
    if alias.lower() == "node_modules":
        pytest.skip("8.3 short names are disabled on this volume")
    response = api("/read", root=str(vault), path=f"{alias}/pkg/readme.md")
    assert response.status_code in (403, 404), response.text
    assert b"dependency readme" not in response.content


# --- Containment is bound to the file that was actually opened ---


def _link_dir(target: Path, link: Path):
    """Create a directory link: a symlink, or an NTFS junction when symlinks are unavailable."""
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        if not IS_WINDOWS:
            raise
        import _winapi

        _winapi.CreateJunction(str(target), str(link))


def _remove_dir_link(link: Path):
    try:
        os.unlink(link)
    except OSError:
        os.rmdir(link)


@pytest.fixture()
def race_area(tmp_path):
    root = tmp_path / "raceroot"
    root.mkdir()
    (root / "safe.md").write_bytes(INSIDE)
    (root / "real").mkdir()
    (root / "real" / "note.md").write_bytes(INSIDE)
    outside = tmp_path / "raceoutside"
    outside.mkdir()
    (outside / "secret.md").write_bytes(OUTSIDE)
    (outside / "note.md").write_bytes(OUTSIDE)
    _symlink_or_skip(tmp_path / "probe-link", outside / "secret.md", is_dir=False)
    (tmp_path / "probe-link").unlink()
    return root, outside


class _Swapper(threading.Thread):
    """Runs step() in a tight loop until stopped; step() mutates the tree under the server."""

    def __init__(self, step):
        super().__init__(daemon=True)
        self.step = step
        self.halt = threading.Event()
        self.rounds = 0

    def run(self):
        while not self.halt.is_set():
            try:
                self.step()
                self.rounds += 1
            except OSError:
                # Windows refuses to replace a file or folder the server holds open.
                pass

    def stop(self):
        self.halt.set()
        self.join(timeout=10)


def _file_swap_step(root: Path, outside: Path):
    flip, pending = root / "flip.md", root / "pending.tmp"

    def settle(make):
        if pending.is_symlink() or pending.exists():
            pending.unlink()
        make()
        os.replace(pending, flip)

    def step():
        settle(lambda: os.symlink(outside / "secret.md", pending))
        settle(lambda: os.link(root / "safe.md", pending))

    os.link(root / "safe.md", flip)
    return step


def _parent_swap_step(root: Path, outside: Path):
    """Alternate root/sub between a real folder and a link to a folder outside the root."""
    sub, keep, away = root / "sub", root / "sub-keep", root / "sub-link"
    sub.mkdir()
    (sub / "note.md").write_bytes(INSIDE)
    _link_dir(outside, away)

    def step():
        if os.path.lexists(keep):  # a previous round was interrupted
            if os.path.lexists(sub):
                os.rename(sub, away)
            os.rename(keep, sub)
        os.rename(sub, keep)
        try:
            os.rename(away, sub)
            os.rename(sub, away)
        finally:
            os.rename(keep, sub)

    return step


def _hammer(client, root: Path, swapper: _Swapper, rel_path: str, budget: float = 4.0, requests: int = 6000):
    statuses = Counter()
    leaked = None
    swapper.start()
    try:
        deadline = time.monotonic() + budget
        for count in range(requests):
            response = client.get(f"{PREFIX}/read", params={"root": str(root), "path": rel_path})
            statuses[response.status_code] += 1
            if OUTSIDE.decode() in response.text:
                leaked = (count + 1, response.status_code, response.text)
                break
            if response.status_code == 200:
                assert response.json()["content"] == INSIDE.decode()
            if time.monotonic() > deadline:
                break
    finally:
        swapper.stop()
    return statuses, leaked


def test_read_never_returns_outside_bytes_while_the_file_is_swapped_for_a_symlink(client, race_area):
    root, outside = race_area
    swapper = _Swapper(_file_swap_step(root, outside))
    statuses, leaked = _hammer(client, root, swapper, "flip.md")
    assert leaked is None, f"outside bytes returned after {leaked[0]} requests: {leaked[2]}"
    assert swapper.rounds > 0, "the swapper never completed a swap, so the race was not exercised"
    assert statuses[200] > 0, statuses
    assert statuses[403] > 0, f"the escaping link was never hit, so the check was not exercised: {statuses}"
    assert set(statuses) <= {200, 403, 404}, statuses


def test_read_never_returns_outside_bytes_while_a_parent_folder_is_swapped_for_a_link(client, race_area):
    root, outside = race_area
    swapper = _Swapper(_parent_swap_step(root, outside))
    statuses, leaked = _hammer(client, root, swapper, "sub/note.md")
    assert leaked is None, f"outside bytes returned after {leaked[0]} requests: {leaked[2]}"
    assert swapper.rounds > 0, "the swapper never completed a swap, so the race was not exercised"
    assert statuses[200] > 0, statuses
    assert statuses[403] > 0, f"the escaping link was never hit, so the check was not exercised: {statuses}"
    assert set(statuses) <= {200, 403, 404}, statuses


@pytest.fixture()
def open_hook(monkeypatch):
    """Run callbacks immediately before and after the server opens one given path."""
    real_open = os.open
    state = {"target": None, "before": None, "after": None, "fired": 0}

    def hooked(path, *args, **kwargs):
        matches = state["target"] is not None and Path(os.fsdecode(path)) == state["target"]
        if matches:
            state["fired"] += 1
            if state["before"]:
                state["before"]()
        fd = real_open(path, *args, **kwargs)
        if matches and state["after"]:
            state["after"]()
        return fd

    monkeypatch.setattr(os, "open", hooked)
    return state


def _swap_in_symlink(root: Path, outside: Path):
    pending = root / "pending.tmp"
    os.symlink(outside / "secret.md", pending)
    os.replace(pending, root / "flip.md")


def test_file_replaced_by_an_escaping_symlink_just_before_open_is_refused(api, race_area, open_hook):
    root, outside = race_area
    os.link(root / "safe.md", root / "flip.md")
    resolved_root = root.resolve()
    open_hook.update(target=resolved_root / "flip.md", before=lambda: _swap_in_symlink(root, outside))
    response = api("/read", root=str(root), path="flip.md")
    assert open_hook["fired"] == 1, "the server did not open the note through os.open"
    assert response.status_code == 403
    assert OUTSIDE.decode() not in response.text


def test_file_replaced_by_an_escaping_symlink_just_after_open_returns_only_the_opened_file(api, race_area, open_hook):
    root, outside = race_area
    os.link(root / "safe.md", root / "flip.md")

    def swap_after():
        try:
            _swap_in_symlink(root, outside)
        except OSError:
            pass  # Windows keeps the opened file from being replaced, which is also safe.

    open_hook.update(target=root.resolve() / "flip.md", after=swap_after)
    response = api("/read", root=str(root), path="flip.md")
    assert open_hook["fired"] == 1, "the server did not open the note through os.open"
    assert response.status_code == 200
    assert response.json()["content"] == INSIDE.decode()


def test_parent_folder_replaced_by_an_escaping_link_just_before_open_is_refused(api, race_area, open_hook):
    root, outside = race_area
    _link_dir(root / "real", root / "sub")

    def swap_parent():
        _remove_dir_link(root / "sub")
        _link_dir(outside, root / "sub")

    open_hook.update(target=root.resolve() / "sub" / "note.md", before=swap_parent)
    response = api("/read", root=str(root), path="sub/note.md")
    assert open_hook["fired"] == 1, "the server did not open the note through os.open"
    assert response.status_code == 403
    assert OUTSIDE.decode() not in response.text


# --- Listing never walks through linked folders ---


def _baseline_listing(api, vault):
    return api("/list", root=str(vault)).json()["files"]


def test_list_does_not_follow_a_folder_link_cycle(api, vault):
    before = _baseline_listing(api, vault)
    _link_dir(vault, vault / "loop")
    _link_dir(vault, vault / "Projects" / "back")
    started = time.monotonic()
    after = _baseline_listing(api, vault)
    assert time.monotonic() - started < 10
    assert after == before


def test_list_does_not_walk_into_a_folder_link_that_points_outside(api, vault, tmp_path):
    outside = tmp_path / "outside-dir"
    (outside / "deeper").mkdir(parents=True)
    (outside / "outside.md").write_bytes(b"TOP SECRET")
    (outside / "deeper" / "more.md").write_bytes(b"TOP SECRET")
    before = _baseline_listing(api, vault)
    _link_dir(outside, vault / "linked")
    assert _baseline_listing(api, vault) == before
    response = api("/read", root=str(vault), path="linked/outside.md")
    assert response.status_code == 403
    assert b"TOP SECRET" not in response.content


def test_list_leaves_out_folder_links_that_stay_inside_root_but_read_allows_them(api, vault):
    before = _baseline_listing(api, vault)
    _link_dir(vault / "Projects", vault / "ProjectsAlias")
    assert _baseline_listing(api, vault) == before
    response = api("/read", root=str(vault), path="ProjectsAlias/Alpha/Plan.md")
    assert response.status_code == 200
    assert response.json()["content"] == "plan"


def test_read_refuses_a_link_inside_root_that_points_into_an_ignored_folder(api, vault):
    _link_dir(vault / ".obsidian", vault / "config-alias")
    response = api("/read", root=str(vault), path="config-alias/notes-in-config.md")
    assert response.status_code == 403
    assert b"config note" not in response.content


@pytest.mark.skipif(not IS_WINDOWS, reason="NTFS junctions")
def test_list_does_not_enter_a_junction_to_an_outside_folder(api, vault, tmp_path):
    import _winapi

    outside = tmp_path / "junction-outside"
    outside.mkdir()
    (outside / "outside.md").write_bytes(b"TOP SECRET")
    before = _baseline_listing(api, vault)
    _winapi.CreateJunction(str(outside), str(vault / "junction"))
    try:
        assert _baseline_listing(api, vault) == before
        response = api("/read", root=str(vault), path="junction/outside.md")
        assert response.status_code == 403
        assert b"TOP SECRET" not in response.content
    finally:
        os.rmdir(vault / "junction")


@pytest.mark.skipif(not IS_WINDOWS, reason="NTFS junctions")
def test_list_terminates_on_a_junction_cycle(api, vault):
    import _winapi

    before = _baseline_listing(api, vault)
    _winapi.CreateJunction(str(vault), str(vault / "Loop"))
    _winapi.CreateJunction(str(vault / "Projects"), str(vault / "Projects" / "Alpha" / "up"))
    try:
        started = time.monotonic()
        after = _baseline_listing(api, vault)
        assert time.monotonic() - started < 10
        assert after == before
    finally:
        os.rmdir(vault / "Loop")
        os.rmdir(vault / "Projects" / "Alpha" / "up")
