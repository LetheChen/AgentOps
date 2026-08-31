import {
  isSafeGenerativeUiArtifactUri,
  isSafeGenerativeUiPreviewUri,
} from "./artifact-uri.js";

export const AGENTOPS_VIEW_SPEC_VERSION = 1 as const;
export const AGENTOPS_VIEW_SPEC_MAX_BYTES = 64 * 1024;
export const AGENTOPS_VIEW_SPEC_MAX_DEPTH = 8;
export const AGENTOPS_VIEW_SPEC_MAX_NODES = 128;
export const AGENTOPS_VIEW_SPEC_MAX_REPEAT_ITEMS = 50;

export type AgentopsViewGapV1 = "none" | "xs" | "sm" | "md" | "lg";
export type AgentopsViewAlignV1 = "start" | "center" | "end" | "stretch";
export type AgentopsViewToneV1 = "neutral" | "info" | "positive" | "warning" | "critical";
export type AgentopsViewFormatV1 = "text" | "number" | "percent" | "datetime" | "duration" | "status" | "tone";
export type AgentopsViewPrimitiveV1 = string | number | boolean;

export type AgentopsViewValueV1 =
  | { literal: AgentopsViewPrimitiveV1 }
  | { path: string; format?: AgentopsViewFormatV1 }
  | { item_path: string; format?: AgentopsViewFormatV1 };

export type AgentopsViewToneValueV1 = AgentopsViewToneV1 | AgentopsViewValueV1;
export type AgentopsViewArtifactKindV1 = "image" | "html" | "file";
export type AgentopsViewArtifactLayoutV1 = "fluid" | "portrait";

export interface AgentopsViewPredicateV1 {
  path?: string;
  item_path?: string;
  operator: "exists" | "not_empty" | "equals" | "not_equals" | "gt" | "gte" | "lt" | "lte";
  value?: AgentopsViewPrimitiveV1;
}

export interface AgentopsViewNodeBaseV1 {
  id: string;
  span?: 1 | 2 | 3;
  when?: AgentopsViewPredicateV1;
}

export interface AgentopsViewContainerBaseV1 extends AgentopsViewNodeBaseV1 {
  children: AgentopsViewNodeV1[];
}

export type AgentopsViewNodeV1 =
  | (AgentopsViewContainerBaseV1 & { type: "stack"; gap?: AgentopsViewGapV1; align?: AgentopsViewAlignV1 })
  | (AgentopsViewContainerBaseV1 & {
      type: "grid";
      columns: { default: 1 | 2 | 3; compact?: 1 | 2 };
      gap?: AgentopsViewGapV1;
      align?: AgentopsViewAlignV1;
    })
  | (AgentopsViewContainerBaseV1 & { type: "section"; title?: AgentopsViewValueV1; tone?: AgentopsViewToneValueV1 })
  | (AgentopsViewNodeBaseV1 & { type: "heading"; text: AgentopsViewValueV1; level?: 1 | 2 | 3 })
  | (AgentopsViewNodeBaseV1 & { type: "text" | "markdown"; text: AgentopsViewValueV1; max_lines?: number })
  | (AgentopsViewNodeBaseV1 & { type: "icon"; name: AgentopsViewIconV1; tone?: AgentopsViewToneValueV1 })
  | (AgentopsViewNodeBaseV1 & { type: "badge"; text: AgentopsViewValueV1; tone?: AgentopsViewToneValueV1 })
  | (AgentopsViewNodeBaseV1 & { type: "divider" })
  | (AgentopsViewNodeBaseV1 & {
      type: "metric";
      label: AgentopsViewValueV1;
      value: AgentopsViewValueV1;
      unit?: AgentopsViewValueV1;
      tone?: AgentopsViewToneValueV1;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "progress";
      label?: AgentopsViewValueV1;
      value: AgentopsViewValueV1;
      tone?: AgentopsViewToneValueV1;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "list";
      source: string;
      item_title_path: string;
      item_detail_path?: string;
      item_badge_path?: string;
      item_status_path?: string;
      max_items?: number;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "table";
      source: string;
      columns: Array<{ id: string; label: string; path: string; format?: AgentopsViewFormatV1 }>;
      max_items?: number;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "timeline";
      source: string;
      item_title_path: string;
      item_detail_path?: string;
      item_time_path?: string;
      item_status_path?: string;
      max_items?: number;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "bar_chart";
      source: string;
      item_label_path: string;
      item_value_path: string;
      item_tone_path?: string;
      max_items?: number;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "dag";
      source: string;
      item_id_path: string;
      item_label_path: string;
      item_detail_path?: string;
      item_status_path?: string;
      item_progress_path?: string;
      item_depends_on_path: string;
      max_items?: number;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "action";
      action_id: string;
      label: AgentopsViewValueV1;
      style?: "primary" | "secondary" | "danger";
    })
  | (AgentopsViewContainerBaseV1 & { type: "disclosure"; title: AgentopsViewValueV1; open?: boolean })
  | (AgentopsViewNodeBaseV1 & { type: "link"; label: AgentopsViewValueV1; uri: AgentopsViewValueV1 })
  | (AgentopsViewNodeBaseV1 & {
      type: "artifact";
      kind: AgentopsViewArtifactKindV1;
      uri: AgentopsViewValueV1;
      title?: AgentopsViewValueV1;
      description?: AgentopsViewValueV1;
      alt?: AgentopsViewValueV1;
      layout?: AgentopsViewArtifactLayoutV1;
    })
  | (AgentopsViewNodeBaseV1 & {
      type: "repeat";
      source: string;
      max_items?: number;
      item: AgentopsViewNodeV1;
      columns?: { default: 1 | 2 | 3; compact?: 1 | 2 };
      gap?: AgentopsViewGapV1;
      align?: AgentopsViewAlignV1;
    });

