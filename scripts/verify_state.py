#!/usr/bin/env python3
import json

d = json.load(open("/app/data/store/strategies.json"))
xs = d if isinstance(d, list) else list(d.values())
print("=== Surge ===")
for s in xs:
    if "Cursor-Surge" in (s.get("name") or ""):
        print(
            s.get("name"),
            "enabled=", s.get("enabled"),
            "status=", s.get("status"),
            "lifecycle=", s.get("lifecycle"),
            "reason=", s.get("disabled_reason"),
        )
print("=== enabled book ===")
for s in xs:
    if s.get("enabled"):
        p = s.get("params") or {}
        print(" ", s.get("name"), "w=", p.get("portfolio_weight"))
