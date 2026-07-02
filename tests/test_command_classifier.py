"""CommandClassifier 单元测试 — 验证 execute_command 命令分类器的
读取白名单 / 删除黑名单 / 命令注入防御 / 混合命令 / 大小写不敏感逻辑。

覆盖 T3 spec 中所有决策规则（黑名单为主 + 白名单加速 + 未知 allow）：
- 读取类命令前缀匹配白名单 → allow / low
- 删除/高危类命令前缀匹配黑名单（含 git push --force / git reset --hard /
  文件重定向 > / >>）→ confirm / high
- 其他命令 → 默认放行 allow / low（reason 标注"未知命令默认放行"以便审计）
- 命令注入防御：含 & / | / ; / && / || 分隔符时拆分子命令，
  任一子命令匹配黑名单则整体 confirm；无副作用命令（cd / echo / pwd）
  跳过不拖累整体；其余子命令都在白名单才整体 allow

运行方式:
    python -m unittest tests.test_command_classifier -v
    python tests/test_command_classifier.py
"""

from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from tests._mock_deps import install_mocks  # noqa: E402

install_mocks()

from src.agent.policy import CommandClassifier, PolicyEngine, Decision  # noqa: E402


class TestCommandClassifierReadWhitelist(unittest.TestCase):
    """读取类白名单：单条命令前缀匹配 → classify 返回 'read' / check 返回 allow。"""

    def _classify(self, command: str) -> str:
        return CommandClassifier.classify(command)

    def _check(self, command: str) -> Decision:
        engine = PolicyEngine()
        return engine.check("bash_exec", {"command": command})

    # ------------------------------------------------------------------
    # 单条读取命令
    # ------------------------------------------------------------------
    def test_dir_alone_is_read(self):
        """``dir`` 单条命令 → read，check 返回 allow / low。"""
        self.assertEqual(self._classify("dir"), "read")
        d = self._check("dir")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "读取类命令")
        self.assertEqual(d.risk_level, "low")

    def test_dir_with_args_is_read(self):
        """``dir C:\\Users`` 带参数 → read（前缀匹配 + 空白分隔）。"""
        self.assertEqual(self._classify("dir C:\\Users"), "read")
        d = self._check("dir C:\\Users")
        self.assertEqual(d.action, "allow")

    def test_ls_is_read(self):
        """``ls`` → read。"""
        self.assertEqual(self._classify("ls"), "read")
        self.assertEqual(self._check("ls").action, "allow")

    def test_cat_with_path_is_read(self):
        """``cat README.md`` → read。"""
        self.assertEqual(self._classify("cat README.md"), "read")
        self.assertEqual(self._check("cat README.md").action, "allow")

    def test_get_content_is_read(self):
        """``Get-Content file.txt``（PowerShell 读取）→ read。"""
        self.assertEqual(self._classify("Get-Content file.txt"), "read")
        self.assertEqual(self._check("Get-Content file.txt").action, "allow")

    def test_get_childitem_is_read(self):
        """``Get-ChildItem`` → read。"""
        self.assertEqual(self._classify("Get-ChildItem"), "read")
        self.assertEqual(self._check("Get-ChildItem").action, "allow")

    def test_git_status_is_read(self):
        """``git status`` → read。"""
        self.assertEqual(self._classify("git status"), "read")
        self.assertEqual(self._check("git status").action, "allow")

    def test_git_log_with_options_is_read(self):
        """``git log --oneline -5`` → read（git log 前缀 + 空白）。"""
        self.assertEqual(self._classify("git log --oneline -5"), "read")
        self.assertEqual(self._check("git log --oneline -5").action, "allow")

    def test_git_diff_is_read(self):
        """``git diff`` → read。"""
        self.assertEqual(self._classify("git diff"), "read")
        self.assertEqual(self._check("git diff").action, "allow")

    def test_git_show_is_read(self):
        """``git show HEAD`` → read。"""
        self.assertEqual(self._classify("git show HEAD"), "read")
        self.assertEqual(self._check("git show HEAD").action, "allow")

    def test_python_version_is_read(self):
        """``python --version`` → read。"""
        self.assertEqual(self._classify("python --version"), "read")
        self.assertEqual(self._check("python --version").action, "allow")

    def test_python_py_compile_is_read(self):
        """``python -m py_compile src/agent/policy.py`` → read。"""
        self.assertEqual(
            self._classify("python -m py_compile src/agent/policy.py"), "read"
        )
        self.assertEqual(
            self._check("python -m py_compile src/agent/policy.py").action, "allow"
        )

    def test_node_version_is_read(self):
        """``node --version`` → read。"""
        self.assertEqual(self._classify("node --version"), "read")
        self.assertEqual(self._check("node --version").action, "allow")

    def test_type_is_read(self):
        """``type config.yaml``（Windows type 命令）→ read。"""
        self.assertEqual(self._classify("type config.yaml"), "read")
        self.assertEqual(self._check("type config.yaml").action, "allow")

    def test_where_is_read(self):
        """``where python`` → read。"""
        self.assertEqual(self._classify("where python"), "read")
        self.assertEqual(self._check("where python").action, "allow")

    def test_whereis_is_read(self):
        """``whereis python`` → read。"""
        self.assertEqual(self._classify("whereis python"), "read")
        self.assertEqual(self._check("whereis python").action, "allow")

    def test_which_is_read(self):
        """``which python`` → read（命令查找类，与 where/whereis 一致）。"""
        self.assertEqual(self._classify("which python"), "read")
        self.assertEqual(self._check("which python").action, "allow")

    def test_pwd_is_read(self):
        """``pwd`` → read（目录查看类，无副作用）。"""
        self.assertEqual(self._classify("pwd"), "read")
        self.assertEqual(self._check("pwd").action, "allow")

    def test_cd_is_read(self):
        """``cd E:\\proj`` → read（目录切换，无副作用，亦在 NO_SIDE_EFFECT）。"""
        self.assertEqual(self._classify("cd E:\\proj"), "read")
        self.assertEqual(self._check("cd E:\\proj").action, "allow")

    def test_echo_is_read(self):
        """``echo hello`` → read（echo 无副作用，亦在 NO_SIDE_EFFECT）。"""
        self.assertEqual(self._classify("echo hello"), "read")
        d = self._check("echo hello")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "读取类命令")

    # ------------------------------------------------------------------
    # 系统信息类命令（新增白名单）
    # ------------------------------------------------------------------
    def test_hostname_is_read(self):
        """``hostname`` → read（系统信息类）。"""
        self.assertEqual(self._classify("hostname"), "read")
        self.assertEqual(self._check("hostname").action, "allow")

    def test_whoami_is_read(self):
        """``whoami`` → read（系统信息类）。"""
        self.assertEqual(self._classify("whoami"), "read")
        self.assertEqual(self._check("whoami").action, "allow")

    def test_ipconfig_is_read(self):
        """``ipconfig`` → read（系统信息类）。"""
        self.assertEqual(self._classify("ipconfig"), "read")
        self.assertEqual(self._check("ipconfig").action, "allow")

    def test_ifconfig_is_read(self):
        """``ifconfig`` → read（系统信息类）。"""
        self.assertEqual(self._classify("ifconfig"), "read")
        self.assertEqual(self._check("ifconfig").action, "allow")

    def test_netstat_is_read(self):
        """``netstat -an`` → read（系统信息类）。"""
        self.assertEqual(self._classify("netstat -an"), "read")
        self.assertEqual(self._check("netstat -an").action, "allow")

    def test_systeminfo_is_read(self):
        """``systeminfo`` → read（系统信息类）。"""
        self.assertEqual(self._classify("systeminfo"), "read")
        self.assertEqual(self._check("systeminfo").action, "allow")

    def test_uname_is_read(self):
        """``uname -a`` → read（系统信息类）。"""
        self.assertEqual(self._classify("uname -a"), "read")
        self.assertEqual(self._check("uname -a").action, "allow")

    def test_env_is_read(self):
        """``env`` → read（系统信息类，打印环境变量）。"""
        self.assertEqual(self._classify("env"), "read")
        self.assertEqual(self._check("env").action, "allow")

    def test_set_is_read(self):
        """``set`` → read（系统信息类，Windows cmd 打印环境变量）。"""
        self.assertEqual(self._classify("set"), "read")
        self.assertEqual(self._check("set").action, "allow")

    # ------------------------------------------------------------------
    # git 读取类命令（新增 git branch）
    # ------------------------------------------------------------------
    def test_git_branch_is_read(self):
        """``git branch`` → read（git 读取类，列出本地分支）。"""
        self.assertEqual(self._classify("git branch"), "read")
        self.assertEqual(self._check("git branch").action, "allow")

    def test_git_branch_with_args_is_read(self):
        """``git branch -a`` → read（git branch 前缀 + 空白）。"""
        self.assertEqual(self._classify("git branch -a"), "read")
        self.assertEqual(self._check("git branch -a").action, "allow")

    # ------------------------------------------------------------------
    # 版本查询类命令（新增 java / go / rustc）
    # ------------------------------------------------------------------
    def test_java_version_is_read(self):
        """``java --version`` → read（版本查询类）。"""
        self.assertEqual(self._classify("java --version"), "read")
        self.assertEqual(self._check("java --version").action, "allow")

    def test_go_version_is_read(self):
        """``go version`` → read（版本查询类）。"""
        self.assertEqual(self._classify("go version"), "read")
        self.assertEqual(self._check("go version").action, "allow")

    def test_rustc_version_is_read(self):
        """``rustc --version`` → read（版本查询类）。"""
        self.assertEqual(self._classify("rustc --version"), "read")
        self.assertEqual(self._check("rustc --version").action, "allow")

    def test_read_with_leading_whitespace(self):
        """命令前含前导空白 → strip 后仍匹配白名单。"""
        self.assertEqual(self._classify("   dir   C:\\Users"), "read")
        self.assertEqual(self._check("   dir   C:\\Users").action, "allow")

    def test_prefix_not_substring_dirxyz(self):
        """``dirxyz`` 不应被 ``dir`` 前缀误匹配（下一个字符必须空白或结束）。

        前缀匹配保护：避免 ``dir`` 误匹配 ``dirxyz``。归 other 后走"未知命令
        默认放行"策略 → allow。
        """
        self.assertEqual(self._classify("dirxyz"), "other")
        # other → 未知命令默认放行 allow
        d = self._check("dirxyz")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "未知命令默认放行")
        self.assertEqual(d.risk_level, "low")

    def test_prefix_not_substring_lsfoo(self):
        """``lsfoo`` 不应被 ``ls`` 前缀误匹配。归 other → allow。"""
        self.assertEqual(self._classify("lsfoo"), "other")
        self.assertEqual(self._check("lsfoo").action, "allow")


