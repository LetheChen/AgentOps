/**
 * A2UI v1.0 协议层入口（纯 TS，无 Vue 依赖）。
 *
 * 本模块只包含协议层（类型 + 校验 + reducer），不含 Vue 渲染器。
 * 渲染器在 web/src/components/a2ui/ 下。
 *
 * @version v1.0
 */

// IR 类型与枚举（Surface/Importance/Phase/Density/Visibility 等）
export * from "./types.js";
// A2UI v1.0 协议定义（34 组件 + 数据绑定 + 纯函数 + 语义校验）
export * from "./a2ui.js";
// JSON Schema（Draft-07，供 Ajv 校验）
export * from "./schemas.js";
// IR 校验（基于 Ajv）
export * from "./validation.js";
// 纯函数 Reducer（document + transaction apply）
export * from "./reducer.js";
// JSON 值分析
export * from "./json-value.js";
// URI 安全验证
export * from "./artifact-uri.js";
// ViewSpec 渲染协议（与 A2UI 并列，含 AGENTOPS_VIEW_SPEC_VERSION）
export * from "./view-spec.js";
