"""一键测试全部账号：按出站身份逐个试，全部失败才自动停用。

批量测试的规则（POST /accounts/test-all）：

  1. 每个启用的账号按出站身份逐个试——当前身分优先，其余跟上；任一身分
     通过就留用并停在该身分上，测试立刻停止；
  2. 全部身分都失败才停用（恢复原身分后 set_enabled False）；
  3. 通过时清掉失败尝试留下的短冷却，账号立刻可以接单；
  4. 已停用的账号不参与。

单次测试本身（probe_account）沿用既有实现，这里打桩替身，只验证批量
的编排：出站身份顺序、留用/停用决策与恢复行为。

Run with the current interpreter (python tests/run_all.py batch).
"""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts
import wb_proxy as P


def make_account(uid, product="workbuddy", enabled=True):
    d = tempfile.mkdtemp(prefix="batch-test-")
    return wb_accounts.Account({"uid": uid, "accessToken": "t-" + uid,
                                "realm": "cn", "enabled": enabled,
                                "product": product}, path=os.path.join(d, uid + ".json"))


def ok_result(uid):
    return {"ok": True, "uid": uid, "model": "deepseek-v4.1-flash",
            "elapsed_ms": 100, "reply": "OK"}


def fail_result(uid, status=403):
    return {"ok": False, "uid": uid, "status": status,
            "error": "HTTP %d" % status, "elapsed_ms": 100}

def banned_result(uid):
    """风控限制 403（code 11140）——明确封号类失败。"""
    return {"ok": False, "uid": uid, "status": 403,
            "error": 'HTTP 403: {"code":11140,"msg":"request illegal"}',
            "elapsed_ms": 100}


class StubPool:
    """set_enabled 走真实的 enabled 翻转，批量停用的落点可断言。"""

    def __init__(self, accounts):
        self.accounts = accounts

    def set_enabled(self, uid, enabled):
        for a in self.accounts:
            if a.uid == uid:
                a.enabled = bool(enabled)
                return a.public()
        return None