export type AgentopsViewIconV1 =
  | "activity" | "alert" | "check" | "clock" | "database" | "external-link"
  | "file" | "git" | "monitor" | "pause" | "play" | "search" | "server"
  | "settings" | "shield" | "sparkles" | "user" | "x";

export interface AgentopsViewSpecV1 {
  view_version: 1;
  root: AgentopsViewNodeV1;
}

export interface AgentopsViewSpecSemanticIssueV1 {
  path: string;
  message: string;
  keyword: string;
}

export interface AgentopsViewModelItemV1 {
  id: string;
  title?: string;
  detail?: string;
  badge?: string;
  status?: string;
  value?: number;
  tone?: AgentopsViewToneV1;
  depends_on?: string[];
  cells?: Array<{ id: string; value: string }>;
}

export interface AgentopsViewModelNodeV1 {
  id: string;
  type: AgentopsViewNodeV1["type"];
  span: 1 | 2 | 3;
  gap?: AgentopsViewGapV1;
  align?: AgentopsViewAlignV1;
  columns?: { default: 1 | 2 | 3; compact?: 1 | 2 };
  title?: string;
  text?: string;
  label?: string;
  value?: string;
  raw_value?: AgentopsViewPrimitiveV1;
  unit?: string;
  tone?: AgentopsViewToneV1;
  level?: 1 | 2 | 3;
  max_lines?: number;
  name?: AgentopsViewIconV1;
  progress?: number;
  items?: AgentopsViewModelItemV1[];
  table_columns?: Array<{ id: string; label: string }>;
  action_id?: string;
  style?: "primary" | "secondary" | "danger";
  open?: boolean;
  uri?: string;
  artifact_kind?: AgentopsViewArtifactKindV1;
  description?: string;
  alt?: string;
  layout?: AgentopsViewArtifactLayoutV1;
  children?: AgentopsViewModelNodeV1[];
}

export interface AgentopsViewModelV1 {
  view_version: 1;
  root: AgentopsViewModelNodeV1;
  node_count: number;
}