class TestCommandClassifierDeleteBlacklist(unittest.TestCase):
    """删除/高危类黑名单：单条命令前缀匹配 → classify 返回 'delete' / check 返回 confirm。

    覆盖删除类（del / rm / rmdir / Remove-Item / rd / unlink / git clean）、
    高危写入类（git push --force / -f / git reset --hard）、文件重定向
    （> / >>）三类。
    """

    def _classify(self, command: str) -> str:
        return CommandClassifier.classify(command)

    def _check(self, command: str) -> Decision:
        engine = PolicyEngine()
        return engine.check("bash_exec", {"command": command})

    def test_del_is_delete(self):
        """``del file.txt`` → delete，check 返回 confirm / high。"""
        self.assertEqual(self._classify("del file.txt"), "delete")
        d = self._check("del file.txt")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")
        self.assertEqual(d.risk_level, "high")

    def test_rm_is_delete(self):
        """``rm file.txt`` → delete。"""
        self.assertEqual(self._classify("rm file.txt"), "delete")
        d = self._check("rm file.txt")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")

    def test_rm_recursive_is_delete(self):
        """``rm -rf /tmp/scratch`` → delete（rm 前缀匹配，参数不影响分类）。"""
        self.assertEqual(self._classify("rm -rf /tmp/scratch"), "delete")
        self.assertEqual(self._check("rm -rf /tmp/scratch").action, "confirm")

    def test_rmdir_is_delete(self):
        """``rmdir empty_dir`` → delete。"""
        self.assertEqual(self._classify("rmdir empty_dir"), "delete")
        self.assertEqual(self._check("rmdir empty_dir").action, "confirm")

    def test_remove_item_is_delete(self):
        """``Remove-Item file.txt``（PowerShell）→ delete。"""
        self.assertEqual(self._classify("Remove-Item file.txt"), "delete")
        self.assertEqual(self._check("Remove-Item file.txt").action, "confirm")

    def test_rd_is_delete(self):
        """``rd /s /q old_dir``（Windows rd）→ delete。"""
        self.assertEqual(self._classify("rd /s /q old_dir"), "delete")
        self.assertEqual(self._check("rd /s /q old_dir").action, "confirm")

    def test_unlink_is_delete(self):
        """``unlink file.txt`` → delete。"""
        self.assertEqual(self._classify("unlink file.txt"), "delete")
        self.assertEqual(self._check("unlink file.txt").action, "confirm")

    def test_git_clean_is_delete(self):
        """``git clean -fd`` → delete。"""
        self.assertEqual(self._classify("git clean -fd"), "delete")
        d = self._check("git clean -fd")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")

    # ------------------------------------------------------------------
    # 高危写入类（git push --force / -f / git reset --hard）
    # ------------------------------------------------------------------
    def test_git_push_force_is_delete(self):
        """``git push --force`` → delete（强制推送覆盖远端历史，高危）。"""
        self.assertEqual(self._classify("git push --force"), "delete")
        d = self._check("git push --force")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")
        self.assertEqual(d.risk_level, "high")

    def test_git_push_force_with_remote_is_delete(self):
        """``git push --force origin main`` → delete（前缀匹配 + 参数）。"""
        self.assertEqual(self._classify("git push --force origin main"), "delete")
        self.assertEqual(self._check("git push --force origin main").action, "confirm")

    def test_git_push_short_force_is_delete(self):
        """``git push -f origin main`` → delete（-f 短形式强制推送）。"""
        self.assertEqual(self._classify("git push -f origin main"), "delete")
        d = self._check("git push -f origin main")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")

    def test_git_reset_hard_is_delete(self):
        """``git reset --hard HEAD~1`` → delete（硬重置丢弃工作区改动，高危）。"""
        self.assertEqual(self._classify("git reset --hard HEAD~1"), "delete")
        d = self._check("git reset --hard HEAD~1")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")

    def test_git_push_without_force_is_other(self):
        """``git push origin main``（普通推送）→ other / allow。

        普通推送不在黑名单（非 --force / -f），归 other 走默认放行策略。
        """
        self.assertEqual(self._classify("git push origin main"), "other")
        d = self._check("git push origin main")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "未知命令默认放行")

    # ------------------------------------------------------------------
    # 文件重定向操作符（> / >>）
    # ------------------------------------------------------------------
    def test_redirect_overwrite_is_delete(self):
        """``echo hello > file.txt`` → delete（`` > `` 覆盖文件重定向，高危）。"""
        self.assertEqual(self._classify("echo hello > file.txt"), "delete")
        d = self._check("echo hello > file.txt")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")
        self.assertEqual(d.risk_level, "high")

    def test_redirect_append_is_delete(self):
        """``echo hello >> file.txt`` → delete（`` >> `` 追加重定向，高危）。"""
        self.assertEqual(self._classify("echo hello >> file.txt"), "delete")
        d = self._check("echo hello >> file.txt")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")

    def test_redirect_leading_is_delete(self):
        """``> file.txt`` → delete（命令以 ``>`` 开头，截断文件）。"""
        self.assertEqual(self._classify("> file.txt"), "delete")
        self.assertEqual(self._check("> file.txt").action, "confirm")

    def test_redirect_leading_double_is_delete(self):
        """``>> file.txt`` → delete（命令以 ``>>`` 开头，追加文件）。"""
        self.assertEqual(self._classify(">> file.txt"), "delete")
        self.assertEqual(self._check(">> file.txt").action, "confirm")

    def test_redirect_in_compound_is_delete(self):
        """``dir & echo x > out.txt`` → 任一子命令含重定向 → delete。"""
        self.assertEqual(self._classify("dir & echo x > out.txt"), "delete")
        self.assertEqual(self._check("dir & echo x > out.txt").action, "confirm")


