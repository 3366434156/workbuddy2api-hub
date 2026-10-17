"""一键配置的模型清单按出口过滤：国内 Key 不该配出 gpt-* 条目。

两个问题，一个根因：_agents_models() 原来只取 fetch_models()（跟随面板当前
的区域开关），面板停在国内版时国际独占模型整排消失，两把 Key 各配一次写出
的清单一模一样；apply 侧也不筛，国内条目里带着只有国际出口才认的模型，用户
选了每个请求都白吃一轮 4xx。

修好后：

  1. _agents_models 两个区域各取一份合并，单出口模型带 `realms` 标注，
     两边都服务的不标（通用）；
  2. /agents 的 keys 条目带 `realm`；
  3. apply 按 provider_label（即所选 Key 的出口）过滤模型清单，默认模型
     不被该出口服务时回落到过滤后清单的第一个，并写入 warnings。

Run with the current interpreter (python tests/run_all.py batch).
"""
import os
import sys
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P


def entry(mid, realms=None):
    e = {"id": mid}
    if realms:
        e["realms"] = realms
    return e


def make_handler(models, key_realm="cn", use_real_models=False):
    h = type("H", (object,), {})()
    if use_real_models:
        # 绑定真方法：本类要测的就是 Handler._agents_models 本身。
        h._agents_models = P.Handler._agents_models.__get__(h)
    else:
        h._agents_models = lambda *a, **k: list(models)
    h._agents_resolve_key = lambda payload, warnings: ("sk-test", key_realm)
    h._json = lambda code, obj: (code, obj)
    h._error = lambda code, msg, *a: (code, {"error": {"message": msg}})
    return h


def run_apply(h, payload):
    """走真 _handle_agents_apply，拦下 wb_agents.integrate 看收到的 models。"""
    captured = {}

    class FakeAgents:
        AgentConfigError = type("AgentConfigError", (Exception,), {})

        @staticmethod
        def integrate(accounts_dir, client_id, base_url, api_key,
                      model=None, models=None, provider_label=None):
            captured["models"] = models
            captured["model"] = model
            captured["provider_label"] = provider_label
            return {"ok": True, "provider_id": "wb-proxy",
                    "files": [{"path": "x", "content": "", "existed": False}]}

    fake_mod = types.ModuleType("wb_agents")
    fake_mod.AgentConfigError = FakeAgents.AgentConfigError
    fake_mod.integrate = FakeAgents.integrate

    with mock.patch.object(P, "agents_module", lambda: fake_mod), \
            mock.patch.object(P, "auth_required", lambda: True), \
            mock.patch.object(P, "ACCOUNTS_DIR", "/tmp/wb-test-agents"):
        resp = P.Handler._handle_agents_apply(h, dict(payload))
    return resp, captured


class AgentsModelsRealmAnnotation(unittest.TestCase):
    def test_merged_from_both_realms_with_annotations(self):
        def fake_fetch(realm=None):
            return {"intl": [("gpt-5.5", {}), ("glm-5.3", {})],
                    "cn": [("glm-5.3", {}), ("minimax-m3", {})]}[realm]
        h = make_handler([], use_real_models=True)
        with mock.patch.object(P, "fetch_models", side_effect=fake_fetch):
            out = h._agents_models()
        by_id = {m["id"]: m for m in out}
        self.assertEqual(by_id["gpt-5.5"]["realms"], ["intl"])
        self.assertEqual(by_id["minimax-m3"]["realms"], ["cn"])
        self.assertNotIn("realms", by_id["glm-5.3"], "两边都服务的是通用模型，不标")

    def test_static_fallback_when_both_live_sources_empty(self):
        h = make_handler([], use_real_models=True)
        with mock.patch.object(P, "fetch_models", side_effect=Exception("down")), \
                mock.patch.object(P.wb_catalog, "STATIC_INTL_MODELS",
                                  [entry("gpt-5.5")]), \
                mock.patch.object(P.wb_catalog, "STATIC_CN_MODELS",
                                  [entry("glm-5.3")]):
            out = h._agents_models()
        by_id = {m["id"]: m for m in out}
        self.assertEqual(by_id["gpt-5.5"]["realms"], ["intl"])
        self.assertEqual(by_id["glm-5.3"]["realms"], ["cn"])


