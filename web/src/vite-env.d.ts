/// <reference types="vite/client" />

// Vite 5 `?raw` import：返回原始文件内容的字符串 default export。
// 用于绕过 __vite__updateStyle 对新 CSS 模块静默丢弃的 dev server bug
// （CSS 模块 dev 注入问题：prod build 走 esbuild 打包无影响）。
declare module '*.css?raw' {
  const content: string;
  export default content;
}