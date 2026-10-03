/* 知屿 KnowIsle Tailwind Play CDN 设计令牌（v1.0 设计体系统一）。
 * 须在 vendor/tailwind-*.js 之后、任何使用扩展类的页面渲染前加载（defer 按序）。
 * CSP 兼容：外置文件，无内联脚本。
 *
 * 令牌约定：
 * - brand = 品牌主色（青蓝，等同 sky 色阶，后续换主题只改这里）；
 * - shadow-card / shadow-lift：卡片静态 / 悬浮两级阴影；
 * - animate-fade-up / animate-pulse-dot：入场与打字点动画（keyframes 定义在 main.css）；
 * - 骨架屏 shimmer：main.css `.skeleton` 自带 keyframes，无需令牌。
 * - rounded-card：卡片统一圆角。
 */
tailwind.config = {
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#f0f9ff', 100: '#e0f2fe', 200: '#bae6fd', 300: '#7dd3fc',
          400: '#38bdf8', 500: '#0ea5e9', 600: '#0284c7', 700: '#0369a1',
          800: '#075985', 900: '#0c4a6e',
        },
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(15 23 42 / 0.05)',
        lift: '0 8px 24px -6px rgb(2 132 199 / 0.15), 0 2px 6px -2px rgb(15 23 42 / 0.08)',
      },
      borderRadius: {
        card: '0.75rem',
      },
      animation: {
        'fade-up': 'fade-up 0.35s ease-out both',
        'pulse-dot': 'pulse-dot 1.2s ease-in-out infinite',
      },
    },
  },
};
