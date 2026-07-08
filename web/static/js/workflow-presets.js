/* ============================================================
   workflow-presets.js — Workflow 模板预设共享模块
   供 /workflow 编辑器与 /scheduler "新建调度" 弹窗共同使用
   暴露到 window.WORKFLOW_PRESETS / window.WORKFLOW_STEP_TYPES
   ============================================================ */

(function () {
  'use strict';

  const WORKFLOW_PRESETS = [
    {
      id: 'daily_news',
      name: '每日新闻两步流',
      description: '先调用搜索工具采集新闻，再让 LLM 总结成简报',
      difficulty: 'beginner',
      tags: ['llm', 'tool'],
      spec: {
        name: 'daily_news',
        steps: [
          { id: 'fetch', name: '抓取新闻', type: 'tool', config: { tool: 'web_search', input: { query: '今日热点新闻' } } },
          { id: 'summarize', name: '生成简报', type: 'llm', depends_on: ['fetch'], config: { prompt: '将上一步新闻整理成 300 字简报' } }
        ]
      }
    },
    {
      id: 'weekly_report',
      name: '周报生成',
      description: '从记忆库检索本周会话要点，LLM 生成结构化周报',
      difficulty: 'intermediate',
      tags: ['llm', 'react'],
      spec: {
        name: 'weekly_report',
        steps: [
          { id: 'recall', name: '回忆本周', type: 'react', config: { task: '检索本周重要会话要点', max_loops: 3 } },
          { id: 'draft', name: '起草周报', type: 'llm', depends_on: ['recall'], config: { prompt: '基于检索结果生成周报，分进度/问题/计划三段' } }
        ]
      }
    },
    {
      id: 'dir_watch_email',
      name: '目录监控+邮件',
      description: '监控目录变更并发送邮件通知，纯确定性步骤',
      difficulty: 'beginner',
      tags: ['deterministic'],
      spec: {
        name: 'dir_watch_email',
        steps: [
          { id: 'watch', name: '监控目录', type: 'deterministic', config: { template: 'directory_watch', path: './data' } },
          { id: 'notify', name: '发送邮件', type: 'deterministic', depends_on: ['watch'], config: { template: 'email_notify', to: 'admin@local' } }
        ]
      }
    },
    {
      id: 'code_review',
      name: '代码 review',
      description: 'ReactLoop 自主审查代码改动并输出报告',
      difficulty: 'advanced',
      tags: ['react'],
      spec: {
        name: 'code_review',
        steps: [
          { id: 'review', name: '审查改动', type: 'react', config: { task: '审查最近一次 git diff，指出问题', max_loops: 5, tool_whitelist: ['shell', 'file_read'] } }
        ]
      }
    },
    {
      id: 'backup_cleanup',
      name: '数据备份清理',
      description: '备份数据目录后清理过期文件，两步确定性流程',
      difficulty: 'intermediate',
      tags: ['tool', 'deterministic'],
      spec: {
        name: 'backup_cleanup',
        steps: [
          { id: 'backup', name: '执行备份', type: 'tool', config: { tool: 'shell', input: { command: 'tar -czf backup.tar.gz ./data' } } },
          { id: 'cleanup', name: '清理过期', type: 'deterministic', depends_on: ['backup'], config: { template: 'cleanup_suggest', max_age_days: 30 } }
        ]
      }
    }
  ];

  const WORKFLOW_STEP_TYPES = ['deterministic', 'llm', 'tool', 'react', 'subworkflow'];

  // 暴露到 window
  window.WORKFLOW_PRESETS = WORKFLOW_PRESETS;
  window.WORKFLOW_STEP_TYPES = WORKFLOW_STEP_TYPES;
})();
