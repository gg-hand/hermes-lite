/* ============================================================
   chat-schedule-badge.js — chat.html 顶栏调度徽章轮询
   仅在 /chat 页面加载，轮询 pending 提议 + 待审查 cron_tool 数量
   ============================================================ */

let _badgeTimer = null;

async function _pollScheduleBadge() {
  try {
    const [proposals, cronPending] = await Promise.all([
      api('/proposals'),
      api('/cron_tools/pending'),
    ]);
    const count = (proposals.proposals || []).filter(p => p.status === 'pending_confirm').length
                + (cronPending.pending || []).length;
    const badge = document.getElementById('scheduleBadge');
    if (!badge) return;
    if (count > 0) { badge.textContent = count; badge.style.display = ''; }
    else { badge.style.display = 'none'; }
  } catch (e) {
    // 静默失败，不打扰用户
  }
}

document.addEventListener('DOMContentLoaded', () => {
  _pollScheduleBadge();
  _badgeTimer = setInterval(_pollScheduleBadge, 60000);
});
