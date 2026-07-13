"""SYSTEM_PROMPT plan 模式说明单元测试 — 验证任务编排能力段落存在且关键字完整。

运行方式:
    python -m unittest tests.test_system_prompt -v
    python tests/test_system_prompt.py

策略:
- prompts.py 仅含字符串常量，无外部依赖，可直接导入无需 mock。
- 校验 SYSTEM_PROMPT 非空、包含 plan_task/update_todo 等关键串及段落标题。
- 校验 CONSOLIDATION_PROMPT 仍存在且未被破坏。
"""

from __future__ import annotations

import os
import sys
import unittest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from hermes.llm.prompts import CONSOLIDATION_PROMPT, SYSTEM_PROMPT  # noqa: E402


class TestSystemPromptExists(unittest.TestCase):
    """验证 SYSTEM_PROMPT 与 CONSOLIDATION_PROMPT 基本完整性。"""

    def test_system_prompt_is_non_empty_string(self) -> None:
        self.assertIsInstance(SYSTEM_PROMPT, str)
        self.assertTrue(SYSTEM_PROMPT.strip(), "SYSTEM_PROMPT 不应为空")

    def test_consolidation_prompt_preserved(self) -> None:
        """CONSOLIDATION_PROMPT 必须仍然存在且非空，未被本次改动破坏。"""
        self.assertIsInstance(CONSOLIDATION_PROMPT, str)
        self.assertTrue(CONSOLIDATION_PROMPT.strip(), "CONSOLIDATION_PROMPT 不应为空")
        self.assertIn("facts", CONSOLIDATION_PROMPT)
        self.assertIn("{conversation}", CONSOLIDATION_PROMPT)


class TestPlanModeSection(unittest.TestCase):
    """验证 '## 任务编排能力' 段落存在且包含所需关键字。"""

    def test_section_title_present(self) -> None:
        self.assertIn("## 任务编排能力", SYSTEM_PROMPT)

    def test_plan_task_keyword(self) -> None:
        self.assertIn("plan_create", SYSTEM_PROMPT)

    def test_update_todo_keyword(self) -> None:
        self.assertIn("plan_update_step", SYSTEM_PROMPT)

    def test_multi_step_task_keyword(self) -> None:
        """必须出现 '多步骤任务' 或 '复杂任务' 之一。"""
        self.assertTrue(
            "多步骤任务" in SYSTEM_PROMPT or "复杂任务" in SYSTEM_PROMPT,
            "SYSTEM_PROMPT 应说明多步骤任务或复杂任务",
        )

    def test_sequential_progress_keyword(self) -> None:
        """必须出现 '不可越级' 或 '按顺序' 之一。"""
        self.assertTrue(
            "不可越级" in SYSTEM_PROMPT or "按顺序" in SYSTEM_PROMPT,
            "SYSTEM_PROMPT 应强调按顺序推进、不可越级",
        )

    def test_in_progress_status(self) -> None:
        self.assertIn("in_progress", SYSTEM_PROMPT)

    def test_completed_and_failed_status(self) -> None:
        self.assertIn("completed", SYSTEM_PROMPT)
        self.assertIn("failed", SYSTEM_PROMPT)

    def test_auto_advance_keyword(self) -> None:
        """必须同时出现 '自动' 与 '推进'，描述系统自动推进机制。"""
        self.assertIn("自动", SYSTEM_PROMPT)
        self.assertIn("推进", SYSTEM_PROMPT)


class TestSectionOrdering(unittest.TestCase):
    """验证 '## 任务编排能力' 段落位于 '## 工具调用规范' 之前。"""

    def test_plan_section_before_tool_section(self) -> None:
        plan_idx = SYSTEM_PROMPT.find("## 任务编排能力")
        tool_idx = SYSTEM_PROMPT.find("## 工具调用规范")
        self.assertGreater(plan_idx, 0, "应存在 '## 任务编排能力' 段落")
        self.assertGreater(tool_idx, 0, "应存在 '## 工具调用规范' 段落")
        self.assertLess(
            plan_idx, tool_idx, "'## 任务编排能力' 应在 '## 工具调用规范' 之前"
        )

    def test_rejection_section_after_tool_section(self) -> None:
        """'## 工具被拦截或失败后的行为' 应位于 '## 工具调用规范' 之后。"""
        tool_idx = SYSTEM_PROMPT.find("## 工具调用规范")
        reject_idx = SYSTEM_PROMPT.find("## 工具被拦截或失败后的行为")
        self.assertGreater(tool_idx, 0, "应存在 '## 工具调用规范' 段落")
        self.assertGreater(reject_idx, 0, "应存在 '## 工具被拦截或失败后的行为' 段落")
        self.assertGreater(
            reject_idx, tool_idx, "'## 工具被拦截或失败后的行为' 应在 '## 工具调用规范' 之后"
        )