const NODE_BASE_KEYS = new Set(["id", "type", "span", "when"]);
const NODE_KEYS: Record<AgentopsViewNodeV1["type"], readonly string[]> = {
  stack: ["children", "gap", "align"],
  grid: ["children", "columns", "gap", "align"],
  section: ["children", "title", "tone"],
  heading: ["text", "level"],
  text: ["text", "max_lines"],
  markdown: ["text", "max_lines"],
  icon: ["name", "tone"],
  badge: ["text", "tone"],
  divider: [],
  metric: ["label", "value", "unit", "tone"],
  progress: ["label", "value", "tone"],
  list: ["source", "item_title_path", "item_detail_path", "item_badge_path", "item_status_path", "max_items"],
  table: ["source", "columns", "max_items"],
  timeline: ["source", "item_title_path", "item_detail_path", "item_time_path", "item_status_path", "max_items"],
  bar_chart: ["source", "item_label_path", "item_value_path", "item_tone_path", "max_items"],
  dag: ["source", "item_id_path", "item_label_path", "item_detail_path", "item_status_path", "item_progress_path", "item_depends_on_path", "max_items"],
  action: ["action_id", "label", "style"],
  disclosure: ["title", "children", "open"],
  link: ["label", "uri"],
  artifact: ["kind", "uri", "title", "description", "alt", "layout"],
  repeat: ["source", "max_items", "item", "columns", "gap", "align"],
};
const NODE_REQUIRED_KEYS: Record<AgentopsViewNodeV1["type"], readonly string[]> = {
  stack: ["children"], grid: ["children", "columns"], section: ["children"], heading: ["text"],
  text: ["text"], markdown: ["text"], icon: ["name"], badge: ["text"], divider: [],
  metric: ["label", "value"], progress: ["value"], list: ["source", "item_title_path"],
  table: ["source", "columns"], timeline: ["source", "item_title_path"],
  bar_chart: ["source", "item_label_path", "item_value_path"],
  dag: ["source", "item_id_path", "item_label_path", "item_depends_on_path"],
  action: ["action_id", "label"], disclosure: ["title", "children"], link: ["label", "uri"],
  artifact: ["kind", "uri"],
  repeat: ["source", "item"],
};

function pointer(root: unknown, path: string): unknown {
  if (path === "") return root;
  let current = root;
  for (const token of path.slice(1).split("/").map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"))) {
    if (!current || typeof current !== "object") return undefined;
    if (Array.isArray(current)) {
      if (!/^(?:0|[1-9][0-9]*)$/.test(token)) return undefined;
      current = current[Number(token)];
    } else {
      current = (current as Record<string, unknown>)[token];
    }
  }
  return current;
}

function isPointer(value: unknown): value is string {
  return typeof value === "string" && /^(?:\/(?:[^~/]|~[01])*)*$/.test(value) && value.length <= 500;
}

function bindingUsesItem(value: unknown): boolean {
  return Boolean(value && typeof value === "object" && !Array.isArray(value) && "item_path" in value);
}

function nodeChildren(node: AgentopsViewNodeV1): AgentopsViewNodeV1[] {
  if (node.type === "repeat") return [node.item];
  if ("children" in node) return node.children;
  return [];
}

function bindingFields(node: AgentopsViewNodeV1): unknown[] {
  return [
    "title" in node ? node.title : undefined,
    "text" in node ? node.text : undefined,
    "label" in node ? node.label : undefined,
    "value" in node ? node.value : undefined,
    "unit" in node ? node.unit : undefined,
    "tone" in node ? node.tone : undefined,
    "uri" in node ? node.uri : undefined,
    "description" in node ? node.description : undefined,
    "alt" in node ? node.alt : undefined,
  ];
}

