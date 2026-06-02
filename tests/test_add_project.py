import importlib.util
import os


def _load():
    """The skill helper lives under engine/skill/add-project/ (hyphen → not importable by name)."""
    p = os.path.join(os.path.dirname(__file__), "..", "engine", "skill", "add-project", "add_project.py")
    spec = importlib.util.spec_from_file_location("add_project", os.path.abspath(p))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_build_config_single_repo():
    cfg = _load().build_config(name="proj", owner="org", mac_root="/m/proj",
                               pi_root="/home/baton/work/proj", default_branch="main",
                               model="sonnet", repos=[], never_mirror=[], host="github")
    assert cfg["owner"] == "org"
    assert cfg["roots"] == {"mac": "/m/proj", "pi": "/home/baton/work/proj"}
    assert cfg["protected_branches"] == ["main"]
    assert "repos" not in cfg                 # single-repo omits the member list
    assert "denied_commands" not in cfg       # the soft denied list is global now, not per-project


def test_build_config_polyrepo():
    cfg = _load().build_config(name="proj", owner="org", mac_root="/m", pi_root="/p",
                               default_branch="dev", model="opus", repos=["a", "b"],
                               never_mirror=["b"], host="github")
    assert cfg["repos"] == ["a", "b"]
    assert cfg["never_mirror"] == ["b"]
    assert cfg["default_branch"] == "dev" and cfg["protected_branches"] == ["dev"]
    assert cfg["default_model"] == "opus"
