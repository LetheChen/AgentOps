import sqlite3, json
con = sqlite3.connect("audit.db")
# 找 #946af18d 卡片（用户报告的表格卡）
rows = con.execute(
    "SELECT session_id, sequence, payload FROM session_events "
    "WHERE event_type='report_surface_state' AND payload LIKE '%946af18d%' "
    "ORDER BY sequence"
).fetchall()
for sid, seq, payload_raw in rows:
    d = json.loads(payload_raw)
    ss = d.get("surface_state") or {}
    print(f"\n=== session={sid} seq={seq} ===")
    print(f"view={ss.get('view_id')} phase={ss.get('phase')}")
    comps = ss.get("components") or []
    print(f"components count: {len(comps)}")
    print(f"full components JSON:")
    print(json.dumps(comps, ensure_ascii=False, indent=2))
    print(f"\nfull data_model JSON:")
    print(json.dumps(ss.get("data_model") or {}, ensure_ascii=False, indent=2))