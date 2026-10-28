#!/usr/bin/env python3
"""Three-way merge of dn-ledger.json so overlapping runs never lose each other's work.
usage: merge_ledger.py BASE LOCAL REMOTE OUT
  BASE   = the ledger this run started from (git HEAD at checkout)
  LOCAL  = the ledger this run wrote
  REMOTE = the ledger now on origin/main (another run may have pushed meanwhile)
Rule: what this run changed wins for the keys it changed; everything else comes from REMOTE.
"""
import json
import sys


def load(p):
    try:
        with open(p) as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def main(base_p, local_p, remote_p, out_p):
    base, local, remote = load(base_p), load(local_p), load(remote_p)
    out = dict(remote)
    # invoice entries and any other plain keys: this run's version wins only where it changed something
    for k, v in local.items():
        if k.startswith("_"):
            continue
        if base.get(k) != v:
            out[k] = v
    # _runs: union, keyed by time + files
    def rkey(r):
        return (r.get("at", ""), ",".join(r.get("files", [])))
    runs = {rkey(r): r for r in remote.get("_runs", [])}
    for r in local.get("_runs", []):
        runs.setdefault(rkey(r), r)
    out["_runs"] = sorted(runs.values(), key=rkey)[-300:]
    # _held: start from remote, add what this run added, drop what this run cleared
    def hkey(h):
        return (h.get("file", ""), h.get("page", ""))
    base_h = {hkey(h) for h in base.get("_held", [])}
    local_h = {hkey(h): h for h in local.get("_held", [])}
    cleared = base_h - set(local_h)
    held = {hkey(h): h for h in remote.get("_held", []) if hkey(h) not in cleared}
    for k, h in local_h.items():
        if k not in base_h:
            held[k] = h
    out["_held"] = sorted(held.values(), key=lambda h: h.get("at", ""))
    # credit notes attached: union
    out["_cn_attached"] = sorted(set(remote.get("_cn_attached", [])) | set(local.get("_cn_attached", [])))
    # last email: the latest
    out["_last_email"] = max(remote.get("_last_email", ""), local.get("_last_email", ""))
    with open(out_p, "w") as fh:
        json.dump(out, fh, indent=1, sort_keys=True)
    print(f"merged: {len([k for k in out if not k.startswith('_')])} invoices, {len(out['_runs'])} runs, {len(out['_held'])} held")


if __name__ == "__main__":
    main(*sys.argv[1:5])
