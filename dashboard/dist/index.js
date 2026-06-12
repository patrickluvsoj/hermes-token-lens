/**
 * Token Lens — Hermes dashboard plugin UI.
 *
 * Plain IIFE, no build step (kanban pattern). Uses window.__HERMES_PLUGIN_SDK__
 * for React + primitives; all colors/fonts via --tl-* vars mapped to Hermes
 * theme tokens (design review D12) so every theme re-skins the plugin.
 *
 * Layout per approved mockup variant C + design review:
 *   D3  composed first viewport: top suggestion (savings = largest number)
 *       left, token waste map right with the targeted category highlighted
 *   D4  window pills BELOW suggestions — they scope analytics only
 *   D5  per-region loading/empty/error states; recorder-not-detected banner
 *   D8  first-run state with one-click 30-day backfill + progress
 *   D9  split gate copy (deterministic findings at 3, AI at 10)
 *   D10/D24 acted-on strip: predicted vs observed ("change since acted")
 *   D11/D15 sessions:top insight strip with setup-state lifetime + dismiss
 *   D13 760px collapse, keyboard nav, chart aria summaries
 *   D14 fixed palette slot per frozen category; charts top-level only,
 *       click-to-expand children; stacks top-5 + other; category-stacked
 *       time series only (never by model — board feedback D20)
 */
