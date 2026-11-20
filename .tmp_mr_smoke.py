# -*- coding: utf-8 -*-
"""临时冒烟脚本（不属于交付物，验证跑完即删）。"""
import json
import os
import pathlib
import shutil
import tempfile

tmp = tempfile.mkdtemp(prefix="mr_smoke_")
os.environ["MYLOCALAI_MODELS_PATH"] = os.path.join(tmp, "models.json")
os.environ["MYLOCALAI_MODEL_SECRETS_PATH"] = os.path.join(tmp, "secrets.json")
os.environ["MYLOCALAI_PRIVACY_PREFERENCES_PATH"] = os.path.join(tmp, "prefs.json")

from mylocalai.llm import model_registry as r

v = r.view()
print("warnings:", v["warnings"])
print("active:", v["active"])
print("runtime:", v["runtime"])
print("providers:", [(p["id"], p["builtin"], p["is_active"], [m["id"] for m in p["models"]])
                    for p in v["providers"]])
print("default models:", r.default_model_views("deepseek"))
print("key_status:", r.key_status("deepseek"))

pv = r.upsert_provider("acme", {"base_url": "https://api.acme.test/v1",
                                "display_name": "Acme",
                                "api_key": "sk-test-1234567890abcd4f2a"})
print("upsert:", pv["id"], pv["api_key_set"], pv["api_key_source"], pv["api_key_masked"], pv["models"])

mv = r.upsert_model("acme", {
    "id": "acme-x", "context_window": 128000, "max_output_tokens": 8192,
    "input_types": ["text", "image"],
    "reasoning": {"enabled": True, "active_tier": "low",
                  "tiers": [{"tier": "off", "api_value": None}, {"tier": "low", "api_value": "low"}]}})
print("models:", [m["id"] for m in mv["models"]])
print("activate:", r.set_active("acme", "acme-x")["is_active"], r.active())
print("runtime:", {k: val for k, val in r.runtime_resolution().items() if k != "api_key"})

models_file = pathlib.Path(tmp) / "models.json"
print("models.json has plaintext key?", "abcdef" in models_file.read_text(encoding="utf-8"))
print("view json has plaintext key?", "sk-test-1234" in json.dumps(r.view(), ensure_ascii=False))
print("files:", sorted(f.name for f in pathlib.Path(tmp).iterdir()))
print("models.json head:", models_file.read_text(encoding="utf-8")[:200].replace("\n", " "))
print("secrets:", (pathlib.Path(tmp) / "secrets.json").read_text(encoding="utf-8").replace("\n", " "))

# 未授权 discover
try:
    r.available_models("acme")
except Exception as exc:
    print("discover unauthorized:", type(exc).__name__, exc)
print("discover unknown:", r.available_models("nope"))

# restore / delete
print("restore deepseek:", [m["id"] for m in r.restore_default_models("deepseek")])
print("delete model:", r.delete_model("deepseek", "deepseek-chat"), r.provider_view("deepseek")["models"])
print("restore again:", [m["id"] for m in r.restore_default_models("deepseek")])
try:
    r.delete_provider("deepseek")
except Exception as exc:
    print("delete builtin:", type(exc).__name__, exc)

# 坏 JSON fail-soft
models_file.write_text("{broken", encoding="utf-8")
r.reset_cache()
v2 = r.view()
print("bad json warnings:", v2["warnings"])
print("bad json providers:", [p["id"] for p in v2["providers"]])
print("file preserved:", models_file.read_text(encoding="utf-8"))
shutil.rmtree(tmp, ignore_errors=True)
