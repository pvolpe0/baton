import json, re
from lib import manifest


def test_new_id_is_unique_and_well_formed():
    a = manifest.new_id(now="20260601T1432Z")
    assert re.fullmatch(r"20260601T1432Z-[0-9a-f]{4}", a)
    assert a != manifest.new_id(now="20260601T1432Z")  # random suffix differs


def test_build_has_seam_fields():
    m = manifest.build(id="20260601T1432Z-a3f9", project="example", model="sonnet",
                       effort="medium", mode="fresh", repos=[], created_at="2026-06-01T14:32:00Z")
    assert m["capabilities"] == []
    assert m["model"] == "sonnet" and m["effort"] == "medium" and m["mode"] == "fresh"


def test_roundtrip(tmp_path):
    m = manifest.build(id="x-0001", project="example", model="opus", effort="high", mode="continue",
                       repos=[{"repo": "api", "wip_branch": "wip/handoff-x-0001", "base_sha": "abc"}],
                       created_at="2026-06-01T00:00:00Z")
    p = tmp_path / "manifest.json"
    manifest.write(str(p), m)
    assert manifest.read(str(p)) == m
    assert json.loads(p.read_text())["repos"][0]["repo"] == "api"
