"""智能体配置的两个服务端偏好：表单记忆 与 远程开关。

  - agent_apply_prefs：面板「智能体配置」页上次的 网关地址 / Key / 模型
    选择，随 accounts/settings.json 持久化，下次打开预填。坏形状回落空串。
  - agents_remote_enabled：一键配置的门禁默认只认回环（issue #246）；
    这个开关显式打开后，非回环的看板会话也放行（Docker/OpenWrt 部署除外）。

Run with the current interpreter (python tests/run_all.py agent_prefs).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_settings
import wb_proxy


class AgentApplyPrefsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agent-prefs-")

    def test_defaults_are_empty_strings(self):
        self.assertEqual(wb_settings.agent_apply_prefs(self.dir),
                         {"base_url": "", "key_id": "", "model": "",
                          "provider_label": ""})

    def test_round_trip_and_partial_update(self):
        wb_settings.set_agent_apply_prefs(self.dir, {
            "base_url": "http://192.168.1.38:8788/v1", "key_id": "k1"})
        self.assertEqual(wb_settings.agent_apply_prefs(self.dir),
                         {"base_url": "http://192.168.1.38:8788/v1",
                          "key_id": "k1", "model": "", "provider_label": ""})
        # 一次只改一个字段，其余保留
        wb_settings.set_agent_apply_prefs(self.dir, {"model": "glm-5.3"})
        self.assertEqual(wb_settings.agent_apply_prefs(self.dir)["model"], "glm-5.3")
        self.assertEqual(wb_settings.agent_apply_prefs(self.dir)["key_id"], "k1")

    def test_junk_shapes_collapse_to_empty(self):
        wb_settings.set_agent_apply_prefs(self.dir, {"model": "glm-5.3"})
        wb_settings.set_agent_apply_prefs(self.dir, {"base_url": 123, "model": ["x"]})
        # 非字符串字段被忽略，已存的 model 不受影响
        self.assertEqual(wb_settings.agent_apply_prefs(self.dir)["model"], "glm-5.3")
        # 非 dict 的整次提交被忽略
        wb_settings.set_agent_apply_prefs(self.dir, "not a dict")
        self.assertEqual(wb_settings.agent_apply_prefs(self.dir)["model"], "glm-5.3")
        self.assertEqual(wb_settings.agent_apply_prefs(self.dir)["base_url"], "")

    def test_values_are_stripped_and_capped(self):
        wb_settings.set_agent_apply_prefs(self.dir, {"base_url": "  " + "x" * 600 + "  "})
        stored = wb_settings.agent_apply_prefs(self.dir)["base_url"]
        self.assertEqual(stored, "x" * 500)


class AgentsRemoteEnabledTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="agents-remote-")

    def test_default_is_off(self):
        self.assertFalse(wb_settings.agents_remote_enabled(self.dir))

    def test_round_trip_with_strict_boolean(self):
        self.assertTrue(wb_settings.set_agents_remote_enabled(self.dir, True))
        self.assertTrue(wb_settings.agents_remote_enabled(self.dir))
        # 与其他开关同款 is True 归一化：字符串 "true"、1 都不算开
        wb_settings.set_agents_remote_enabled(self.dir, False)
        for junk in ("true", 1, [True]):
            data = wb_settings.load(self.dir)
            data[wb_settings.AGENTS_REMOTE_ENABLED_KEY] = junk
            wb_settings.save(self.dir, data)
            self.assertFalse(wb_settings.agents_remote_enabled(self.dir),
                             "%r 不应被视为开启" % (junk,))


class AgentsGateTests(unittest.TestCase):
    """模块级门禁：只认回环；远程开关在方法层叠加（settings 默认关）。"""

    def test_loopback_allowed_lan_rejected(self):
        self.assertTrue(wb_proxy.agents_client_allowed(("127.0.0.1", 5000)))
        self.assertTrue(wb_proxy.agents_client_allowed(("::1", 5000)))
        self.assertTrue(wb_proxy.agents_client_allowed(("::ffff:127.0.0.1", 5000)))
        # 局域网来源默认拒绝——远程开关在 _agents_client_allowed 里叠加。
        self.assertFalse(wb_proxy.agents_client_allowed(("192.168.1.5", 5000)))
        self.assertFalse(wb_proxy.agents_client_allowed(None))

    def test_dashboard_wires_the_remote_toggle(self):
        with open(os.path.join(os.path.dirname(os.path.dirname(
                os.path.abspath(__file__))), "dashboard.html"),
                encoding="utf-8") as fh:
            html = fh.read()
        self.assertIn("saveAgentsRemote", html)
        self.assertIn("agents_remote_enabled: value", html)


if __name__ == "__main__":
    unittest.main()