class TestCommandClassifierInjectionDefense(unittest.TestCase):
    """命令注入防御：含分隔符时拆分子命令，任一黑名单 → confirm；全白名单 → allow。"""

    def _classify(self, command: str) -> str:
        return CommandClassifier.classify(command)

    def _check(self, command: str) -> Decision:
        engine = PolicyEngine()
        return engine.check("bash_exec", {"command": command})

    # ------------------------------------------------------------------
    # 任一子命令黑名单 → delete（整体 confirm）
    # ------------------------------------------------------------------
    def test_ampersand_with_delete_confirms(self):
        """``dir & del xxx`` → 任一子命令 (del) 匹配黑名单 → delete / confirm。"""
        self.assertEqual(self._classify("dir & del xxx"), "delete")
        d = self._check("dir & del xxx")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")
        self.assertEqual(d.risk_level, "high")

    def test_pipe_with_delete_confirms(self):
        """``ls | rm file`` → rm 匹配黑名单 → delete / confirm。"""
        self.assertEqual(self._classify("ls | rm file"), "delete")
        self.assertEqual(self._check("ls | rm file").action, "confirm")

    def test_semicolon_with_delete_confirms(self):
        """``dir ; rm file`` → rm 匹配黑名单 → delete / confirm。"""
        self.assertEqual(self._classify("dir ; rm file"), "delete")
        self.assertEqual(self._check("dir ; rm file").action, "confirm")

    def test_double_ampersand_with_delete_confirms(self):
        """``dir && del xxx`` → del 匹配黑名单 → delete / confirm。

        验证 ``&&`` 多字符分隔符正确拆分（不会因 ``&`` 单字符分隔把
        ``&&`` 拆成 4 个空子命令）。
        """
        self.assertEqual(self._classify("dir && del xxx"), "delete")
        self.assertEqual(self._check("dir && del xxx").action, "confirm")

    def test_double_pipe_with_delete_confirms(self):
        """``ls || rm file`` → rm 匹配黑名单 → delete / confirm。"""
        self.assertEqual(self._classify("ls || rm file"), "delete")
        self.assertEqual(self._check("ls || rm file").action, "confirm")

    def test_git_clean_in_pipe_confirms(self):
        """``git status & git clean -fd`` → git clean 匹配黑名单 → confirm。"""
        self.assertEqual(self._classify("git status & git clean -fd"), "delete")
        self.assertEqual(
            self._check("git status & git clean -fd").action, "confirm"
        )

    # ------------------------------------------------------------------
    # 所有子命令白名单 → read（整体 allow）
    # ------------------------------------------------------------------
    def test_ampersand_all_read_allows(self):
        """``dir & ls`` → 所有子命令都在白名单 → read / allow。"""
        self.assertEqual(self._classify("dir & ls"), "read")
        d = self._check("dir & ls")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "读取类命令")
        self.assertEqual(d.risk_level, "low")

    def test_pipe_all_read_allows(self):
        """``git status | cat`` → 所有子命令都在白名单 → read / allow。"""
        self.assertEqual(self._classify("git status | cat"), "read")
        self.assertEqual(self._check("git status | cat").action, "allow")

    def test_semicolon_all_read_allows(self):
        """``ls ; git log`` → 所有子命令都在白名单 → read / allow。"""
        self.assertEqual(self._classify("ls ; git log"), "read")
        self.assertEqual(self._check("ls ; git log").action, "allow")

    def test_double_ampersand_all_read_allows(self):
        """``dir && ls`` → 所有子命令都在白名单 → read / allow。"""
        self.assertEqual(self._classify("dir && ls"), "read")
        self.assertEqual(self._check("dir && ls").action, "allow")