class AgentsKeysCarryRealm(unittest.TestCase):
    def test_get_agents_keys_include_realm(self):
        h = type("H", (object,), {})()
        h._get_agents = P.Handler._get_agents.__get__(h)
        h._agents_models = lambda *a, **k: []
        h._agents_client_allowed = lambda: True
        h._agents_base_url_hint = lambda: "http://127.0.0.1:8788/v1"
        h._json = lambda code, obj: obj
        with mock.patch.object(P, "configured_keys", lambda: [
                {"id": "k1", "name": "国内", "realm": "cn", "enabled": True},
                {"id": "k2", "name": "国际", "realm": "intl", "enabled": True}]), \
                mock.patch.object(P, "API_KEY", "sk"), \
                mock.patch.object(P, "auth_required", lambda: True):
            import wb_settings
            with mock.patch.object(wb_settings, "agent_apply_prefs",
                                   lambda d: {}):
                out = h._get_agents()
        realms = {k["id"]: k["realm"] for k in out["keys"]}
        self.assertEqual(realms, {"k1": "cn", "k2": "intl"})


class ApplyFiltersModelsByExit(unittest.TestCase):
    MODELS = [entry("gpt-5.5", ["intl"]), entry("glm-5.3"),
              entry("minimax-m3", ["cn"])]

    def test_cn_key_drops_intl_only_models(self):
        h = make_handler(self.MODELS, key_realm="cn")
        resp, cap = run_apply(h, {
            "client": "codex", "base_url": "http://127.0.0.1:8788",
            "models": ["gpt-5.5", "glm-5.3", "minimax-m3"],
        })
        ids = [m["id"] for m in cap["models"]]
        self.assertNotIn("gpt-5.5", ids, "国内出口不该带上国际独占模型")
        self.assertIn("minimax-m3", ids)
        self.assertIn("glm-5.3", ids, "通用模型保留")

    def test_intl_key_drops_cn_only_models(self):
        h = make_handler(self.MODELS, key_realm="intl")
        resp, cap = run_apply(h, {
            "client": "codex", "base_url": "http://127.0.0.1:8788",
            "models": ["gpt-5.5", "glm-5.3", "minimax-m3"],
        })
        ids = [m["id"] for m in cap["models"]]
        self.assertNotIn("minimax-m3", ids)
        self.assertIn("gpt-5.5", ids)

    def test_default_model_falls_back_when_not_served(self):
        h = make_handler(self.MODELS, key_realm="cn")
        resp, cap = run_apply(h, {
            "client": "codex", "base_url": "http://127.0.0.1:8788",
            "model": "gpt-5.5",
            "models": ["gpt-5.5", "glm-5.3", "minimax-m3"],
        })
        self.assertEqual(cap["model"], "glm-5.3",
                         "默认模型不被国内出口服务时回落到清单第一个国内模型")

    def test_no_label_keeps_everything(self):
        # 出口未知（全局 Key、无绑定）：不筛，通用+独占都保留。
        h = make_handler(self.MODELS, key_realm="")
        resp, cap = run_apply(h, {
            "client": "codex", "base_url": "http://127.0.0.1:8788",
            "models": ["gpt-5.5", "glm-5.3", "minimax-m3"],
        })
        self.assertEqual(len(cap["models"]), 3)

    def test_invalid_label_still_400(self):
        h = make_handler(self.MODELS, key_realm="")
        resp, _ = run_apply(h, {
            "client": "codex", "base_url": "http://127.0.0.1:8788",
            "provider_label": "nope",
            "models": ["glm-5.3"],
        })
        self.assertEqual(resp[0], 400)


if __name__ == "__main__":
    unittest.main()
