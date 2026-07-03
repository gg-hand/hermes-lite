/* ============================================================
   theme.js — 主题切换（明暗 + 强调色）
   localStorage 持久化 + 首次跟随 prefers-color-scheme
   ============================================================ */

(function () {
  'use strict';

  const THEME_KEY = 'hermes_theme';        // 'dark' | 'light'
  const ACCENT_KEY = 'hermes_accent';      // 'amber' | 'teal' | 'violet'
  const ACCENTS = ['amber', 'teal', 'violet'];

  const root = document.documentElement;

  /** 读取存储/系统偏好，应用初始主题（在 <head> 内联调用避免闪烁） */
  function initTheme() {
    const storedTheme = localStorage.getItem(THEME_KEY);
    const theme = storedTheme || (window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark');
    const accent = localStorage.getItem(ACCENT_KEY) || 'amber';
    applyTheme(theme, accent);
  }

  function applyTheme(theme, accent) {
    root.dataset.theme = theme;
    root.dataset.accent = accent;
  }

  function getTheme() {
    return root.dataset.theme || 'dark';
  }

  function getAccent() {
    return root.dataset.accent || 'amber';
  }

  function toggleTheme() {
    const next = getTheme() === 'dark' ? 'light' : 'dark';
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next, getAccent());
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next, accent: getAccent() } }));
    return next;
  }

  function setAccent(accent) {
    if (!ACCENTS.includes(accent)) return;
    localStorage.setItem(ACCENT_KEY, accent);
    applyTheme(getTheme(), accent);
    document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: getTheme(), accent } }));
  }

  // 立即初始化（脚本在 head 末尾加载，避免 FOUC）
  initTheme();

  // 监听系统主题变化（仅当用户未显式设置时跟随）
  window.matchMedia('(prefers-color-scheme: light)').addEventListener('change', (e) => {
    if (!localStorage.getItem(THEME_KEY)) {
      const next = e.matches ? 'light' : 'dark';
      applyTheme(next, getAccent());
      document.dispatchEvent(new CustomEvent('themechange', { detail: { theme: next, accent: getAccent() } }));
    }
  });

  // 暴露 API（toggle 与 toggleTheme 同义，兼容 HTML onclick 两种写法）
  window.HermesTheme = {
    init: initTheme,
    toggle: toggleTheme,
    toggleTheme,
    setAccent,
    getTheme,
    getAccent,
    ACCENTS,
  };
})();
