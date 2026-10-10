/* 知屿 KnowIsle Tailwind Play CDN 设计令牌（v1.1「翡翠绿·海岛」设计令牌 v2）。
 * 须在 vendor/tailwind-*.js 之后、任何使用扩展类的页面渲染前加载（defer 按序）。
 * CSP 兼容：外置文件，无内联脚本。
 *
 * 令牌约定（后续批次页面统一遵守）：
 * - brand  = 翡翠绿系品牌主色十档 + 950（主色 600）；旧 sky-* 硬编码一律改走 brand；
 * - accent = 暖沙色辅助色板十档（点缀 / 精华 / 悬赏 / 高亮场景）；
 * - shadow-card / shadow-lift：卡片静态 / 悬浮两级阴影（brand 系色调）；
 * - rounded-card：卡片统一圆角；
 * - ease-island / duration-island：统一动效曲线与时长；
 * - animate-fade-up / animate-fade-in / animate-scale-in / animate-pulse-dot：
 *   入场与打字点动画（keyframes 实体定义在 main.css，Play CDN 按需生成 keyframes
 *   不可靠，令牌仅引用名字）；
 * - 骨架屏 shimmer：main.css `.skeleton` 自带 keyframes，无需令牌；
 * - text-display / text-display-sm：Hero 大标题字级令牌。
 */
tailwind.config = {
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#ecfdf5', 100: '#d1fae5', 200: '#a7f3d0', 300: '#6ee7b7',
          400: '#34d399', 500: '#10b981', 600: '#059669', 700: '#047857',
          800: '#065f46', 900: '#064e3b', 950: '#022c22',
        },
        accent: {
          50: '#fdf9ec', 100: '#faf0cd', 200: '#f4df97', 300: '#eec95c',
          400: '#e8b23a', 500: '#dd9424', 600: '#c47420', 700: '#a3541e',
          800: '#86431f', 900: '#6f381c',
        },
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(2 44 34 / 0.06)',
        lift: '0 10px 28px -8px rgb(5 150 105 / 0.18), 0 2px 8px -2px rgb(2 44 34 / 0.08)',
      },
      borderRadius: {
        card: '0.75rem',
      },
      fontSize: {
        display: ['2.5rem', { lineHeight: '1.2', letterSpacing: '-0.02em', fontWeight: '700' }],
        'display-sm': ['2rem', { lineHeight: '1.25', letterSpacing: '-0.02em', fontWeight: '700' }],
      },
      transitionTimingFunction: {
        island: 'cubic-bezier(0.22, 1, 0.36, 1)',
      },
      transitionDuration: {
        island: '200ms',
      },
      animation: {
        'fade-up': 'fade-up 0.35s cubic-bezier(0.22, 1, 0.36, 1) both',
        'fade-in': 'fade-in 0.3s ease-out both',
        'scale-in': 'scale-in 0.25s cubic-bezier(0.22, 1, 0.36, 1) both',
        'pulse-dot': 'pulse-dot 1.2s ease-in-out infinite',
      },
    },
  },
};
