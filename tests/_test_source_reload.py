"""源码变更自动重启的单元行为（默认关闭，显式开启才生效）。

重启会瞬断进行中的请求，所以默认必须是关。SourceReloader 的 run() 会
os.execv 换进程，测不了也不敢在套件里测；这里钉住两件能单元测的事：

  1. enabled()：默认关；--reload 或 WB_AUTO_RELOAD=1 显式打开；
  2. snapshot()：只看脚本目录的 wb_*.py 与 dashboard.html，accounts/ 与
     usage/ 里的运行数据怎么写都不进快照——不然一次签到就把服务重启了。

重启路径本身由 `_lifecycle.spawn_managed` 的 WB_AUTO_RELOAD=0 保证测试期间
不触发，冒烟验证（touch dashboard.html 看重启）是手动步骤。

Run with the current interpreter (python tests/run_all.py reload).
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ArgsStub(object):
    def __init__(self, reload=False):
        self.reload = reload


class ReloadEnabledTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("WB_AUTO_RELOAD", None)

    def test_off_by_default(self):
        # 重启会瞬断进行中的请求，默认必须不自动重启。
        os.environ.pop("WB_AUTO_RELOAD", None)
        self.assertFalse(P.SourceReloader.enabled(ArgsStub(False)))

    def test_flag_turns_it_on(self):
        os.environ.pop("WB_AUTO_RELOAD", None)
        self.assertTrue(P.SourceReloader.enabled(ArgsStub(True)))

    def test_env_turns_it_on(self):
        for value in ("1", "on", "true", " ON "):
            os.environ["WB_AUTO_RELOAD"] = value
            self.assertTrue(P.SourceReloader.enabled(ArgsStub(False)),
                            "WB_AUTO_RELOAD=%r should enable" % value)

    def test_env_off_keeps_it_off(self):
        os.environ["WB_AUTO_RELOAD"] = "0"
        self.assertFalse(P.SourceReloader.enabled(ArgsStub(False)))


class ReloadSnapshotTests(unittest.TestCase):
    def setUp(self):
        # snapshot() 以 sys.argv[0] 所在目录为根；测试进程的 argv[0] 是本套件
        # 文件，指回仓库根让快照看到真实的源码集。
        self.saved_argv0 = sys.argv[0]
        sys.argv[0] = os.path.join(ROOT, "wb_proxy.py")

    def tearDown(self):
        sys.argv[0] = self.saved_argv0
        os.environ.pop("WB_AUTO_RELOAD", None)

    def test_covers_sources_and_ignores_runtime_data(self):
        state = P.SourceReloader().snapshot()
        self.assertIn(os.path.normcase(os.path.join(ROOT, "wb_proxy.py")), state)
        self.assertIn(os.path.normcase(os.path.join(ROOT, "dashboard.html")), state)
        self.assertTrue(any(k.endswith("wb_accounts.py") for k in state))
        # 运行数据与子目录一概不在监视范围。
        self.assertFalse(any(os.sep + "accounts" + os.sep in k for k in state))
        self.assertFalse(any(os.sep + "usage" + os.sep in k for k in state))
        self.assertFalse(any("__pycache__" in k for k in state))

    def test_touch_changes_the_snapshot(self):
        reloader = P.SourceReloader()
        before = reloader.snapshot()
        target = os.path.join(ROOT, "dashboard.html")
        os.utime(target, None)
        after = reloader.snapshot()
        self.assertNotEqual(before, after)
        self.assertEqual(before.keys(), after.keys())


if __name__ == "__main__":
    unittest.main()
