#!/usr/bin/env python3
"""Local stdin/JSON fixture, never a model client."""

import json
import sys


prompt = sys.stdin.read()
if prompt == "invalid":
    print("{broken")
    raise SystemExit(1)
failed = prompt == "paid_error"
print(json.dumps({"result": None if failed else prompt, "is_error": failed,
                  "total_cost_usd": 0.01,
                  "usage": {"input_tokens": 1, "cache_creation_input_tokens": 2,
                            "cache_read_input_tokens": 3, "output_tokens": 4}}))
raise SystemExit(1 if failed else 0)