(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  const { React, fetchJSON } = SDK;
  const h = React.createElement;
  const { useState, useEffect, useCallback, useRef } = SDK.hooks;
  const Button = SDK.components.Button || function (p) { return h("button", p, p.children); };

  const API = "/api/plugins/token-lens";

  // -- strings (D12 copy table; single module, utility tone) -----------------
  const S = {
    tabTitle: "Token Lens",
    wasteMapHeading: "Where tokens go",
    calibBadge: "calibrated to billed totals",
    estBadge: "~estimated",
    exactBadge: "exact",
    emptyWindow: "No recorded calls in this window yet",
    firstRunTitle: "No data recorded yet.",
    firstRunBody: "Run a Hermes session — or import your last 30 days.",
    firstRunButton: "Import 30 days of history",
    recorderBanner: "Token Lens isn't recording — restart your gateway/CLI session (hooks load at process start).",
    breakerBanner: "Recording paused after repeated errors — see /health.",
    gatePreData: (n, min) => `Findings appear after ${min} recorded sessions — ${n}/${min} so far.`,
    gateSplit: (n, min) => `Deterministic findings below · AI-evaluated suggestions unlock at ${min} sessions (${n}/${min})`,
    refreshQueued: "refresh queued — runs at next session activity",
    copied: "Copied ✓",
    copyPlan: "Copy plan",
    dismiss: "Dismiss",
    markDone: "Mark done",
    actedHeading: "Acted on",
    actedObservedLabel: "change since acted",
    measuring: (n, need) => `measuring… (${n} of ${need} sessions)`,
    noBaseline: "no pre-action baseline",
    byModelHeading: "By model",
    dailyHeading: "Daily tokens by category · 7 days",
    perSessionHeading: "Tokens by session · 24 hrs",
    hatchedLegend: "hatched = estimated",
    footer: (o, a, r) => `Token Lens overhead: ${o} tokens this week · analyzer v${a} · rules v${r}`,
    errorRetry: "Couldn't load · Retry",
    suggUnavailable: "Couldn't load suggestions",
    setupCard: "Token Lens active — recording your first session…",
    unlockCard: (n, min) => `Suggestions unlock in ${Math.max(0, min - n)} sessions`,
    foundCard: (pct) => `Token Lens found ${pct}% avoidable weekly token waste`,
    open: "Open",
  };

  // -- number formatting (D12): tokens 3 sig figs k/M, counts locale, pct int
  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return (n / 1e6).toPrecision(3) + "M";
    if (n >= 1e3) return (n / 1e3).toPrecision(3) + "k";
    return String(Math.round(n));
  }
  const fmtInt = (n) => (Number(n) || 0).toLocaleString();
  const fmtPct = (n) => Math.round(Number(n) || 0) + "%";

  // -- D14 fixed palette slots (assignment pinned; unattributed always gray)
  const CAT_COLORS = {
    "history.user": "hsl(215 60% 58%)",
    "history.assistant": "hsl(195 55% 55%)",
    "tool_results": "hsl(130 45% 50%)",
    "tool_schemas.mcp": "hsl(270 50% 62%)",
    "tool_schemas.builtin": "hsl(320 45% 60%)",
    "system_prompt": "hsl(5 60% 58%)",
    "skill_loading": "hsl(45 70% 52%)",
    "memory": "hsl(170 50% 45%)",
    "output": "hsl(95 40% 52%)",
    "reasoning": "hsl(240 35% 60%)",
    "unattributed": "hsl(220 8% 55%)",
    "other": "hsl(220 8% 45%)",
  };
  const CAT_LABELS = {
    "history.user": "History (user)", "history.assistant": "History (assistant)",
    "tool_results": "Tool results", "tool_schemas.mcp": "Tool schemas (MCP)",
    "tool_schemas.builtin": "Tool schemas (built-in)", "system_prompt": "System prompt",
    "skill_loading": "Skills", "memory": "Memory", "output": "Output",
    "reasoning": "Reasoning", "unattributed": "Unattributed", "other": "Other",
  };
  const catColor = (k) => CAT_COLORS[k] || CAT_COLORS.other;
  const catLabel = (k) => CAT_LABELS[k] || k;

  // -- styles (D12 token mapping; injected once) ------------------------------
  const CSS = `
  .tl-root{--tl-surface:var(--color-card);--tl-border:var(--color-border);
    --tl-text:var(--color-foreground);--tl-muted:var(--color-muted-foreground);
    --tl-accent:var(--color-primary);--tl-warn:var(--color-warning,#b8860b);
    --tl-danger:var(--color-destructive);--tl-mono:var(--font-mono,ui-monospace,monospace);
    color:var(--tl-text);font-size:14px;max-width:1080px;margin:0 auto;padding:4px 8px 40px;}
  .tl-root *:focus-visible{outline:2px solid var(--tl-accent);outline-offset:2px;}
  .tl-num{font-family:var(--tl-mono);}
  .tl-muted{color:var(--tl-muted);}
  .tl-section{border-top:1px solid var(--tl-border);margin-top:20px;padding-top:14px;}
  .tl-h{font-size:13px;font-weight:600;margin:0 0 10px;}
  .tl-badge{font-family:var(--tl-mono);font-size:11px;padding:1px 8px;border-radius:999px;
    border:1px solid var(--tl-border);display:inline-block;}
  .tl-badge.exact{color:var(--tl-accent);border-color:var(--tl-accent);}
  .tl-badge.est{color:var(--tl-warn);border-style:dashed;}
  .tl-banner{border:1px solid var(--tl-warn);border-radius:var(--radius,8px);
    padding:10px 14px;margin:10px 0;font-size:13px;}
  .tl-work{display:grid;grid-template-columns:11fr 9fr;gap:28px;margin-top:8px;}
  .tl-savings{font-family:var(--tl-mono);font-size:34px;font-weight:700;color:var(--tl-accent);line-height:1.1;}
  .tl-sugg-title{font-size:16px;font-weight:600;margin:2px 0 6px;}
  .tl-plan{font-family:var(--tl-mono);font-size:12px;line-height:1.7;color:var(--tl-muted);
    background:var(--tl-surface);border:1px solid var(--tl-border);border-radius:var(--radius,8px);
    padding:8px 12px;margin-top:10px;white-space:pre-wrap;}
  .tl-actions{display:flex;gap:8px;margin-top:10px;align-items:center;flex-wrap:wrap;}
  .tl-btn{font-size:13px;padding:6px 14px;border-radius:6px;border:1px solid var(--tl-border);
    background:transparent;color:var(--tl-text);cursor:pointer;min-height:32px;}
  .tl-btn.primary{background:var(--tl-accent);color:var(--tl-surface);border-color:var(--tl-accent);font-weight:600;}
  .tl-btn.quiet{border:none;color:var(--tl-muted);text-decoration:underline;padding:6px 6px;}
  .tl-rows{margin-top:14px;display:flex;flex-direction:column;gap:6px;}
  .tl-row{display:flex;justify-content:space-between;align-items:center;gap:10px;
    border:1px solid var(--tl-border);border-radius:var(--radius,8px);
    background:var(--tl-surface);padding:9px 14px;cursor:pointer;text-align:left;width:100%;}
  .tl-row:hover{border-color:var(--tl-accent);}
  .tl-pills{display:flex;gap:8px;margin:18px 0 2px;}
  .tl-pill{padding:5px 14px;border:1px solid var(--tl-border);border-radius:999px;
    font-size:12px;color:var(--tl-muted);background:transparent;cursor:pointer;min-height:30px;}
  .tl-pill.on{color:var(--tl-text);border-color:var(--tl-accent);}
  .tl-kpis{display:flex;gap:0;margin:12px 0 4px;flex-wrap:wrap;}
  .tl-kpi{padding:4px 22px 4px 0;margin-right:22px;border-right:1px solid var(--tl-border);}
  .tl-kpi:last-child{border-right:none;}
  .tl-kpi .v{font-family:var(--tl-mono);font-size:20px;font-weight:600;}
  .tl-kpi .l{font-size:11px;color:var(--tl-muted);margin-top:1px;}
  .tl-wm-bar{display:flex;height:26px;border-radius:4px;overflow:hidden;margin:6px 0 10px;}
  .tl-wm-seg{height:100%;min-width:1px;}
  .tl-wm-seg.hl{box-shadow:inset 0 0 0 2px var(--tl-accent);}
  .tl-legend{display:flex;flex-direction:column;gap:3px;font-size:12px;}
  .tl-legend-row{display:flex;justify-content:space-between;gap:10px;background:none;
    border:none;color:var(--tl-text);cursor:pointer;padding:2px 0;text-align:left;width:100%;font-size:12px;}
  .tl-legend-row .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:7px;}
  .tl-legend-row.hl{font-weight:600;}
  .tl-children{margin:2px 0 6px 18px;font-size:12px;color:var(--tl-muted);}
  .tl-grid2{display:grid;grid-template-columns:1fr 1fr;gap:28px;}
  .tl-bars{display:flex;align-items:flex-end;gap:10px;height:120px;margin-top:10px;}
  .tl-bar{flex:1;display:flex;flex-direction:column-reverse;border-radius:3px 3px 0 0;
    overflow:hidden;border:none;background:none;cursor:pointer;padding:0;min-width:8px;}
  .tl-bar:disabled{cursor:default;}
  .tl-bar.est .tl-seg{background-image:repeating-linear-gradient(45deg,transparent 0 4px,
    color-mix(in srgb, currentColor 15%, transparent) 4px 8px);}
  .tl-xlab{display:flex;gap:10px;font-size:10px;color:var(--tl-muted);margin-top:4px;}
  .tl-xlab span{flex:1;text-align:center;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
  table.tl-table{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px;}
  table.tl-table th{text-align:left;color:var(--tl-muted);font-weight:500;
    border-bottom:1px solid var(--tl-border);padding:5px 8px;}
  table.tl-table td{padding:5px 8px;border-bottom:1px solid var(--tl-border);font-family:var(--tl-mono);}
  .tl-skel{background:var(--tl-border);border-radius:4px;opacity:.4;animation:tlp 1.2s infinite;}
  @keyframes tlp{0%,100%{opacity:.35}50%{opacity:.15}}
  .tl-footer{margin-top:26px;border-top:1px solid var(--tl-border);padding-top:10px;
    font-family:var(--tl-mono);font-size:11px;color:var(--tl-muted);}
  .tl-firstrun{text-align:center;padding:60px 20px;}
  .tl-acted{margin-top:12px;font-size:12px;}
  .tl-acted-row{display:flex;gap:14px;padding:5px 0;border-top:1px dashed var(--tl-border);align-items:baseline;}
  .tl-drill{border:1px solid var(--tl-border);border-radius:var(--radius,8px);
    background:var(--tl-surface);padding:12px 16px;margin-top:10px;}
  .tl-entry{display:flex;justify-content:space-between;align-items:center;gap:14px;
    border:1px solid var(--tl-border);border-radius:var(--radius,8px);
    background:var(--tl-surface);padding:10px 16px;margin-bottom:12px;}
  .tl-spark{display:flex;align-items:flex-end;gap:2px;height:22px;}
  @media (max-width:760px){
    .tl-work{grid-template-columns:1fr;gap:14px;}
    .tl-grid2{grid-template-columns:1fr;}
    .tl-kpi{padding-right:14px;margin-right:14px;}
    .tl-table-wrap{overflow-x:auto;}
  }`;

  function injectCss() {
    if (document.getElementById("token-lens-css")) return;
    const el = document.createElement("style");
    el.id = "token-lens-css";
    el.textContent = CSS;
    document.head.appendChild(el);
  }

  // -- data hook: independent per-region fetch (D5: no full-page spinner) -----
  function useEndpoint(path, deps) {
    const [state, setState] = useState({ loading: true, data: null, error: null });
    const reload = useCallback(() => {
      setState((s) => ({ ...s, loading: true, error: null }));
      fetchJSON(API + path)
        .then((data) => setState({ loading: false, data, error: null }))
        .catch((error) => setState({ loading: false, data: null, error }));
    }, deps || []);
    useEffect(() => { reload(); }, [reload]);
    return [state, reload];
  }

  function Skeleton({ w, hgt }) {
    return h("div", { className: "tl-skel", style: { width: w || "100%", height: hgt || 16 } });
  }

  function ErrorRetry({ onRetry }) {
    return h("button", { className: "tl-btn quiet", onClick: onRetry }, S.errorRetry);
  }

  // -- waste map (the D3 visual anchor) ---------------------------------------
  function WasteMap({ categories, total, highlight, childrenMap }) {
    const [expanded, setExpanded] = useState(null);
    const entries = Object.entries(categories || {})
      .filter(([, v]) => v > 0).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      return h("div", { className: "tl-muted", style: { padding: "20px 0" } }, S.emptyWindow);
    }
    const summary = entries.map(([k, v]) =>
      `${catLabel(k)} ${fmtTokens(v)} tokens ${fmtPct(v / total * 100)}`).join(", ");
    return h("div", null,
      h("div", { className: "tl-wm-bar", role: "img", "aria-label": S.wasteMapHeading + ": " + summary },
        entries.map(([k, v]) => h("div", {
          key: k,
          className: "tl-wm-seg" + (k === highlight ? " hl" : ""),
          style: { width: (v / total * 100) + "%", background: catColor(k) },
          title: catLabel(k) + " · " + fmtTokens(v),
        }))),
      h("div", { className: "tl-legend" }, entries.map(([k, v]) => {
        const kids = childrenMap && childrenMap[k];
        return h("div", { key: k },
          h("button", {
            className: "tl-legend-row" + (k === highlight ? " hl" : ""),
            onClick: () => kids && setExpanded(expanded === k ? null : k),
            "aria-expanded": kids ? expanded === k : undefined,
          },
            h("span", null,
              h("span", { className: "sw", style: { background: catColor(k) }, "aria-hidden": "true" }),
              catLabel(k), kids ? " ▸" : ""),
            h("span", { className: "tl-num tl-muted" },
              fmtTokens(v) + " · " + fmtPct(v / total * 100))),
          kids && expanded === k && h("div", { className: "tl-children" },
            Object.entries(kids).sort((a, b) => b[1] - a[1]).map(([ck, cv]) =>
              h("div", { key: ck }, ck.split(".").pop() + " — " + fmtTokens(cv)))));
      })));
  }

  // -- suggestion cards ---------------------------------------------------------
  function PlanActions({ sugg, onAction }) {
    const [copied, setCopied] = useState(false);
    const copy = () => {
      navigator.clipboard.writeText(sugg.plan_md).then(() => {
        setCopied(true);
        setTimeout(() => setCopied(false), 2000);
      });
    };
    const post = (verb) => fetchJSON(API + `/suggestions/${sugg.id}/${verb}`, { method: "POST" })
      .then(onAction).catch(() => {});
    return h("div", { className: "tl-actions" },
      h("button", { className: "tl-btn primary", onClick: copy }, copied ? S.copied : S.copyPlan),
      h("button", { className: "tl-btn", onClick: () => post("done") }, S.markDone),
      h("button", { className: "tl-btn quiet", onClick: () => post("dismiss") }, S.dismiss));
  }

  function riskBadge(risk) {
    const color = risk === "low" ? "var(--tl-accent)"
      : risk === "medium" ? "var(--tl-warn)" : "var(--tl-danger)";
    return h("span", { className: "tl-badge", style: { color } }, risk + " risk");
  }

  function TopSuggestion({ sugg, onAction }) {
    const evidence = (sugg.evidence || "").split("\n");
    return h("div", null,
      h("div", { className: "tl-savings" }, "−" + sugg.est_savings_pct + "%/week"),
      h("div", { className: "tl-sugg-title" }, sugg.title, " ", riskBadge(sugg.risk)),
      h("div", { className: "tl-muted", style: { fontSize: 13 } }, evidence[0]),
      evidence.slice(1).map((line, i) =>
        h("div", { key: i, className: "tl-muted", style: { fontSize: 12, marginTop: 2 } }, line)),
      h("div", { className: "tl-plan" }, sugg.plan_md),
      h(PlanActions, { sugg, onAction }));
  }

  function CollapsedRow({ sugg, expanded, onToggle, onAction }) {
    return h("div", null,
      h("button", {
        className: "tl-row", onClick: onToggle, "aria-expanded": expanded,
      },
        h("span", null, sugg.title),
        h("span", { style: { whiteSpace: "nowrap" } },
          h("span", { className: "tl-badge", style: { color: "var(--tl-accent)", marginRight: 6 } },
            "−" + sugg.est_savings_pct + "%/week"),
          riskBadge(sugg.risk))),
      expanded && h("div", { style: { padding: "4px 14px 10px" } },
        h("div", { className: "tl-muted", style: { fontSize: 12 } }, sugg.evidence),
        h("div", { className: "tl-plan" }, sugg.plan_md),
        h(PlanActions, { sugg, onAction })));
  }

  function ActedOnStrip({ acted }) {
    if (!acted || !acted.length) return null;
    return h("div", { className: "tl-acted" },
      h("div", { className: "tl-h", style: { marginBottom: 4 } }, S.actedHeading),
      acted.map((a) => {
        const o = a.observed || {};
        let observed;
        if (o.state === "measured") {
          observed = `${o.pct > 0 ? "+" : ""}${Math.round(o.pct)}% (${fmtTokens(Math.abs(o.abs_per_session))} tok/session) — ${S.actedObservedLabel}`;
        } else if (o.state === "no_baseline") {
          observed = S.noBaseline;
        } else {
          observed = S.measuring(o.post_sessions || 0, o.needed || 5);
        }
        return h("div", { key: a.id, className: "tl-acted-row" },
          h("span", null, a.title),
          h("span", { className: "tl-num tl-muted" }, "predicted −" + a.est_savings_pct + "%"),
          h("span", { className: "tl-num" }, observed));
      }));
  }

  // -- timeseries ----------------------------------------------------------------
  function StackedBars({ data, heading, onBarClick }) {
    if (!data) return null;
    const bars = data.bars || [];
    const max = Math.max(1, ...bars.map((b) =>
      Object.values(b.segments).reduce((a, v) => a + v, 0)));
    const summary = bars.map((b) => {
      const t = Object.values(b.segments).reduce((a, v) => a + v, 0);
      return `${b.label}: ${fmtTokens(t)}${b.estimated ? " (estimated)" : ""}`;
    }).join("; ");
    return h("div", null,
      h("div", { className: "tl-h" }, heading, " ",
        h("span", { className: "tl-badge est" }, S.hatchedLegend)),
      h("div", { className: "tl-bars", role: "img", "aria-label": heading + ". " + summary },
        bars.map((b, i) => {
          const total = Object.values(b.segments).reduce((a, v) => a + v, 0);
          return h("button", {
            key: i,
            className: "tl-bar" + (b.estimated ? " est" : ""),
            style: { height: Math.max(2, total / max * 100) + "%" },
            onClick: onBarClick ? () => onBarClick(b) : undefined,
            disabled: !onBarClick,
            "aria-label": b.label + ": " + fmtTokens(total) + " tokens" +
              (b.estimated ? " (estimated)" : ""),
          }, (data.stack_categories || []).map((cat) => {
            const v = b.segments[cat] || 0;
            return v > 0 ? h("div", {
              key: cat, className: "tl-seg",
              style: { height: (v / (total || 1) * 100) + "%", background: catColor(cat), color: catColor(cat) },
            }) : null;
          }));
        })),
      h("div", { className: "tl-xlab" },
        bars.map((b, i) => h("span", { key: i }, String(b.label).slice(-8)))));
  }

  function Drilldown({ sessionId, onClose }) {
    const [st] = useEndpoint("/sessions/" + encodeURIComponent(sessionId), [sessionId]);
    useEffect(() => {
      const esc = (e) => { if (e.key === "Escape") onClose(); };
      document.addEventListener("keydown", esc);
      return () => document.removeEventListener("keydown", esc);
    }, [onClose]);
    if (st.loading) return h("div", { className: "tl-drill" }, h(Skeleton, { hgt: 40 }));
    if (st.error) return h("div", { className: "tl-drill tl-muted" }, "No per-call data for this session");
    const d = st.data;
    const total = (d.totals && d.totals.billed) || 1;
    return h("div", { className: "tl-drill" },
      h("div", { style: { display: "flex", justifyContent: "space-between" } },
        h("span", { className: "tl-num" }, sessionId.slice(0, 12), " · ",
          fmtTokens(total), " tokens · ", fmtInt(d.api_calls), " calls · ",
          h("span", { className: "tl-badge " + (d.precision === "exact" ? "exact" : "est") }, d.precision)),
        h("button", { className: "tl-btn quiet", onClick: onClose, "aria-label": "Close" }, "✕")),
      h(WasteMap, { categories: d.categories, total, childrenMap: d.children }));
  }

  // -- first run (D8) ---------------------------------------------------------------
  function FirstRun({ onDataAppeared }) {
    const [job, setJob] = useState(null);
    const timer = useRef(null);
    const start = () => {
      fetchJSON(API + "/backfill", { method: "POST" }).then((r) => {
        setJob(r.job);
        timer.current = setInterval(() => {
          fetchJSON(API + "/backfill/status").then((s) => {
            setJob(s.job);
            if (s.job.state === "done" || s.job.state === "failed") {
              clearInterval(timer.current);
              if (s.job.state === "done") onDataAppeared();
            }
          }).catch(() => {});
        }, 800);
      }).catch(() => {});
    };
    useEffect(() => () => timer.current && clearInterval(timer.current), []);
    return h("div", { className: "tl-firstrun" },
      h("div", { style: { fontSize: 18, fontWeight: 600 } }, S.firstRunTitle),
      h("div", { className: "tl-muted", style: { margin: "6px 0 18px" } }, S.firstRunBody),
      job == null
        ? h("button", { className: "tl-btn primary", onClick: start }, S.firstRunButton)
        : job.state === "running"
          ? h("div", { className: "tl-num tl-muted" },
              "Analyzing session " + (job.done || 0) + (job.total ? " of " + job.total : "") + "…")
          : job.state === "failed"
            ? h("div", { className: "tl-muted" }, "Import failed: " + (job.error || "unknown"))
            : h("div", { className: "tl-muted" }, "Imported " + (job.sessions || 0) + " sessions ✓"));
  }

  // -- main tab -----------------------------------------------------------------------
  function TokenLensPage() {
    injectCss();
    const [window_, setWindow] = useState("7d");
    const [drill, setDrill] = useState(null);
    const [summarySt, reloadSummary] = useEndpoint("/summary?window=" + window_, [window_]);
    const [suggSt, reloadSugg] = useEndpoint("/suggestions", []);
    const [catSt, reloadCats] = useEndpoint("/categories?window=" + window_, [window_]);
    const [tsSt, reloadTs] = useEndpoint(
      "/timeseries?window=" + (window_ === "session" ? "24h" : window_), [window_]);
    const [modelsSt, reloadModels] = useEndpoint("/by-model?window=" + window_, [window_]);
    const [healthSt] = useEndpoint("/health", []);
    const [metaSt] = useEndpoint("/meta", []);
    const [expandedRow, setExpandedRow] = useState(null);
    const [refreshMsg, setRefreshMsg] = useState(null);

    const reloadAll = () => { reloadSummary(); reloadSugg(); reloadCats(); reloadTs(); reloadModels(); };

    // DB-newer-than-code: every endpoint 409s — show the mismatch card.
    const dbErr = summarySt.error && String(summarySt.error).indexOf("409") !== -1;
    if (dbErr) {
      return h("div", { className: "tl-root" },
        h("div", { className: "tl-banner", role: "alert" },
          "Token Lens database was written by a NEWER plugin version. ",
          "Upgrade the plugin (git pull + restart) or remove ~/.hermes/token_lens.db."));
    }

    const health = healthSt.data || {};
    const summary = summarySt.data;
    const firstRun = summary && !summary.has_any_data;

    const sugg = suggSt.data;
    const gates = (sugg && sugg.gates) || {};
    const top = sugg && sugg.suggestions && sugg.suggestions[0];
    const rest = (sugg && sugg.suggestions || []).slice(1);
    const highlightCat = top ? topLevelOf(top.category) : null;

    const manualRefresh = () => {
      fetchJSON(API + "/suggestions/refresh", { method: "POST" })
        .then((r) => setRefreshMsg(r.message || S.refreshQueued))
        .catch((e) => setRefreshMsg(String(e.message || e).replace(/^\d+:\s*/, "")));
    };

    return h("div", { className: "tl-root" },
      h("h2", { style: { fontSize: 17, margin: "10px 0 2px" } }, S.tabTitle),

      // banners (D5): recorder-not-detected wins; breaker second
      health.recorder_warning && h("div", { className: "tl-banner", role: "alert" }, S.recorderBanner),
      !health.recorder_warning && health.breaker && health.breaker.tripped &&
        h("div", { className: "tl-banner", role: "alert" }, S.breakerBanner),

      firstRun
        ? h(FirstRun, { onDataAppeared: reloadAll })
        : [
          // ── 1st viewport: composed work surface (D3) ──────────────────
          h("div", { key: "work" },
            suggSt.loading
              ? h("div", { className: "tl-work" }, h(Skeleton, { hgt: 120 }), h(Skeleton, { hgt: 120 }))
              : suggSt.error
                ? h("div", { className: "tl-muted" }, S.suggUnavailable, " ", h(ErrorRetry, { onRetry: reloadSugg }))
                : top
                  ? h("div", { className: "tl-work" },
                      h(TopSuggestion, { sugg: top, onAction: reloadSugg }),
                      h("div", null,
                        h("div", { className: "tl-h" }, S.wasteMapHeading, " ",
                          h("span", { className: "tl-badge exact" }, S.calibBadge)),
                        catSt.loading ? h(Skeleton, { hgt: 90 })
                          : catSt.error ? h(ErrorRetry, { onRetry: reloadCats })
                          : h(WasteMap, {
                              categories: catSt.data.categories,
                              total: catSt.data.total || 1,
                              highlight: highlightCat,
                              childrenMap: catSt.data.children,
                            })))
                  : h("div", null,
                      // gated states (D9 split copy)
                      h("div", { className: "tl-muted", style: { margin: "8px 0" } },
                        gates.detector && !gates.detector.open
                          ? S.gatePreData(gates.observed_sessions || 0, gates.detector.min_sessions || 3)
                          : S.gateSplit(gates.observed_sessions || 0, (gates.llm || {}).min_sessions || 10)),
                      h("div", { className: "tl-h" }, S.wasteMapHeading, " ",
                        h("span", { className: "tl-badge exact" }, S.calibBadge)),
                      catSt.data && h(WasteMap, {
                        categories: catSt.data.categories,
                        total: catSt.data.total || 1,
                        childrenMap: catSt.data.children,
                      })),
            // collapsed siblings + gate header when a top suggestion exists
            top && gates.llm && !gates.llm.open &&
              h("div", { className: "tl-muted", style: { fontSize: 12, marginTop: 10 } },
                S.gateSplit(gates.observed_sessions || 0, gates.llm.min_sessions || 10)),
            rest.length > 0 && h("div", { className: "tl-rows" },
              rest.map((sg) => h(CollapsedRow, {
                key: sg.id, sugg: sg,
                expanded: expandedRow === sg.id,
                onToggle: () => setExpandedRow(expandedRow === sg.id ? null : sg.id),
                onAction: reloadSugg,
              }))),
            sugg && h(ActedOnStrip, { acted: sugg.acted_on }),
            // refresh status line (D5)
            sugg && h("div", { className: "tl-muted", style: { fontSize: 11, marginTop: 8 } },
              h("button", { className: "tl-btn quiet", onClick: manualRefresh, style: { fontSize: 11 } },
                "Refresh suggestions"),
              refreshMsg ? " · " + refreshMsg
                : sugg.refresh && sugg.refresh.status === "running" ? " · refresh running…"
                : sugg.refresh && sugg.refresh.status === "skipped" && sugg.refresh.reason
                  ? " · last refresh skipped: " + sugg.refresh.reason : "",
              sugg.hidden_count > 0 ? ` · ${sugg.hidden_count} below quality bar` : "")),

          // ── pills BELOW suggestions (D4) ───────────────────────────────
          h("div", { key: "pills", className: "tl-pills", role: "tablist", "aria-label": "Analytics window" },
            ["session", "24h", "7d"].map((w) =>
              h("button", {
                key: w, role: "tab", "aria-selected": window_ === w,
                className: "tl-pill" + (window_ === w ? " on" : ""),
                onClick: () => { setWindow(w); setDrill(null); },
              }, w === "session" ? "Last session" : w === "24h" ? "24 hrs" : "7 days"))),

          // ── inline KPI strip (D3: no card chrome) ──────────────────────
          h("div", { key: "kpis", className: "tl-kpis" },
            summarySt.loading ? h(Skeleton, { w: 420, hgt: 40 })
              : summarySt.error ? h(ErrorRetry, { onRetry: reloadSummary })
              : [
                kpi("Total tokens", fmtTokens(summary.total_tokens),
                  h("span", {
                    className: "tl-badge " + (summary.precision === "exact" ? "exact" : "est"),
                    title: summary.precision === "exact"
                      ? "matches Analytics → Usage"
                      : summary.estimated_share_pct + "% of tokens in this window are estimated (backfill/no-usage)",
                  }, summary.precision === "exact" ? S.exactBadge : S.estBadge)),
                kpi("API calls", fmtInt(summary.api_calls)),
                kpi("Cache hit rate", fmtPct(summary.cache_hit_rate * 100)),
                kpi("Sessions", fmtInt(summary.sessions)),
              ]),

          // ── time series + by-model ────────────────────────────────────
          h("div", { key: "ts", className: "tl-section" },
            tsSt.loading ? h(Skeleton, { hgt: 120 })
              : tsSt.error ? h(ErrorRetry, { onRetry: reloadTs })
              : h(StackedBars, {
                  data: tsSt.data,
                  heading: tsSt.data.window === "24h" ? S.perSessionHeading : S.dailyHeading,
                  onBarClick: tsSt.data.window === "24h"
                    ? (b) => setDrill(drill === b.label ? null : b.label) : null,
                }),
            drill && h(Drilldown, { sessionId: drill, onClose: () => setDrill(null) })),

          h("div", { key: "models", className: "tl-section tl-table-wrap" },
            h("div", { className: "tl-h" }, S.byModelHeading),
            modelsSt.loading ? h(Skeleton, { hgt: 60 })
              : modelsSt.error ? h(ErrorRetry, { onRetry: reloadModels })
              : (modelsSt.data.models || []).length === 0
                ? h("div", { className: "tl-muted", style: { fontSize: 12 } }, S.emptyWindow)
                : h("table", { className: "tl-table" },
                    h("thead", null, h("tr", null,
                      ["Model", "Input", "Output", "Calls"].map((c) => h("th", { key: c }, c)))),
                    h("tbody", null, modelsSt.data.models.map((m) =>
                      h("tr", { key: m.model },
                        h("td", null, m.model),
                        h("td", null, fmtTokens(m.input)),
                        h("td", null, fmtTokens(m.output)),
                        h("td", null, fmtInt(m.calls))))))),

          // ── quiet footer ───────────────────────────────────────────────
          h("div", { key: "footer", className: "tl-footer" },
            metaSt.data
              ? S.footer(fmtTokens(metaSt.data.overhead_tokens_week),
                  metaSt.data.analyzer_version, metaSt.data.rules_version)
              : ""),
        ]);
  }

  function kpi(label, value, badge) {
    return h("div", { key: label, className: "tl-kpi" },
      h("div", { className: "v" }, value, badge ? " " : null, badge || null),
      h("div", { className: "l" }, label));
  }

  function topLevelOf(cat) {
    if (!cat) return null;
    if (cat.indexOf("tool_schemas.mcp") === 0) return "tool_schemas.mcp";
    if (cat.indexOf("tool_schemas") === 0) return "tool_schemas.builtin";
    if (cat.indexOf("tool_results") === 0) return "tool_results";
    return cat;
  }

  // -- sessions:top entry card (insight strip, D11/D15) -------------------------
  const ENTRY_DISMISS_KEY = "token-lens-setup-dismissed";

  function EntryCard() {
    injectCss();
    const [summarySt] = useEndpoint("/summary?window=7d", []);
    const [suggSt] = useEndpoint("/suggestions", []);
    const [dismissed, setDismissed] = useState(
      () => localStorage.getItem(ENTRY_DISMISS_KEY) === "1");

    // D5: render NOTHING while loading or on error — no skeleton on someone
    // else's page, no layout shift.
    if (summarySt.loading || summarySt.error) return null;
    const summary = summarySt.data;
    const sugg = suggSt.data;

    const open = () => { window.location.href = "/token-lens"; };

    if (!summary.has_any_data) {
      // setup state persists until first data; dismissible (D15)
      if (dismissed) return null;
      return h("div", { className: "tl-entry tl-root", style: { padding: "8px 16px" } },
        h("span", { className: "tl-muted", style: { fontSize: 13 } }, S.setupCard),
        h("span", null,
          h("button", { className: "tl-btn quiet", onClick: open }, S.open),
          h("button", {
            className: "tl-btn quiet", "aria-label": "Dismiss",
            onClick: () => { localStorage.setItem(ENTRY_DISMISS_KEY, "1"); setDismissed(true); },
          }, "✕")));
    }

    const shown = (sugg && sugg.suggestions) || [];
    const totalPct = Math.min(99, Math.round(
      shown.reduce((a, s) => a + (s.est_savings_pct || 0), 0)));
    const gates = (sugg && sugg.gates) || {};
    let headline;
    if (shown.length && totalPct > 0) {
      headline = S.foundCard(totalPct);
    } else if (gates.detector && !gates.detector.open) {
      headline = S.unlockCard(gates.observed_sessions || 0, gates.detector.min_sessions || 3);
    } else {
      headline = fmtTokens(summary.total_tokens) + " tokens this week";
    }

    return h("div", { className: "tl-entry tl-root", style: { padding: "8px 16px" } },
      h("span", null,
        h("strong", { style: { fontSize: 13 } }, headline),
        shown[0] && h("div", { className: "tl-muted", style: { fontSize: 12 } },
          "Top: " + shown[0].title + " (−" + shown[0].est_savings_pct + "%/week)")),
      h("span", { style: { display: "flex", alignItems: "center", gap: 10 } },
        h(Sparkline, { summary }),
        h("button", { className: "tl-btn primary", onClick: open }, S.open)));
  }

  function Sparkline({ summary }) {
    const [tsSt] = useEndpoint("/timeseries?window=7d", []);
    if (tsSt.loading || tsSt.error) return null;
    const bars = tsSt.data.bars || [];
    const max = Math.max(1, ...bars.map((b) =>
      Object.values(b.segments).reduce((a, v) => a + v, 0)));
    return h("span", { className: "tl-spark", "aria-hidden": "true" },
      bars.map((b, i) => {
        const t = Object.values(b.segments).reduce((a, v) => a + v, 0);
        return h("span", {
          key: i,
          style: {
            width: 4, background: "var(--tl-accent)", opacity: 0.7,
            height: Math.max(2, t / max * 22),
          },
        });
      }));
  }

  // -- registration ----------------------------------------------------------------
  if (window.__HERMES_PLUGINS__ && typeof window.__HERMES_PLUGINS__.register === "function") {
    window.__HERMES_PLUGINS__.register("token-lens", TokenLensPage);
    if (typeof window.__HERMES_PLUGINS__.registerSlot === "function") {
      window.__HERMES_PLUGINS__.registerSlot("sessions:top", "token-lens", EntryCard);
    }
  }
})();