export function analyzeAgentopsViewSpecSemantics(
  view: AgentopsViewSpecV1,
  options: { action_ids?: ReadonlySet<string>; path?: string } = {},
): AgentopsViewSpecSemanticIssueV1[] {
  const rootPath = options.path ?? "";
  const issues: AgentopsViewSpecSemanticIssueV1[] = [];
  const ids = new Set<string>();
  let count = 0;
  const issue = (path: string, message: string, keyword: string) => issues.push({ path: `${rootPath}${path}`, message, keyword });
  const visit = (node: AgentopsViewNodeV1, path: string, depth: number, inRepeat: boolean): void => {
    count += 1;
    if (depth > AGENTOPS_VIEW_SPEC_MAX_DEPTH) issue(path, `view depth exceeds ${AGENTOPS_VIEW_SPEC_MAX_DEPTH}`, "maxViewDepth");
    if (count > AGENTOPS_VIEW_SPEC_MAX_NODES) issue(path, `view node count exceeds ${AGENTOPS_VIEW_SPEC_MAX_NODES}`, "maxViewNodes");
    if (ids.has(node.id)) issue(`${path}/id`, `duplicate view node id: ${node.id}`, "uniqueViewNodeId");
    ids.add(node.id);
    const allowed = new Set([...NODE_BASE_KEYS, ...(NODE_KEYS[node.type] ?? [])]);
    for (const key of Object.keys(node)) {
      if (!allowed.has(key)) issue(`${path}/${key}`, `${key} is not valid for ${node.type}`, "viewNodeShape");
    }
    for (const key of NODE_REQUIRED_KEYS[node.type] ?? []) {
      if (!Object.prototype.hasOwnProperty.call(node, key)) issue(`${path}/${key}`, `${node.type} requires ${key}`, "required");
    }
    for (const [index, value] of bindingFields(node).entries()) {
      if (bindingUsesItem(value) && !inRepeat) issue(`${path}/binding/${index}`, "item_path is only valid inside repeat", "itemBindingScope");
    }
    if (node.when) {
      const hasPath = Object.prototype.hasOwnProperty.call(node.when, "path");
      const hasItemPath = Object.prototype.hasOwnProperty.call(node.when, "item_path");
      if (hasPath === hasItemPath) issue(`${path}/when`, "when requires exactly one path or item_path", "predicateSource");
      if (hasItemPath && !inRepeat) issue(`${path}/when/item_path`, "item_path is only valid inside repeat", "itemBindingScope");
      if ((node.when.operator === "equals" || node.when.operator === "not_equals" || ["gt", "gte", "lt", "lte"].includes(node.when.operator)) && node.when.value === undefined) {
        issue(`${path}/when/value`, `${node.when.operator} requires value`, "predicateValue");
      }
    }
    if (node.type === "action" && options.action_ids && !options.action_ids.has(node.action_id)) {
      issue(`${path}/action_id`, `view references unavailable action: ${node.action_id}`, "viewActionReference");
    }
    if (node.type === "repeat" && (node.max_items ?? 16) > AGENTOPS_VIEW_SPEC_MAX_REPEAT_ITEMS) {
      issue(`${path}/max_items`, `repeat exceeds ${AGENTOPS_VIEW_SPEC_MAX_REPEAT_ITEMS} items`, "maxRepeatItems");
    }
    for (const [index, child] of nodeChildren(node).entries()) {
      visit(child, `${path}/${node.type === "repeat" ? "item" : `children/${index}`}`, depth + 1, inRepeat || node.type === "repeat");
    }
  };
  visit(view.root, "/root", 1, false);
  return issues;
}

function rawBinding(binding: AgentopsViewValueV1, content: Record<string, unknown>, item: unknown): unknown {
  if ("literal" in binding) return binding.literal;
  if ("item_path" in binding) return pointer(item, binding.item_path);
  return pointer(content, binding.path);
}

function formatValue(value: unknown, format: AgentopsViewFormatV1 | undefined, locale?: string): { text: string; raw?: AgentopsViewPrimitiveV1 } {
  const raw = typeof value === "string" || typeof value === "number" || typeof value === "boolean" ? value : undefined;
  if (format === "number") return typeof value === "number" && Number.isFinite(value)
    ? { text: new Intl.NumberFormat(locale, { maximumFractionDigits: 2 }).format(value), raw: value }
    : { text: "" };
  if (format === "percent") return typeof value === "number" && Number.isFinite(value)
    ? { text: `${new Intl.NumberFormat(locale, { maximumFractionDigits: 1 }).format(value)}%`, raw: value }
    : { text: "" };
  if (format === "datetime") {
    const date = typeof value === "string" || typeof value === "number" ? new Date(value) : undefined;
    return date && Number.isFinite(date.getTime()) ? { text: new Intl.DateTimeFormat(locale, { dateStyle: "medium", timeStyle: "short" }).format(date), ...(raw !== undefined ? { raw } : {}) } : { text: "" };
  }
  if (format === "duration") {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return { text: "" };
    const seconds = Math.round(value);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remainder = seconds % 60;
    return { text: [hours ? `${hours}h` : "", minutes ? `${minutes}m` : "", remainder || (!hours && !minutes) ? `${remainder}s` : ""].filter(Boolean).join(" "), raw: value };
  }
  if (raw === undefined) return { text: "" };
  return { text: String(raw).trim().slice(0, 4000), raw };
}

function binding(binding: AgentopsViewValueV1 | undefined, content: Record<string, unknown>, item: unknown, locale?: string): { text: string; raw?: AgentopsViewPrimitiveV1 } {
  if (!binding) return { text: "" };
  return formatValue(rawBinding(binding, content, item), "format" in binding ? binding.format : undefined, locale);
}

function toneFrom(value: unknown): AgentopsViewToneV1 {
  const normalized = String(value ?? "").trim().toLowerCase();
  if (["positive", "passed", "ready", "resolved", "verified", "succeeded", "success", "complete", "completed"].includes(normalized)) return "positive";
  if (["critical", "failed", "error", "danger", "changes_requested"].includes(normalized)) return "critical";
  if (["warning", "blocked", "pending", "waiting", "skipped"].includes(normalized)) return "warning";
  if (["info", "running", "active", "progress"].includes(normalized)) return "info";
  return "neutral";
}

