"""present_content 工具的 13 种 content_type data schema 定义。

用于第 1 层 schema 校验（§5.2），校验 agent 传入的 data 是否符合结构要求。
所有 schema 使用 JSON Schema draft-07 格式。
"""
from __future__ import annotations

# tone 枚举值（§3.4）
TONE_ENUM = ["neutral", "info", "positive", "warning", "critical"]

# status 枚举值（progress steps / dag nodes）
STATUS_ENUM = ["done", "active", "pending", "blocked", "skipped"]

# content_type 枚举（13 种）
CONTENT_TYPES = [
    "metric_group", "table", "timeline", "progress", "comparison",
    "dag_flow", "disclosure_list", "bar_chart", "line_chart", "pie_chart",
    "media", "form", "dashboard",
]

# 各 content_type 的 data schema
CONTENT_TYPE_SCHEMAS: dict[str, dict] = {
    "metric_group": {
        "type": "object",
        "properties": {
            "metrics": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "minLength": 1, "maxLength": 100},
                        "value": {"type": ["string", "number"]},
                        "unit": {"type": "string", "maxLength": 20},
                        "tone": {"enum": TONE_ENUM},
                    },
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["metrics"],
        "additionalProperties": False,
    },

    "table": {
        "type": "object",
        "properties": {
            "columns": {
                "type": "array",
                "minItems": 1,
                "maxItems": 24,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 50},
                        "label": {"type": "string", "minLength": 1, "maxLength": 100},
                        "format": {"enum": ["text", "number", "percent", "status", "duration", "datetime"]},
                    },
                    "required": ["id", "label"],
                    "additionalProperties": False,
                },
            },
            "rows": {
                "type": "array",
                "maxItems": 50,
                "items": {"type": "object"},
            },
        },
        "required": ["columns", "rows"],
        "additionalProperties": False,
    },

    "timeline": {
        "type": "object",
        "properties": {
            "events": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "time": {"type": "string", "maxLength": 50},
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "detail": {"type": "string", "maxLength": 500},
                        "tone": {"enum": TONE_ENUM},
                    },
                    "required": ["time", "title"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["events"],
        "additionalProperties": False,
    },

    "progress": {
        "type": "object",
        "properties": {
            "percent": {"type": "number", "minimum": 0, "maximum": 100},
            "steps": {
                "type": "array",
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "detail": {"type": "string", "maxLength": 500},
                        "status": {"enum": STATUS_ENUM},
                    },
                    "required": ["title"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["percent"],
        "additionalProperties": False,
    },

    "comparison": {
        "type": "object",
        "properties": {
            "left": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 100},
                    "items": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "maxLength": 100},
                                "value": {"type": ["string", "number"]},
                            },
                            "required": ["label", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "items"],
                "additionalProperties": False,
            },
            "right": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "maxLength": 100},
                    "items": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "properties": {
                                "label": {"type": "string", "maxLength": 100},
                                "value": {"type": ["string", "number"]},
                            },
                            "required": ["label", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["title", "items"],
                "additionalProperties": False,
            },
        },
        "required": ["left", "right"],
        "additionalProperties": False,
    },

    "dag_flow": {
        "type": "object",
        "properties": {
            "nodes": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "minLength": 1, "maxLength": 50},
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "status": {"enum": STATUS_ENUM},
                        "depends_on": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["id", "title"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["nodes"],
        "additionalProperties": False,
    },

    "disclosure_list": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "minLength": 1, "maxLength": 200},
                        "detail": {"type": "string", "maxLength": 1000},
                        "tone": {"enum": TONE_ENUM},
                    },
                    "required": ["title", "detail"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["items"],
        "additionalProperties": False,
    },

    "bar_chart": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "maxLength": 100},
                        "value": {"type": "number"},
                        "tone": {"enum": TONE_ENUM},
                    },
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            },
            "unit": {"type": "string", "maxLength": 20},
        },
        "required": ["items"],
        "additionalProperties": False,
    },

    "line_chart": {
        "type": "object",
        "properties": {
            "x_axis": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {"type": "string"},
            },
            "series": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 100},
                        "data": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": 50,
                            "items": {"type": "number"},
                        },
                    },
                    "required": ["name", "data"],
                    "additionalProperties": False,
                },
            },
            "unit": {"type": "string", "maxLength": 20},
        },
        "required": ["x_axis", "series"],
        "additionalProperties": False,
    },

    "pie_chart": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": 50,
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "maxLength": 100},
                        "value": {"type": "number"},
                        "tone": {"enum": TONE_ENUM},
                    },
                    "required": ["label", "value"],
                    "additionalProperties": False,
                },
            },
            "unit": {"type": "string", "maxLength": 20},
        },
        "required": ["items"],
        "additionalProperties": False,
    },

    "media": {
        "type": "object",
        "properties": {
            "type": {"enum": ["image", "video", "audio"]},
            "url": {"type": "string", "minLength": 1, "maxLength": 2000},
            "caption": {"type": "string", "maxLength": 500},
            "fit": {"enum": ["contain", "cover", "fill", "none", "scale-down"]},
            "variant": {"enum": ["icon", "avatar", "smallFeature", "mediumFeature", "largeFeature", "header"]},
        },
        "required": ["type", "url"],
        "additionalProperties": False,
    },

    "form": {
        "type": "object",
        "properties": {
            "fields": {
                "type": "array",
                "minItems": 1,
                "maxItems": 10,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "minLength": 1, "maxLength": 50},
                        "label": {"type": "string", "minLength": 1, "maxLength": 100},
                        "type": {"enum": ["text", "textarea", "number", "select", "checkbox", "date"]},
                        "options": {"type": "array", "items": {"type": "string"}, "maxItems": 50},
                        "default": {},
                        "required": {"type": "boolean"},
                    },
                    "required": ["name", "label", "type"],
                    "additionalProperties": False,
                },
            },
            "submit_label": {"type": "string", "maxLength": 50},
        },
        "required": ["fields"],
        "additionalProperties": False,
    },

    "dashboard": {
        "type": "object",
        "properties": {
            "panels": {
                "type": "array",
                "minItems": 1,
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string", "maxLength": 100},
                        "content_type": {"enum": [ct for ct in CONTENT_TYPES if ct != "dashboard"]},
                        "data": {"type": "object"},
                        "tone": {"enum": TONE_ENUM},
                    },
                    "required": ["title", "content_type", "data"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["panels"],
        "additionalProperties": False,
    },
}


def get_schema(content_type: str) -> dict | None:
    """获取指定 content_type 的 data schema。"""
    return CONTENT_TYPE_SCHEMAS.get(content_type)
