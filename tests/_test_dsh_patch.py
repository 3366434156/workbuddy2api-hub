"""新版 DSH（profiles/<profile>/*.patch.yml 补丁层）的一键配置。

新版 DSH 把旧 settings.yaml 迁移成 profiles 档案目录下的补丁层：一个顶层
YAML 数组，每个条目 `- id: ...` + `config:`（本机实测旧文件被改名成
settings.yaml.imported，旧版 apply 生成的 settings.yaml 新版根本不读）。
wb-proxy 要动的是两个条目：llm-pi-ai（providers.<id>：baseURL / 模型清单）
与 agent-default-model（provider / model）；API Key 走 apiKeyEnv 引用，真值
写进 .credentials.yaml 的 refs 表。

要钉住的行为：

  1. 有补丁文件时只碰补丁 + 凭据，绝不再生成一份旧 settings.yaml；
  2. 插入到既有 providers 里时，其他 provider（wawapii/gemini/glm…）的行
     逐字节保留——那是用户手写的配置；
  3. 条目缺失时追加骨架，agent-default-model 的 provider 与 model 都要更新；
  4. 没有补丁文件（旧版 DSH）时回落 legacy settings.yaml 路径。

Run with the current interpreter (python tests/run_all.py dsh).
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_agents

PATCH = '''# Your patch layer for this dsh profile, applied after every load.
- id: agent-default-model
  name: "@deepseek-ai/dsh-agent-default-model"
  config:
    provider: wb
    model: hy4-preview-f
    reasoningEffort: max
- id: llm-pi-ai
  name: "@deepseek-ai/dsh-llm-pi-ai"
  config:
    providers:
      wawapii-deepseek:
        displayName: wawapii - deepseek - 0.45x
        apiKeyEnv: WAWAPII_DEEPSEEK_API_KEY
        api: openai-completions
        baseURL: https://wawapii.com/v1
        models:
          - id: deepseek-v4.1-flash
            name: deepseek-v4.1-flash
            contextWindow: 1000000
- id: permission
  name: "@deepseek-ai/dsh-permission-presets"
  config:
    defaultPreset: danger-full-access
'''

CRED = '''version: 1
records:
  deepseek-account-platform/default:
    kind: grant
    payload:
      token: secret-token
refs:
  DEEPSEEK_API_KEY: sk-deepseek
  WB_API_KEY: sk-old
'''


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    return path


class DshPatchTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="dsh-patch-")
        self.patch = write(os.path.join(self.home, ".dsh", "profiles",
                                        "desktop", "cordis.patch.yml"), PATCH)
        self.cred = write(os.path.join(self.home, ".dsh", ".credentials.yaml"),
                          CRED)
        self.paths = wb_agents.CLIENTS["dsh"]["files"](self.home)
        self.ctx = {
            "base_url": "http://192.168.1.38:8788",
            "base_url_v1": "http://192.168.1.38:8788/v1",
            "api_key": "wb-test-key",
            "provider_id": wb_agents.PROVIDER_ID,
            "provider_name": wb_agents.PROVIDER_NAME,
            "credential_ref": wb_agents.CREDENTIAL_REF,
            "model": "glm-5.3",
            "models": [
                {"id": "glm-5.3", "context_window": 1000000,
                 "reasoning_efforts": ["minimal", "low", "medium", "high",
                                       "max"]},
                {"id": "deepseek-v4.1-flash"},
            ],
        }

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def apply(self):
        # _apply_dsh 只返回新内容，落盘是 integrate() 的事；测试里代写。
        written = wb_agents._apply_dsh(self.paths, self.ctx)
        for path, text in written.items():
            write(path, text)
        return written

    def test_dispatch_prefers_the_patch_layer(self):
        written = wb_agents._apply_dsh(self.paths, self.ctx)
        self.assertIn(self.patch, written)
        self.assertNotIn(self.paths["settings"], written)

    def test_wb_proxy_provider_lands_in_llm_pi_ai(self):
        self.apply()
        text = self.read(self.patch)
        self.assertIn("- id: llm-pi-ai", text)
        self.assertIn("      wb-proxy:", text)
        self.assertIn("        displayName: WB Proxy", text)
        self.assertIn("        baseURL: http://192.168.1.38:8788/v1", text)
        self.assertIn("        apiKeyEnv: WB_PROXY_API_KEY", text)
        self.assertIn("          - id: glm-5.3", text)
        self.assertIn("            contextWindow: 1000000", text)
        # 思考档位来自网关宣告的 reasoning_efforts（照抄档位名）：
        self.assertIn("            reasoningEfforts:", text)
        self.assertIn("              max: max", text)

    def test_sibling_providers_are_preserved_byte_for_byte(self):
        before = self.read(self.patch)
        wb_agents._apply_dsh(self.paths, self.ctx)
        after = self.read(self.patch)
        for line in before.split("\n"):
            if "wawapii-deepseek" in line or "WAWAPII_DEEPSEEK_API_KEY" in line:
                self.assertIn(line, after, "用户手写的 provider 行不该被动")

    def test_agent_default_model_follows_the_panel_choice(self):
        self.apply()
        text = self.read(self.patch)
        self.assertIn("    provider: wb-proxy", text)
        self.assertIn("    model: glm-5.3", text)
        self.assertIn("    reasoningEffort: max", text)

    def test_missing_entries_are_appended(self):
        # 只留一个不相关条目的补丁：两个目标条目都要新建骨架。
        write(self.patch, "- id: ui-theme\n  config:\n    preference: dark\n")
        self.apply()
        text = self.read(self.patch)
        self.assertIn("- id: llm-pi-ai", text)
        self.assertIn('  name: "@deepseek-ai/dsh-llm-pi-ai"', text)
        self.assertIn("- id: agent-default-model", text)
        self.assertIn("    model: glm-5.3", text)
        self.assertIn("    preference: dark", text)

    def test_credentials_ref_gets_the_key(self):
        self.apply()
        text = self.read(self.cred)
        self.assertIn("WB_PROXY_API_KEY: wb-test-key", text)
        self.assertIn("WB_API_KEY: sk-old", text)

    def test_missing_patch_file_is_created_fresh(self):
        # profiles 目录在、补丁文件不在（全新档案）：按空文件新建，两个条目都有。
        os.remove(self.patch)
        self.apply()
        text = self.read(self.patch)
        self.assertIn("- id: llm-pi-ai", text)
        self.assertIn("- id: agent-default-model", text)

    def test_no_profiles_dir_falls_back_to_legacy(self):
        # 整个 profiles/ 目录都没有（旧版 DSH）：回落 settings.yaml 路径，
        # 且 files 里不该再出现补丁文件。
        os.remove(self.patch)
        os.rmdir(os.path.dirname(self.patch))
        os.rmdir(os.path.join(self.home, ".dsh", "profiles"))
        self.assertFalse(wb_agents._dsh_has_patch_layer(self.home))
        legacy_paths = wb_agents.CLIENTS["dsh"]["files"](self.home)
        self.assertNotIn("patch", legacy_paths)
        written = wb_agents._apply_dsh(legacy_paths, self.ctx)
        self.assertIn(legacy_paths["settings"], written)
        self.assertNotIn(self.patch, written)


if __name__ == "__main__":
    unittest.main()