function resolvedTone(value: AgentopsViewToneValueV1 | undefined, content: Record<string, unknown>, item: unknown): AgentopsViewToneV1 | undefined {
  if (!value) return undefined;
  if (typeof value === "string") return value;
  return toneFrom(rawBinding(value, content, item));
}

function visible(predicate: AgentopsViewPredicateV1 | undefined, content: Record<string, unknown>, item: unknown): boolean {
  if (!predicate) return true;
  const hasItemPath = Object.prototype.hasOwnProperty.call(predicate, "item_path");
  const value = pointer(hasItemPath ? item : content, predicate.item_path ?? predicate.path ?? "");
  if (predicate.operator === "exists") return value !== undefined && value !== null;
  if (predicate.operator === "not_empty") return Array.isArray(value) ? value.length > 0 : typeof value === "string" ? value.trim().length > 0 : Boolean(value);
  if (predicate.operator === "equals") return Object.is(value, predicate.value);
  if (predicate.operator === "not_equals") return !Object.is(value, predicate.value);
  if (typeof value !== "number" || typeof predicate.value !== "number") return false;
  if (predicate.operator === "gt") return value > predicate.value;
  if (predicate.operator === "gte") return value >= predicate.value;
  if (predicate.operator === "lt") return value < predicate.value;
  return value <= predicate.value;
}

function sourceItems(content: Record<string, unknown>, source: string, maxItems = 16): unknown[] {
  const value = pointer(content, source);
  return (Array.isArray(value) ? value : []).slice(0, Math.max(0, Math.min(AGENTOPS_VIEW_SPEC_MAX_REPEAT_ITEMS, maxItems)));
}

function itemText(item: unknown, pathValue: string | undefined, locale?: string, format?: AgentopsViewFormatV1): string {
  if (!pathValue) return "";
  return formatValue(pointer(item, pathValue), format, locale).text.slice(0, 500);
}

