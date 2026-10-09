"""The look every herdr-py page shares (engineview, dagview, coopview): one dark palette, a near-black ground, panels
with a 1 px edge and a faint highlight along the top, low-saturation lines, and small status colours without glow.
Pages draw with these custom properties; the older names the DAG and coop pages use point at the same colours."""

TOKENS = """:root { color-scheme:dark; --bg:#090A0F; --panel:#11141A; --inset:#0C0F14; --line:rgba(255,255,255,.07);
  --hi:rgba(255,255,255,.10); --ink:#E5E7EB; --muted:#9CA3AF; --faint:#6B7280; --wire:#3B4352; --grid:rgba(255,255,255,.06);
  --ice:#93C5FD; --ice-bg:rgba(147,197,253,.10); --ok:#10B981; --ok-bg:rgba(16,185,129,.12); --bad:#EF4444;
  --bad-bg:rgba(239,68,68,.12); --amber:#D4A24C; --amber-bg:rgba(212,162,76,.12);
  --surface:var(--panel); --rule:var(--line); --accent:var(--ice); --pass:var(--ok); --pass-bg:var(--ok-bg); --fail:var(--bad);
  --fail-bg:var(--bad-bg); --wait:var(--amber); --wait-bg:var(--amber-bg); --run-bg:var(--ice-bg);
  --text:var(--ink); --warn:var(--amber); --idle:var(--faint); --chip-ink:#0B0D12; }
"""
SHADOW = "inset 0 1px 0 rgba(255,255,255,.10), 0 10px 30px rgba(0,0,0,.6)"  # a panel's edge highlight and depth
