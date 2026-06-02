"""Node registry: which machines have joined this baton instance, in which role,
and whether they passed doctor (a node is inert until doctor_ok)."""
import json, os


def _path(home, hostname):
    return os.path.join(home, "nodes", f"{hostname}.json")


def register(home, *, hostname, role, doctor_ok, created_at):
    os.makedirs(os.path.join(home, "nodes"), exist_ok=True)
    rec = {"hostname": hostname, "role": role, "doctor_ok": bool(doctor_ok), "registered_at": created_at}
    with open(_path(home, hostname), "w") as f:
        json.dump(rec, f, indent=2, sort_keys=True)
    return rec


def read_all(home):
    d = os.path.join(home, "nodes")
    if not os.path.isdir(d):
        return []
    return [json.load(open(os.path.join(d, f))) for f in sorted(os.listdir(d)) if f.endswith(".json")]


def runnable(home, hostname):
    """A worker may run jobs only if it is registered AND passed doctor."""
    p = _path(home, hostname)
    return os.path.exists(p) and json.load(open(p)).get("doctor_ok") is True
