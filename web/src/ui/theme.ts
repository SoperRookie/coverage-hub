import { computed, ref, watchEffect } from "vue";

// 主题：浅色 / 深色 / 跟随系统。选择存在 localStorage，切换只动 <html> 上的 class 与属性，
// 颜色全靠 base.css 里的 token（Element Plus 的深色变量也挂在 html.dark 上）。
// 系列色与状态色不随主题变（colors.ts），变的只有底、面、线、字。
export type ThemeChoice = "light" | "dark" | "system";
const KEY = "covhub.theme";

function readChoice(): ThemeChoice {
  // 地址里带 theme=light|dark|system 时以它为准并记住（打印 / 截图 / 贴链接时省得先去点按钮）
  const m = /[?&]theme=(light|dark|system)/.exec(location.hash + location.search);
  if (m) return m[1] as ThemeChoice;
  try {
    const v = localStorage.getItem(KEY);
    if (v === "light" || v === "dark" || v === "system") return v;
  } catch { /* 隐私模式等拿不到 localStorage，按跟随系统 */ }
  return "system";
}

export const theme = ref<ThemeChoice>(readChoice());
const media = window.matchMedia("(prefers-color-scheme: dark)");
const systemDark = ref(media.matches);
media.addEventListener("change", (e) => { systemDark.value = e.matches; });

export const isDark = computed(() => theme.value === "dark" || (theme.value === "system" && systemDark.value));

watchEffect(() => {
  const root = document.documentElement;
  root.classList.toggle("dark", isDark.value);
  root.setAttribute("data-theme", isDark.value ? "dark" : "light");
  try { localStorage.setItem(KEY, theme.value); } catch { /* 同上 */ }
});

/** ECharts 拿不到 CSS 变量，画图时从 <html> 上现读一份 token。 */
export function chartTokens() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string) => css.getPropertyValue(name).trim();
  return {
    surface: v("--surface"), line: v("--line"), lineStrong: v("--line-strong"), grid: v("--grid"),
    ink: v("--ink"), ink2: v("--ink-2"), ink3: v("--ink-3"),
  };
}