function itemNumber(item: unknown, pathValue: string | undefined): number {
  const value = pathValue ? pointer(item, pathValue) : undefined;
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

export function buildAgentopsViewModel(
  view: AgentopsViewSpecV1,
  content: Record<string, unknown>,
  options: { locale?: string } = {},
): AgentopsViewModelV1 {
  let count = 0;
  const materialize = (node: AgentopsViewNodeV1, item: unknown, suffix = ""): AgentopsViewModelNodeV1 | undefined => {
    if (!visible(node.when, content, item)) return undefined;
    count += 1;
    if (count > AGENTOPS_VIEW_SPEC_MAX_NODES) throw new Error(`Materialized view exceeds ${AGENTOPS_VIEW_SPEC_MAX_NODES} nodes`);
    const model: AgentopsViewModelNodeV1 = { id: `${node.id}${suffix}`, type: node.type, span: node.span ?? 1 };
    if ("gap" in node) model.gap = node.gap;
    if ("align" in node) model.align = node.align;
    if ((node.type === "grid" || node.type === "repeat") && node.columns) {
      model.columns = structuredClone(node.columns);
    }
    if ("tone" in node) model.tone = resolvedTone(node.tone, content, item);
    if (node.type === "stack" || node.type === "grid" || node.type === "section" || node.type === "disclosure") {
      if (node.type === "section") model.title = binding(node.title, content, item, options.locale).text;
      if (node.type === "disclosure") {
        model.title = binding(node.title, content, item, options.locale).text;
        model.open = node.open === true;
      }
      model.children = node.children.flatMap((child) => {
        const result = materialize(child, item, suffix);
        return result ? [result] : [];
      });
      return model;
    }
    if (node.type === "repeat") {
      model.gap = node.gap ?? "sm";
      model.children = sourceItems(content, node.source, node.max_items).flatMap((entry, index) => {
        const result = materialize(node.item, entry, `${suffix}:${index}`);
        return result ? [result] : [];
      });
      return model;
    }
    if (node.type === "heading" || node.type === "text" || node.type === "markdown" || node.type === "badge") {
      model.text = binding(node.text, content, item, options.locale).text;
      if (node.type === "heading") model.level = node.level ?? 2;
      if (node.type === "text" || node.type === "markdown") model.max_lines = node.max_lines;
      return model.text ? model : undefined;
    }
    if (node.type === "icon") { model.name = node.name; return model; }
    if (node.type === "divider") return model;
    if (node.type === "metric") {
      model.label = binding(node.label, content, item, options.locale).text;
      const value = binding(node.value, content, item, options.locale);
      model.value = value.text; model.raw_value = value.raw; model.unit = binding(node.unit, content, item, options.locale).text;
      return model.label || model.value ? model : undefined;
    }
    if (node.type === "progress") {
      model.label = binding(node.label, content, item, options.locale).text;
      const value = binding(node.value, content, item, options.locale);
      const numeric = Number(value.raw);
      model.progress = Number.isFinite(numeric) ? Math.max(0, Math.min(100, numeric)) : 0;
      model.value = value.text;
      return model;
    }
    if (node.type === "list" || node.type === "timeline") {
      model.items = sourceItems(content, node.source, node.max_items).map((entry, index) => ({
        id: `${model.id}:${index}`,
        title: itemText(entry, node.item_title_path, options.locale),
        detail: itemText(entry, node.item_detail_path, options.locale),
        badge: node.type === "list" ? itemText(entry, node.item_badge_path, options.locale) : itemText(entry, node.item_time_path, options.locale),
        status: itemText(entry, node.item_status_path, options.locale),
        tone: toneFrom(itemText(entry, node.item_status_path, options.locale)),
      })).filter((entry) => entry.title || entry.detail);
      return model;
    }
    if (node.type === "table") {
      model.table_columns = node.columns.map((column) => ({ id: column.id, label: column.label }));
      model.items = sourceItems(content, node.source, node.max_items).map((entry, index) => ({
        id: `${model.id}:${index}`,
        cells: node.columns.map((column) => ({ id: column.id, value: itemText(entry, column.path, options.locale, column.format) })),
      }));
      return model;
    }
    if (node.type === "bar_chart") {
      model.items = sourceItems(content, node.source, node.max_items).map((entry, index) => ({
        id: `${model.id}:${index}`,
        title: itemText(entry, node.item_label_path, options.locale),
        value: itemNumber(entry, node.item_value_path),
        tone: toneFrom(itemText(entry, node.item_tone_path, options.locale)),
      })).filter((entry) => entry.title);
      return model;
    }
    if (node.type === "dag") {
      model.items = sourceItems(content, node.source, node.max_items).map((entry, index) => {
        const id = itemText(entry, node.item_id_path, options.locale) || `${model.id}:${index}`;
        const dependencies = pointer(entry, node.item_depends_on_path);
        return {
          id,
          title: itemText(entry, node.item_label_path, options.locale) || id,
          detail: itemText(entry, node.item_detail_path, options.locale),
          status: itemText(entry, node.item_status_path, options.locale),
          value: Math.max(0, Math.min(100, itemNumber(entry, node.item_progress_path))),
          tone: toneFrom(itemText(entry, node.item_status_path, options.locale)),
          depends_on: (Array.isArray(dependencies) ? dependencies : []).filter((value): value is string => typeof value === "string").slice(0, 8),
        };
      });
      return model;
    }
    if (node.type === "action") {
      model.action_id = node.action_id; model.label = binding(node.label, content, item, options.locale).text; model.style = node.style ?? "secondary";
      return model.label ? model : undefined;
    }
    if (node.type === "link") {
      model.label = binding(node.label, content, item, options.locale).text;
      const uri = binding(node.uri, content, item, options.locale).text;
      model.uri = uri && isSafeGenerativeUiArtifactUri(uri) ? uri : undefined;
      return model.label && model.uri ? model : undefined;
    }
    if (node.type === "artifact") {
      model.artifact_kind = node.kind;
      model.title = binding(node.title, content, item, options.locale).text;
      model.description = binding(node.description, content, item, options.locale).text;
      model.alt = binding(node.alt, content, item, options.locale).text || model.title;
      model.layout = node.layout ?? "fluid";
      const uri = binding(node.uri, content, item, options.locale).text;
      model.uri = uri && isSafeGenerativeUiPreviewUri(uri) ? uri : undefined;
      return model.uri ? model : undefined;
    }
    return model;
  };
  const root = materialize(view.root, undefined);
  if (!root) throw new Error("ViewSpec root did not materialize");
  return { view_version: 1, root, node_count: count };
}

export function isAgentopsViewJsonPointer(value: unknown): value is string {
  return isPointer(value);
}
