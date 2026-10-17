"""余额见底账号的调度：付费跳过、免费优先、从不停用。

背景（2026-10-17 用户报告「老是选择积分为 0 的账号」）：上游允许 0 积分
账号继续透支消费（实测 remain=0 的账号跑 x0.06 模型一天扣了 122 分），
而旧调度对余额毫无感知，0 积分与 4000 积分账号在轮询里权重完全一样。
修复后的行为：

  1. 付费模型：余额见底（已知余额 <= 0）的账号不接单（ready() 挡掉）；
  2. 免费模型（目录 x0.00）：余额见底的账号优先接单（省付费账号的积分），
     它们全忙时落回普通调度；
  3. 余额从未抓取的账号不受影响（fresh install 不该被当成空的）；
  4. 没有任何一条路径因为「积分为 0」停用账号——停用只来自明确封号。

Run with the current interpreter (python tests/run_all.py batch).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts


def make_account(uid, remain, realm="cn", enabled=True, weight=1):
    d = tempfile.mkdtemp(prefix="balance-")
    # expiresAt 给到远期：假 token 无 JWT exp，ready() 会走 refresh()（真网）
    # 而挂掉——闸门测试只关心积分路径，别让凭证刷新掺和。
    return wb_accounts.Account(
        {"uid": uid, "accessToken": "t-" + uid, "realm": realm,
         "enabled": enabled, "product": "workbuddy", "weight": weight,
         "expiresAt": 4102444800,
         "credits": {"remain": remain}},
        path=os.path.join(d, uid + ".json"))


class BalanceExhaustedTests(unittest.TestCase):
    def test_known_zero_balance_is_exhausted(self):
        a = make_account("b-zero", 0)
        self.assertTrue(a.balance_exhausted())

    def test_negative_balance_is_exhausted(self):
        a = make_account("b-neg", -5)
        self.assertTrue(a.balance_exhausted())

    def test_unknown_balance_is_not_exhausted(self):
        a = make_account("b-unknown", None)
        self.assertFalse(a.balance_exhausted())
        a2 = make_account("b-nocredits", 0)
        a2.credits = None
        self.assertFalse(a2.balance_exhausted())

    def test_positive_balance_is_not_exhausted(self):
        a = make_account("b-some", 4.02)
        self.assertFalse(a.balance_exhausted())


class CreditsBlockedForModelTests(unittest.TestCase):
    def test_paid_model_blocked_on_zero_balance(self):
        a = make_account("c-zero", 0)
        self.assertTrue(a.credits_blocked_for_model("glm-5.3"))

    def test_free_model_not_blocked_on_zero_balance(self):
        a = make_account("c-free", 0)
        a.free_models = frozenset(["hy3"])
        self.assertFalse(a.credits_blocked_for_model("hy3"))

    def test_unknown_model_counts_as_paid(self):
        a = make_account("c-unknown", 0)
        a.free_models = frozenset(["hy3"])
        self.assertTrue(a.credits_blocked_for_model("mystery-model"))

    def test_paid_model_fine_with_balance(self):
        a = make_account("c-some", 100)
        self.assertFalse(a.credits_blocked_for_model("glm-5.3"))


class ReadyGateTests(unittest.TestCase):
    def test_ready_false_for_paid_on_zero_balance(self):
        a = make_account("r-zero", 0)
        self.assertFalse(a.ready(model="glm-5.3"))
        self.assertTrue(a.enabled, "ready 挡的是模型不是账号——账号绝不因此停用")

    def test_ready_true_for_free_on_zero_balance(self):
        a = make_account("r-free", 0)
        a.free_models = frozenset(["hy3"])
        self.assertTrue(a.ready(model="hy3"))

    def test_ready_true_for_paid_without_model(self):
        # model=None（无法判断）不挡：与 credit_limit_blocked 同款口径。
        a = make_account("r-nomodel", 0)
        self.assertTrue(a.ready())


class FreeForZeroBalanceLayerTests(unittest.TestCase):
    def setUp(self):
        self.pool = wb_accounts.AccountPool(tempfile.mkdtemp(prefix="pool-"))
        self.zero = make_account("z1", 0)
        self.zero.free_models = frozenset(["hy3"])
        self.paid = make_account("p1", 500)
        self.paid.free_models = frozenset(["hy3"])
        self.pool.accounts = [self.zero, self.paid]

    def test_free_model_picks_zero_balance_first(self):
        got = {self.pool.pick(model="hy3").uid for _ in range(10)}
        self.assertEqual(got, {"z1"}, "免费请求应全部优先喂 0 积分账号")

    def test_paid_model_skips_zero_balance(self):
        got = {self.pool.pick(model="glm-5.3").uid for _ in range(10)}
        self.assertEqual(got, {"p1"}, "付费请求不该落到 0 积分账号")

    def test_busy_zero_balance_falls_back(self):
        # 0 积分账号被排除时，免费请求照常落到有余额账号。
        got = self.pool.pick(model="hy3", exclude={self.zero.uid})
        self.assertEqual(got.uid, "p1")

    def test_no_zero_balance_short_circuits(self):
        pool = wb_accounts.AccountPool(tempfile.mkdtemp(prefix="pool2-"))
        pool.accounts = [self.paid]
        self.assertIsNone(pool._pick_free_for_zero_balance(
            pool.accounts, set(), "hy3"))

    def test_unknown_balance_not_preferred(self):
        a = make_account("u1", None)
        a.free_models = frozenset(["hy3"])
        self.pool.accounts = [a, self.paid]
        # 优先层本身只挑见底账号：u1（余额未知）不算，池里没有见底账号时
        # 这一层直接放手（None），选择权交回普通调度。
        got = self.pool._pick_free_for_zero_balance(
            self.pool.accounts, set(), "hy3")
        self.assertIsNone(got, "余额未知的账号不算「见底」，层内无可选就返回 None")


if __name__ == "__main__":
    unittest.main()