class TestCommandClassifierMixedAndOther(unittest.TestCase):
    """混合命令与其他命令：白名单+未知 → other/allow；纯未知 → other/allow；
    空命令 → other（防御性归类，但仍 allow）。

    黑名单为主策略下，other 类命令默认放行 allow，reason 标注
    "未知命令默认放行"以便审计追踪。
    """

    def _classify(self, command: str) -> str:
        return CommandClassifier.classify(command)

    def _check(self, command: str) -> Decision:
        engine = PolicyEngine()
        return engine.check("bash_exec", {"command": command})

    def test_mixed_read_and_unknown_is_other(self):
        """``dir & mkdir foo`` → mkdir 非白名单非黑名单 → other / allow。

        黑名单为主策略：拆分后非全白名单（但无黑名单）→ other → 默认放行 allow。
        """
        self.assertEqual(self._classify("dir & mkdir foo"), "other")
        d = self._check("dir & mkdir foo")
        self.assertEqual(d.action, "allow")
        # reason 标注"未知命令默认放行"（other 路径审计标注）
        self.assertEqual(d.reason, "未知命令默认放行")
        self.assertEqual(d.risk_level, "low")

    def test_mixed_unknown_and_read_is_other(self):
        """``mkdir foo & ls`` → mkdir 非白名单 → other / allow。"""
        self.assertEqual(self._classify("mkdir foo & ls"), "other")
        d = self._check("mkdir foo & ls")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "未知命令默认放行")

    def test_mixed_read_and_delete_is_delete(self):
        """``dir & rm file`` → rm 匹配黑名单 → delete / confirm（黑名单优先）。"""
        self.assertEqual(self._classify("dir & rm file"), "delete")
        d = self._check("dir & rm file")
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")

    def test_pure_unknown_command_is_other(self):
        """``mkdir new_folder`` → 非白名单非黑名单 → other / allow。"""
        self.assertEqual(self._classify("mkdir new_folder"), "other")
        d = self._check("mkdir new_folder")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "未知命令默认放行")
        self.assertEqual(d.risk_level, "low")

    def test_echo_command_is_read(self):
        """``echo hello`` → echo 在 NO_SIDE_EFFECT / READ_WHITELIST → read / allow。

        echo 现归 read（白名单加速），不再走 other 路径。
        """
        self.assertEqual(self._classify("echo hello"), "read")
        d = self._check("echo hello")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "读取类命令")

    def test_cd_with_dir_compound_is_read(self):
        """``cd E:\\proj && dir /b`` → cd 是 NO_SIDE_EFFECT 跳过，dir 在白名单 → read。

        验证 NO_SIDE_EFFECT 命令不拖累整体分类：cd 被跳过，仅 dir 决定整体归 read。
        """
        self.assertEqual(self._classify("cd E:\\proj && dir /b"), "read")
        d = self._check("cd E:\\proj && dir /b")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "读取类命令")

    def test_cd_with_unknown_compound_is_other(self):
        """``cd E:\\proj && mkdir foo`` → cd 跳过，mkdir 非白名单 → other / allow。

        NO_SIDE_EFFECT 命令跳过不拖累，但 mkdir 仍标记 has_side_effect_unsafe → other。
        """
        self.assertEqual(self._classify("cd E:\\proj && mkdir foo"), "other")
        d = self._check("cd E:\\proj && mkdir foo")
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "未知命令默认放行")

    def test_pwd_alone_is_read(self):
        """``pwd`` 单条 → NO_SIDE_EFFECT 跳过，无 unsafe → read / allow。"""
        self.assertEqual(self._classify("pwd"), "read")
        self.assertEqual(self._check("pwd").action, "allow")

    def test_echo_with_delete_compound_is_delete(self):
        """``echo hi & rm file`` → rm 匹配黑名单 → delete / confirm（黑名单优先）。

        echo 是 NO_SIDE_EFFECT 但黑名单优先级最高，任一 delete 子命令 → delete。
        """
        self.assertEqual(self._classify("echo hi & rm file"), "delete")
        self.assertEqual(self._check("echo hi & rm file").action, "confirm")

    def test_empty_command_is_other(self):
        """空字符串命令 → other（防御）。check 时无 command 字段走 DEFAULT_RULES。"""
        self.assertEqual(self._classify(""), "other")

    def test_whitespace_only_command_is_other(self):
        """纯空白命令 → other（防御）。"""
        self.assertEqual(self._classify("    "), "other")

    def test_separators_only_command_is_other(self):
        """纯分隔符 ``& | ;`` → 拆分后无有效子命令 → other（防御）。"""
        self.assertEqual(self._classify("& | ;"), "other")

    def test_no_command_field_falls_back_to_default_rules(self):
        """tool_input 无 ``command`` 字段 → 降级到 DEFAULT_RULES → confirm。

        无 command 字段属于防御路径，不走命令分类器，仍按 DEFAULT_RULES confirm。
        """
        engine = PolicyEngine()
        d = engine.check("bash_exec", {})
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "执行 shell 命令可能修改、删除系统资源")

    def test_none_tool_input_falls_back_to_default_rules(self):
        """tool_input 为 None → 降级到 DEFAULT_RULES → confirm（防御）。"""
        engine = PolicyEngine()
        d = engine.check("bash_exec", None)
        self.assertEqual(d.action, "confirm")


