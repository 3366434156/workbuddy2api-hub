"""区域条目落地时迁走旧的无后缀条目 + 中文显示名。

背景（2026-10-17 用户报告「选了国内版，消耗的还是国际版」）：旧版一键配置
只写一条无后缀 wb-proxy（launcher_key，不绑区域，请求跟随面板切换路由）。
新版配上 WB代理-国内版 后旧条目还留在客户端里，下拉里选的还是它——继续
「跟随面板」，看起来就是区域失灵。修两件事：

  1. 显示名用中文：WB代理-国内版 / WB代理-国际版（id/凭据引用仍是机器
     后缀 wb-proxy-cn / WB_PROXY_API_KEY_CN——文件键和环境变量名读不懂
     中文）；无后缀单条目叫 WB代理。
  2. 带区域后缀的条目落地时，把旧的无后缀条目从各客户端配置里删掉
     （codex 的 [model_providers.wb-proxy]、DSH 补丁层 providers 里的
     wb-proxy 块、legacy settings.yaml 的行内键、opencode/crush/zcode 的
     JSON 键）；agent-default-model 还指着旧 id 时跟着指到新条目。

Run with the current interpreter (python tests/run_all.py batch).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_agents as A


def apply_dsh_patch(home, label, model=None, patch_text=None):
    """对隔离 home 里的 DSH 补丁层跑一次 plan（不落盘，返回文件内容）。

    上游语义：只写**已存在**的补丁文件（绝不新建空档案），无补丁时回落
    settings.yaml。测试要打补丁层就先放一个文件进去。
    """
    profiles = os.path.join(home, ".dsh", "profiles", "desktop")
    os.makedirs(profiles, exist_ok=True)
    if patch_text is not None:
        with open(os.path.join(profiles, "cordis.patch.yml"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write(patch_text)
    return A.plan("dsh", "http://127.0.0.1:8788", "sk-test", home=home,
                  model=model, provider_label=label)


PATCH_WITH_LEGACY = '''# patch
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      wb-proxy:
        displayName: WB Proxy
        apiKeyEnv: WB_PROXY_API_KEY
      mine:
        displayName: keep-me
        apiKeyEnv: MINE_API_KEY
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: wb-proxy
    model: glm-5.3
'''


class ChineseNamesTests(unittest.TestCase):
    def test_scoped_names_are_chinese(self):
        home = tempfile.mkdtemp()
        for label, want, mid, ref in (
                ("cn", "WB代理-国内版", "wb-proxy-cn", "WB_PROXY_API_KEY_CN"),
                ("intl", "WB代理-国际版", "wb-proxy-intl",
                 "WB_PROXY_API_KEY_INTL")):
            r = apply_dsh_patch(home, label, patch_text="[]\n")
            patch = next(f for f in r["files"]
                         if f["path"].endswith("cordis.patch.yml"))
            self.assertIn("displayName: " + want, patch["content"])
            self.assertIn("      " + mid + ":", patch["content"])
            self.assertIn(ref, patch["content"])

    def test_unscoped_name(self):
        home = tempfile.mkdtemp()
        r = apply_dsh_patch(home, None, patch_text="[]\n")
        patch = next(f for f in r["files"]
                     if f["path"].endswith("cordis.patch.yml"))
        self.assertIn("displayName: WB代理", patch["content"])
        self.assertIn("      wb-proxy:", patch["content"],
                      "无后缀条目仍写进 wb-proxy 键")

    def test_custom_label_falls_back(self):
        home = tempfile.mkdtemp()
        r = apply_dsh_patch(home, "lab", patch_text="[]\n")
        patch = next(f for f in r["files"]
                     if f["path"].endswith("cordis.patch.yml"))
        self.assertIn("displayName: WB代理-lab", patch["content"])


class DshPatchMigrationTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        profiles = os.path.join(self.home, ".dsh", "profiles", "desktop")
        os.makedirs(profiles, exist_ok=True)
        self.patch_path = os.path.join(profiles, "cordis.patch.yml")
        with open(self.patch_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(PATCH_WITH_LEGACY)

    def read(self):
        with open(self.patch_path, encoding="utf-8") as fh:
            return fh.read()

    def _apply(self, label="cn", model=None):
        r = apply_dsh_patch(self.home, label, model=model)
        patch = next(f for f in r["files"]
                     if f["path"].endswith("cordis.patch.yml"))
        with open(self.patch_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(patch["content"])

    def test_legacy_entry_removed_on_scoped_apply(self):
        self._apply("cn")
        text = self.read()
        self.assertNotIn("      wb-proxy:", text,
                         "旧无后缀条目必须被迁走（launcher_key 跟面板路由）")
        self.assertIn("      wb-proxy-cn:", text)
        self.assertIn("      mine:", text, "用户自己的条目不动")

    def test_default_model_follows_new_provider(self):
        self._apply("cn")
        text = self.read()
        self.assertIn('provider: "wb-proxy-cn"', text,
                      "agent-default-model 不能再指着已删除的 wb-proxy")

    def test_no_removal_when_applying_unscoped(self):
        # 无后缀应用（单条目模式）写的就是 wb-proxy，绝不能自删。
        self._apply(None)
        text = self.read()
        self.assertIn("      wb-proxy:", text)
        self.assertNotIn("      wb-proxy-cn:", text)


class CodexMigrationTests(unittest.TestCase):
    def test_legacy_table_dropped(self):
        d = tempfile.mkdtemp()
        os.environ["CODEX_HOME"] = os.path.join(d, ".codex")
        self.addCleanup(os.environ.pop, "CODEX_HOME", None)
        cfg = os.path.join(d, ".codex", "config.toml")
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('model_provider = "wb-proxy"\n'
                     '[model_providers.wb-proxy]\n'
                     'name = "WB Proxy"\n'
                     'base_url = "http://old/v1"\n')
        r = A.plan("codex", "http://127.0.0.1:8788", "sk-test", home=d,
                   provider_label="cn")
        text = next(f["content"] for f in r["files"]
                    if f["path"] == cfg)
        self.assertNotIn("[model_providers.wb-proxy]", text)
        self.assertIn("[model_providers.wb-proxy-cn]", text)
        self.assertIn('model_provider = "wb-proxy-cn"', text)


class JsonClientMigrationTests(unittest.TestCase):
    def test_opencode_legacy_provider_dropped(self):
        import json
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, ".config", "opencode", "opencode.json")
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"provider": {
                "wb-proxy": {"name": "WB Proxy"},
                "other": {"name": "keep"}}}, fh)
        r = A.plan("opencode", "http://127.0.0.1:8788", "sk-test", home=d,
                   provider_label="cn")
        data = json.loads(next(f["content"] for f in r["files"]
                               if f["path"] == cfg))
        self.assertNotIn("wb-proxy", data["provider"])
        self.assertIn("wb-proxy-cn", data["provider"])
        self.assertIn("other", data["provider"])

    def test_zcode_legacy_rule_dropped(self):
        import json
        d = tempfile.mkdtemp()
        cfg = os.path.join(d, ".zcode", "v2", "provider_config.json")
        os.makedirs(os.path.dirname(cfg), exist_ok=True)
        with open(cfg, "w", encoding="utf-8") as fh:
            json.dump({"schemaVersion": 1, "config": {
                "providerOrder": ["wb-proxy", "other"],
                "providerConfigRules": {"providerRules": [
                    {"providerId": "wb-proxy"},
                    {"providerId": "other"}]}}}, fh)
        r = A.plan("zcode", "http://127.0.0.1:8788", "sk-test", home=d,
                   provider_label="cn")
        data = json.loads(next(f["content"] for f in r["files"]
                               if f["path"] == cfg))
        ids = [r["providerId"] for r in
               data["config"]["providerConfigRules"]["providerRules"]]
        self.assertNotIn("wb-proxy", ids)
        self.assertIn("wb-proxy-cn", ids)
        self.assertNotIn("wb-proxy", data["config"]["providerOrder"])
        self.assertIn("other", data["config"]["providerOrder"])


if __name__ == "__main__":
    unittest.main()
