"""渠道校验拦截（400 / code 11-128）：换号重试，绝不直接抛给客户端。

用户实测（2026-10-19）：上游渠道校验会间歇性回 400 / 11-128「Illegal API
invocation from an unapproved channel」——不是账号的错、不是内容的错，同一
会话重发常常就过。旧处置把这个 400 当普通请求错误直接 raise，agent 收到 400
就终止整轮任务（「本轮运行失败 400 …」）。修复后的处置：

  1. dispatch 循环识别 11-128 正文 → 不记账、不冷却，把该号 demote 出亲和，
     换下一个号重试；
  2. 全池被拦时整池重轮，最多 CHANNEL_BLOCK_ROUNDS 轮（间隔 1/2/3s）；
  3. 重轮耗尽才包装成 ChannelBlocked（code 400）交出去，客户端重发即可；
  4. 其他 400（真请求错误）保持原样 raise——换号救不了参数错误，别烧重试。

Run with the current interpreter (python tests/run_all.py batch).
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_accounts as A
import wb_proxy as P
from _isolated_dirs import isolated_data_dirs

_TMP = isolated_data_dirs("wb-channel-block-")

BLOCK_BODY = ('{"code":"11-128","msg":"Illegal API invocation from an '
              'unapproved channel","requestId":"01a15c","displayMsg":{'
              '"zh":"请求被拦截，请重新发送"}}')
BAD_REQUEST_BODY = '{"code":"1001","msg":"invalid messages shape"}'


def account(uid="aaaa1111-bbbb-cccc-dddd-eeeeffff0000", realm="intl", **overrides):
    data = {"uid": uid, "accessToken": "x.y.z", "nickname": uid[:8],
            "enabled": True, "realm": realm,
            "expiresAt": 4102444800}
    data.update(overrides)
    return A.Account(data)


class _FakePool(object):
    def __init__(self, accounts):
        self.accounts = accounts
        self.affinity = types.SimpleNamespace(
            demote=lambda *a, **k: None)

    def apply_remaining_weights(self, weights=None):
        return weights or {}

    def pick_for_session(self, realm=None, session_key=None, exclude=None,
                         model=None, page=None):
        exclude = exclude or set()
        for a in self.accounts:
            if a.realm == realm and a.uid not in exclude and a.enabled:
                return a
        return None

    def count_ready(self, realm=None, model=None):
        return len([a for a in self.accounts
                    if a.realm == realm and a.enabled])


class ChannelBlockPredicateTests(unittest.TestCase):
    def test_full_body_matches(self):
        self.assertTrue(P.channel_block_is_11_128(BLOCK_BODY))

    def test_truncated_body_still_matches(self):
        # 短截断正文保得住 code 字段。
        self.assertTrue(P.channel_block_is_11_128(BLOCK_BODY[:60]))

    def test_other_400_does_not_match(self):
        self.assertFalse(P.channel_block_is_11_128(BAD_REQUEST_BODY))
        self.assertFalse(P.channel_block_is_11_128(""))
        self.assertFalse(P.channel_block_is_11_128(None))

    def test_chinese_display_msg_matches(self):
        self.assertTrue(P.channel_block_is_11_128("没通过渠道校验，可能不是官方客户端发出的"))


def http_error(code, body):
    err = urllib.error.HTTPError("https://up", code, "error", {},
                                 _BytesIO(body))
    err._wb_body = body   # body 只能读一次；重建时从这里拿原文
    return err


class _BytesIO(object):
    def __init__(self, text):
        self._buf = __import__("io").BytesIO(text.encode("utf-8"))

    def read(self, n=-1):
        return self._buf.read(n)

    def close(self):
        pass


class DispatchRotationTests(unittest.TestCase):
    """open_upstream 对 11-128 的换号/重轮/最终包装。"""

    def setUp(self):
        self.a1 = account(uid="11111111-0000-0000-0000-000000000001")
        self.a2 = account(uid="22222222-0000-0000-0000-000000000002")
        self.old_pool = P.POOL
        self.old_urlopen = A.urlopen
        P.POOL = _FakePool([self.a1, self.a2])

    def tearDown(self):
        P.POOL = self.old_pool
        A.urlopen = self.old_urlopen

    def _run(self, responses):
        seq = list(responses)

        def fake_urlopen(req, timeout=30, proxy=""):
            item = seq.pop(0) if seq else responses[-1]
            if isinstance(item, Exception):
                # HTTPError 的 body 只能读一次：同一实例复用会在第二次重试时
                # read 出空串，拦截判定退化为"普通 400"。每次新建。
                if isinstance(item, urllib.error.HTTPError):
                    raise http_error(item.code,
                                     getattr(item, "_wb_body", "") or "")
                raise item
            return item

        A.urlopen = fake_urlopen
        with mock.patch.object(P.time, "sleep", lambda *_: None):
            return P.open_upstream(
                {"model": "deepseek-v4.1-flash",
                 "messages": [{"role": "user", "content": "hi"}]},
                target_realm="intl")

    def test_block_on_first_account_rotates_to_second(self):
        ok = mock.Mock()
        ok.__enter__ = lambda s: ok
        ok.__exit__ = lambda s, *e: False
        response, _account, _effort = self._run(
            [http_error(400, BLOCK_BODY), ok])
        self.assertIs(response, ok)
        # 拦截不记账：两个号的 fails 都是 0，也没有冷却。
        self.assertEqual(self.a1.fails, 0)
        self.assertEqual(self.a2.fails, 0)
        self.assertEqual(self.a1.throttle_wait(), 0)

    def test_persistent_block_raises_channel_blocked(self):
        with self.assertRaises(P.ChannelBlocked) as ctx:
            self._run([http_error(400, BLOCK_BODY)])
        self.assertEqual(ctx.exception.code, 400)
        self.assertIn("11-128", ctx.exception.detail)

    def test_non_block_400_still_raises_immediately(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._run([http_error(400, BAD_REQUEST_BODY)])
        self.assertEqual(ctx.exception.code, 400)
        # 真 400 只试了第一个号（不烧重试）。
        self.assertEqual(self.a2.fails, 0)

    def test_block_does_not_regress_to_client_on_round_one(self):
        # 第一轮全拦后应进入重轮而不是立刻交出：只要 3 轮内放行就成功。
        ok = mock.Mock()
        ok.__enter__ = lambda s: ok
        ok.__exit__ = lambda s, *e: False
        # 第一轮 2 个号都被拦（2 个 400），重轮第 1 次后第一个号放行。
        seq = [http_error(400, BLOCK_BODY), http_error(400, BLOCK_BODY), ok]
        response, _account, _effort = self._run(seq)
        self.assertIs(response, ok)


if __name__ == "__main__":
    unittest.main()