class TestCommandClassifierCaseInsensitive(unittest.TestCase):
    """大小写不敏感：DIR / RM / GIT STATUS 等大小写变体等价处理。"""

    def _classify(self, command: str) -> str:
        return CommandClassifier.classify(command)

    def _check(self, command: str) -> Decision:
        engine = PolicyEngine()
        return engine.check("bash_exec", {"command": command})

    def test_uppercase_dir_is_read(self):
        """``DIR``（全大写）→ read（Windows 大小写不敏感）。"""
        self.assertEqual(self._classify("DIR"), "read")
        self.assertEqual(self._check("DIR").action, "allow")

    def test_mixed_case_dir_is_read(self):
        """``Dir C:\\Users``（首字母大写）→ read。"""
        self.assertEqual(self._classify("Dir C:\\Users"), "read")
        self.assertEqual(self._check("Dir C:\\Users").action, "allow")

    def test_uppercase_rm_is_delete(self):
        """``RM file.txt``（全大写）→ delete（大小写不敏感）。"""
        self.assertEqual(self._classify("RM file.txt"), "delete")
        self.assertEqual(self._check("RM file.txt").action, "confirm")

    def test_mixed_case_git_status_is_read(self):
        """``Git Status``（首字母大写）→ read。"""
        self.assertEqual(self._classify("Git Status"), "read")
        self.assertEqual(self._check("Git Status").action, "allow")

    def test_uppercase_get_content_is_read(self):
        """``GET-CONTENT file.txt``（全大写）→ read。"""
        self.assertEqual(self._classify("GET-CONTENT file.txt"), "read")
        self.assertEqual(self._check("GET-CONTENT file.txt").action, "allow")

    def test_lowercase_remove_item_is_delete(self):
        """``remove-item file.txt``（全小写）→ delete（PowerShell 大小写不敏感）。"""
        self.assertEqual(self._classify("remove-item file.txt"), "delete")
        self.assertEqual(self._check("remove-item file.txt").action, "confirm")

    def test_uppercase_git_push_force_is_delete(self):
        """``GIT PUSH --FORCE``（全大写）→ delete（大小写不敏感）。"""
        self.assertEqual(self._classify("GIT PUSH --FORCE"), "delete")
        self.assertEqual(self._check("GIT PUSH --FORCE").action, "confirm")

    def test_mixed_case_git_reset_hard_is_delete(self):
        """``Git Reset --hard HEAD~1``（首字母大写）→ delete。"""
        self.assertEqual(self._classify("Git Reset --hard HEAD~1"), "delete")
        self.assertEqual(self._check("Git Reset --hard HEAD~1").action, "confirm")

    def test_uppercase_echo_is_read(self):
        """``ECHO hello``（全大写）→ read（echo 大小写不敏感）。"""
        self.assertEqual(self._classify("ECHO hello"), "read")
        self.assertEqual(self._check("ECHO hello").action, "allow")