class TestToolRejectionSection(unittest.TestCase):
    """验证 '## 工具被拦截或失败后的行为' 段落存在且包含 spec 要求的关键说明文字。"""

    def test_section_title_present(self) -> None:
        self.assertIn("## 工具被拦截或失败后的行为", SYSTEM_PROMPT)

    def test_intercepted_marker_keyword(self) -> None:
        """应说明 tool_result 含 [拦截] / [失败] 详情块。"""
        self.assertIn("[拦截]", SYSTEM_PROMPT)
        self.assertIn("[失败]", SYSTEM_PROMPT)

    def test_no_retry_same_tool(self) -> None:
        self.assertIn("不要重试同一个工具", SYSTEM_PROMPT)

    def test_no_retry_with_new_params(self) -> None:
        self.assertIn("不要换参数重试", SYSTEM_PROMPT)

    def test_ask_user_keyword(self) -> None:
        self.assertIn("改为询问用户", SYSTEM_PROMPT)

    def test_absolute_stop_keyword(self) -> None:
        self.assertIn("绝对停止", SYSTEM_PROMPT)


class TestCapabilityBoundarySection(unittest.TestCase):
    """验证 '## 能力边界与拒答' 宪法段存在且显式授权 abstain。"""

    def test_section_title_present(self) -> None:
        self.assertIn("## 能力边界与拒答", SYSTEM_PROMPT)

    def test_abstain_keyword(self) -> None:
        """应包含 abstain 关键字，显式授权拒答。"""
        self.assertIn("abstain", SYSTEM_PROMPT)

    def test_refusal_is_capability_not_failure(self) -> None:
        """应明确说明拒答是能力不是失败。"""
        self.assertIn("不是失败", SYSTEM_PROMPT)

    def test_do_not_fabricate(self) -> None:
        """应禁止编造答案。"""
        self.assertIn("不要编造答案", SYSTEM_PROMPT)

    def test_section_after_core_ability(self) -> None:
        """'## 能力边界与拒答' 应位于 '## 你的核心能力' 之后。"""
        core_idx = SYSTEM_PROMPT.find("## 你的核心能力")
        boundary_idx = SYSTEM_PROMPT.find("## 能力边界与拒答")
        self.assertGreater(core_idx, 0, "应存在 '## 你的核心能力' 段落")
        self.assertGreater(boundary_idx, 0, "应存在 '## 能力边界与拒答' 段落")
        self.assertGreater(
            boundary_idx, core_idx,
            "'## 能力边界与拒答' 应在 '## 你的核心能力' 之后",
        )


class TestSelfExtensionBoundarySection(unittest.TestCase):
    """验证 '## 自主扩展边界' 宪法段存在且列出不可修改的文件。"""

    def test_section_title_present(self) -> None:
        self.assertIn("## 自主扩展边界", SYSTEM_PROMPT)

    def test_src_dir_protected(self) -> None:
        """应列出 src/ 目录不可修改。"""
        self.assertIn("src/", SYSTEM_PROMPT)

    def test_config_files_protected(self) -> None:
        """应列出 config.yaml、.env 不可修改。"""
        self.assertIn("config.yaml", SYSTEM_PROMPT)
        self.assertIn(".env", SYSTEM_PROMPT)

    def test_skill_md_protected(self) -> None:
        """应列出 SKILL.md 不可修改。"""
        self.assertIn("SKILL.md", SYSTEM_PROMPT)

    def test_policy_engine_deny_mentioned(self) -> None:
        """应说明 PolicyEngine 会直接 deny。"""
        self.assertIn("PolicyEngine", SYSTEM_PROMPT)
        self.assertIn("deny", SYSTEM_PROMPT)

    def test_skill_propose_flow_allowed(self) -> None:
        """应说明可通过 propose → validate → confirm 流程扩展能力。"""
        self.assertIn("propose", SYSTEM_PROMPT)
        self.assertIn("validate", SYSTEM_PROMPT)
        self.assertIn("confirm", SYSTEM_PROMPT)

    def test_section_after_source_code_boundary(self) -> None:
        """'## 自主扩展边界' 应位于 '## 自我源码访问边界' 之后。"""
        src_boundary_idx = SYSTEM_PROMPT.find("## 自我源码访问边界")
        ext_boundary_idx = SYSTEM_PROMPT.find("## 自主扩展边界")
        self.assertGreater(src_boundary_idx, 0, "应存在 '## 自我源码访问边界' 段落")
        self.assertGreater(ext_boundary_idx, 0, "应存在 '## 自主扩展边界' 段落")
        self.assertGreater(
            ext_boundary_idx, src_boundary_idx,
            "'## 自主扩展边界' 应在 '## 自我源码访问边界' 之后",
        )


class TestSoftExplorationBudgetRemoved(unittest.TestCase):
    """验证原 L188 软探索预算提示已删除（升级为硬配额，Task 11 实现）。"""

    def test_no_soft_budget_hint(self) -> None:
        """不应再包含软探索预算提示文本。"""
        self.assertNotIn("探索类工具预算", SYSTEM_PROMPT)
        self.assertNotIn("≥8 次时，系统会注入提醒", SYSTEM_PROMPT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
