/* 知屿 KnowIsle 设计令牌 v4：品牌色 + 语义表面/文字/边框/状态，零构建。 */
tailwind.config = {
  theme: {
    extend: {
      colors: {
        brand: {
          50: '#ecfdf5', 100: '#d1fae5', 200: '#a7f3d0', 300: '#6ee7b7',
          400: '#34d399', 500: '#10b981', 600: '#047857', 700: '#046c52',
          800: '#065f46', 900: '#064e3b', 950: '#022c22',
        },
        accent: {
          50: '#fdf9ec', 100: '#faf0cd', 200: '#f4df97', 300: '#eec95c',
          400: '#e8b23a', 500: '#dd9424', 600: '#c47420', 700: '#a3541e',
          800: '#86431f', 900: '#6f381c',
        },
        surface: {
          page: '#f6f8f7', raised: '#ffffff', inset: '#eef3f0', subtle: '#f1f5f3',
        },
        primary: '#17262e', secondary: '#405462', muted: '#526873', disabled: '#9aa9a2',
        inverse: '#17262e',
        slate: {400: '#526873', 500: '#526873'},
        default: '#d7e1dc', strong: '#a9bab2',
        success: '#047857', warning: '#a3541e', danger: '#b4232a', info: '#1d5f85',
        'danger-soft': '#fee4e2', 'danger-border': '#f5b8bc', 'danger-strong': '#911c23',
      },
      boxShadow: {
        card: '0 1px 2px 0 rgb(23 38 46 / 0.05)',
        lift: '0 12px 32px -14px rgb(23 38 46 / 0.18), 0 2px 8px -4px rgb(23 38 46 / 0.08)',
      },
      borderRadius: {card: '0.875rem'},
      fontSize: {
        display: ['2.75rem', {lineHeight: '1.12', letterSpacing: '-0.025em', fontWeight: '750'}],
        'display-sm': ['2.125rem', {lineHeight: '1.18', letterSpacing: '-0.02em', fontWeight: '750'}],
        title: ['1.75rem', {lineHeight: '1.28', letterSpacing: '-0.012em', fontWeight: '700'}],
        'card-title': ['1.0625rem', {lineHeight: '1.45', fontWeight: '650'}],
        body: ['0.9375rem', {lineHeight: '1.75'}],
        caption: ['0.75rem', {lineHeight: '1.55'}],
        num: ['1.5rem', {lineHeight: '1.2', fontWeight: '700'}],
        code: ['0.8125rem', {lineHeight: '1.65'}],
      },
      spacing: {section: '2.5rem', reading: '48rem', wide: '80rem'},
      transitionTimingFunction: {island: 'cubic-bezier(0.22, 1, 0.36, 1)'},
      transitionDuration: {island: '200ms'},
      animation: {
        'fade-up': 'fade-up 0.2s cubic-bezier(0.22, 1, 0.36, 1) both',
        'fade-in': 'fade-in 0.18s ease-out both',
        'scale-in': 'scale-in 0.2s cubic-bezier(0.22, 1, 0.36, 1) both',
        'pulse-dot': 'pulse-dot 1.2s ease-in-out infinite',
      },
    },
  },
};