class TestCommandClassifierCustomRulesIntegration(unittest.TestCase):
    """验证命令分类器与自定义 rules 配置的协同。

    黑名单为主策略下，命令分类器（read/delete/other）优先于自定义规则：
    - read → 始终 allow（即使规则为 deny）
    - delete → 始终 confirm（固定 reason"删除类命令"，不走规则 reason）
    - other → 始终 allow（reason 固定"未知命令默认放行"，不走规则 reason）
    仅当 ``tool_input`` 无 ``command`` 字段时降级到自定义规则匹配。
    """

    def test_other_command_ignores_custom_rule(self):
        """other 路径不再走自定义规则，直接 allow + 审计标注。

        黑名单为主策略：未知命令默认放行，reason 固定"未知命令默认放行"，
        不受用户配置的 execute_command 规则影响（自定义规则仅在无 command
        字段的防御路径下生效）。
        """
        engine = PolicyEngine(
            rules=[
                {
                    "tool": "bash_exec",
                    "risk": "confirm",
                    "reason": "自定义执行拦截原因",
                }
            ]
        )
        # mkdir 非白名单非黑名单 → other → 默认放行 allow（不走自定义规则）
        d = engine.check("bash_exec", {"command": "mkdir foo"})
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "未知命令默认放行")
        self.assertEqual(d.risk_level, "low")

    def test_other_command_ignores_custom_deny_rule(self):
        """other 路径即使配置 deny 也默认 allow（黑名单为主，未知放行）。"""
        engine = PolicyEngine(
            rules=[
                {"tool": "bash_exec", "risk": "deny", "reason": "全拒绝"}
            ]
        )
        d = engine.check("bash_exec", {"command": "mkdir foo"})
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "未知命令默认放行")

    def test_no_command_field_uses_custom_rule(self):
        """无 command 字段时降级到自定义规则（防御路径不受分类器影响）。"""
        engine = PolicyEngine(
            rules=[
                {
                    "tool": "bash_exec",
                    "risk": "confirm",
                    "reason": "自定义执行拦截原因",
                }
            ]
        )
        # 无 command 字段 → 降级到自定义规则
        d = engine.check("bash_exec", {})
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "自定义执行拦截原因")

    def test_delete_command_ignores_custom_rule_reason(self):
        """删除类命令始终返回"删除类命令"（不走 DEFAULT_RULES 的 reason）。"""
        engine = PolicyEngine(
            rules=[
                {
                    "tool": "bash_exec",
                    "risk": "confirm",
                    "reason": "自定义执行拦截原因",
                }
            ]
        )
        # rm 走 delete 路径，固定返回"删除类命令"
        d = engine.check("bash_exec", {"command": "rm file.txt"})
        self.assertEqual(d.action, "confirm")
        self.assertEqual(d.reason, "删除类命令")

    def test_read_command_allows_even_if_rule_is_deny(self):
        """读取类命令豁免 allow，即使配置中 execute_command 规则为 deny。

        这验证了命令分类器优先于 DEFAULT_RULES / 自定义规则：读取类命令
        始终 allow，不受用户配置影响（防止用户误把 execute_command 设为 deny
        后所有读取命令也被拦截）。
        """
        engine = PolicyEngine(
            rules=[
                {"tool": "bash_exec", "risk": "deny", "reason": "全拒绝"}
            ]
        )
        # dir 仍 allow（命令分类器豁免）
        d = engine.check("bash_exec", {"command": "dir"})
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "读取类命令")

    def test_classifier_independent_of_file_registry(self):
        """命令分类器不依赖 file_registry：未注入时仍生效。

        这验证了 spec 要求："在 check 方法中先检查 execute_command，
        再检查 write_file / delete_file"。
        """
        # 不注入 file_registry
        engine = PolicyEngine()
        d = engine.check("bash_exec", {"command": "dir"}, session_id="s1")
        self.assertEqual(d.action, "allow")

        # 注入 file_registry 但 session_id=None
        from src.agent.file_registry import FileOperationRegistry
        engine2 = PolicyEngine(file_registry=FileOperationRegistry())
        d2 = engine2.check("bash_exec", {"command": "dir"})
        self.assertEqual(d2.action, "allow")

    def test_disabled_engine_still_allows(self):
        """enabled=False 时一律 allow，命令分类器不执行。"""
        engine = PolicyEngine(enabled=False)
        # 即使是删除类命令，enabled=False 也 allow
        d = engine.check("bash_exec", {"command": "rm file.txt"})
        self.assertEqual(d.action, "allow")
        self.assertEqual(d.reason, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