class BatchTestTests(unittest.TestCase):
    def setUp(self):
        self.old_pool = P.POOL
        self.old_dir = P.ACCOUNTS_DIR
        self.dir = tempfile.mkdtemp(prefix="batch-route-")
        P.ACCOUNTS_DIR = self.dir

    def tearDown(self):
        P.POOL = self.old_pool
        P.ACCOUNTS_DIR = self.old_dir

    def route(self, accounts, script):
        """打桩 probe_account 按脚本吐结果，_json 原样透传。"""
        P.POOL = StubPool(accounts)
        calls = {"n": 0}
        fake = types.SimpleNamespace(
            _json=lambda code, obj: obj,
            # test-all 的返回走 _json_panel（昵称脱敏），与生产一致。
            _json_panel=lambda code, obj: obj,
        )

        def next_result(account, model, timeout=30):
            r = script[min(calls["n"], len(script) - 1)]
            calls["n"] += 1
            return r

        old_probe = P.probe_account
        P.probe_account = next_result
        try:
            return P.Handler._route_accounts_test_all(fake, {}), calls
        finally:
            P.probe_account = old_probe

    def test_first_product_pass_keeps_it_and_stops_early(self):
        a = make_account("b-pass", product="workbuddy")
        result, calls = self.route([a], [ok_result("b-pass")])
        self.assertEqual(calls["n"], 1, "第一个身分通过就不该再试其余身分")
        self.assertTrue(a.enabled)
        self.assertEqual(a.product, "workbuddy")
        self.assertEqual(result["disabled_count"], 0)

    def test_second_product_pass_switches_and_keeps_enabled(self):
        a = make_account("b-switch", product="workbuddy")
        result, calls = self.route(
            [a], [fail_result("b-switch"), ok_result("b-switch")])
        self.assertEqual(calls["n"], 2, "当前身分失败后应试下一个身分")
        self.assertTrue(a.enabled)
        self.assertEqual(a.product, "vscode", "通过的身分应被留用")
        self.assertEqual(result["results"][0]["product"], "vscode")
        self.assertFalse(result["results"][0]["disabled"])

    def test_all_products_fail_with_ban_disables_and_restores_product(self):
        a = make_account("b-dead", product="cli")
        result, calls = self.route([a], [banned_result("b-dead")] * 3)
        self.assertEqual(calls["n"], 3, "三个出站身份都该试到")
        self.assertFalse(a.enabled, "全部身分失败且属于明确封号——这里应停用")
        self.assertEqual(a.product, "cli", "停用时恢复原出站身份")
        self.assertEqual(result["disabled_count"], 1)
        self.assertEqual(result["results"][0]["tried"][0]["product"], "workbuddy",
                         "尝试顺序应为当前身分优先")

    def test_all_products_fail_with_ban_401_also_disables(self):
        a = make_account("b-401", product="workbuddy")
        result, _ = self.route([a], [fail_result("b-401", status=401)] * 3)
        self.assertFalse(a.enabled, "401 凭证失效属于明确封号类，应停用")
        self.assertEqual(result["disabled_count"], 1)

    def test_any_ban_signal_among_failures_disables(self):
        # 混合失败里只要有一个明确封号信号就停用。
        a = make_account("b-mixed", product="workbuddy")
        result, _ = self.route(
            [a], [fail_result("b-mixed", status=402), banned_result("b-mixed"),
                  fail_result("b-mixed", status=500)])
        self.assertFalse(a.enabled)
        self.assertEqual(result["disabled_count"], 1)

    def test_quota_and_credit_failures_never_disable(self):
        # 额度（429/6004）、积分（402）、限流、内容审核 403、5xx：都不停用。
        for status in (429, 402, 500):
            a = make_account("b-soft-%d" % status, product="workbuddy")
            result, _ = self.route([a], [fail_result("b-soft", status=status)] * 3)
            self.assertTrue(a.enabled, "HTTP %d 失败不应停用" % status)
            self.assertEqual(result["disabled_count"], 0)
            self.assertEqual(a.product, "workbuddy", "非封号失败应恢复原身分")

    def test_content_review_403_is_not_a_ban(self):
        a = make_account("b-content", product="workbuddy")
        result, _ = self.route([a], [fail_result("b-content")] * 3)
        self.assertTrue(a.enabled, "内容审核 403 不是封号，不应停用")
        self.assertEqual(result["disabled_count"], 0)

    def test_disabled_accounts_are_skipped(self):
        dead = make_account("b-off", enabled=False)
        alive = make_account("b-on", product="workbuddy")
        result, calls = self.route([dead, alive], [ok_result("b-on")])
        self.assertEqual(calls["n"], 1, "已停用的账号不参与测试")
        self.assertFalse(dead.enabled, "停用账号不应被顺手改动（保持停用）")
        self.assertEqual(result["total"], 1)

    def test_pass_clears_the_ladder_from_failed_attempts(self):
        # 通过后账号要能立刻接单：失败尝试留下的错误态必须被清掉。
        a = make_account("b-clear", product="workbuddy")
        P.POOL = StubPool([a])

        def probe_by_product(account, model, timeout=30):
            if account.product == "workbuddy":
                return fail_result("b-clear")
            return ok_result("b-clear")

        fake = types.SimpleNamespace(
            _json=lambda code, obj: obj,
            _json_panel=lambda code, obj: obj,
        )
        old_probe = P.probe_account
        P.probe_account = probe_by_product
        try:
            P.Handler._route_accounts_test_all(fake, {})
        finally:
            P.probe_account = old_probe
        self.assertEqual(a.last_error, "", "通过后应清掉失败尝试的错误态")
        self.assertEqual(a.cooldown_until, 0.0, "通过后不应残留失败冷却")


if __name__ == "__main__":
    unittest.main()
