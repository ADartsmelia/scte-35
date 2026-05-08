/**
 * SCTE-35 Stream Overlay — Dashboard
 *
 * Designed for broadcast operators who don't need to know what "SCTE-35" means.
 * Plain language throughout. Technical details are under "Advanced Settings".
 */

import { useEffect, useRef, useState } from "react";

const API = import.meta.env.VITE_API ?? "http://localhost:8000";
// In production nginx deployment VITE_API is empty — derive WebSocket base from
// the browser's own host so the WS URL works on any server without a rebuild.
const WS_BASE = API
  ? API.replace(/^http/, "ws")
  : `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}`;

// ─── Preset stream profiles ────────────────────────────────────────────────
const PRESETS = {
  "Live SRT (caller)": {
    input_url:     "srt://5.178.129.17:12349?mode=caller&latency=200",
    output_url:    "srt://0.0.0.0:9003?mode=listener&latency=200",
    output_format: "mpegts",
  },
  "Local UDP test": {
    input_url:     "udp://239.0.0.1:1234",
    output_url:    "udp://127.0.0.1:5004",
    output_format: "mpegts",
  },
  "RTMP relay": {
    input_url:     "rtmp://server/live/stream",
    output_url:    "rtmp://server/live/output",
    output_format: "flv",
  },
};

// Human-readable names for SCTE-35 segmentation type IDs
const SEG_NAMES = {
  0x10: "Program Start",        0x11: "Program End",
  0x12: "Early Termination",    0x13: "Program Breakaway",
  0x14: "Program Resumed",
  0x20: "Chapter Start",        0x21: "Chapter End",
  0x22: "Ad Break Started",     0x23: "Ad Break Ended",
  0x30: "Provider Ad Start",    0x31: "Provider Ad End",
  0x32: "Distributor Ad Start", 0x33: "Distributor Ad End",
  0x34: "Placement Opp Start",  0x35: "Placement Opp End",
  0x36: "Dist. Placement Start",0x37: "Dist. Placement End",
  0x40: "Unscheduled Event",    0x41: "Unscheduled Event End",
  0x50: "Network Start",        0x51: "Network End",
};

// Event types that are "end" events (hide overlay rather than show it)
const IS_END_TYPE = new Set([
  0x11,0x12,0x13,0x21,0x23,0x31,0x33,0x35,0x37,0x41,0x51,
]);

const DEFAULTS = {
  preset:        "Live SRT (caller)",
  input_url:     PRESETS["Live SRT (caller)"].input_url,
  output_url:    PRESETS["Live SRT (caller)"].output_url,
  output_format: "mpegts",
  overlay_path:  "/opt/scte35/data/test.mov",
  overlay_x: 50, overlay_y: 50,
  overlay_w: "", overlay_h: "",
  overlay_duration_ms: "",
  overlay_mode: "restart",
  encoder_preset: "veryfast",
  encoder_bitrate: "4M",
  triggered_segmentation_types: [0x22, 0x30, 0x32, 0x34, 0x36],
};

// ─── Formatting helpers ────────────────────────────────────────────────────
const fmtTime = (t) => {
  if (!t) return "—";
  return new Date(t * 1000).toLocaleTimeString([], { hour12: false });
};

const fmtUptime = (startedAt) => {
  if (!startedAt) return "";
  const s = Math.floor(Date.now() / 1000 - startedAt);
  if (s < 60)   return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
};

const fmtDuration = (sec) => {
  if (sec == null) return null;
  if (sec < 60) return `${Math.round(sec)}s`;
  return `${Math.floor(sec / 60)}m ${Math.floor(sec % 60)}s`;
};

const fmtLatency = (ms) => {
  if (ms == null) return null;
  if (ms < 1000) return `${ms} ms`;
  return `${(ms / 1000).toFixed(1)} s`;
};

const latencyInfo = (ms) => {
  if (ms == null) return { label: "Measuring…", color: "var(--text-3)", pct: 0 };
  if (ms < 200)   return { label: "Excellent",  color: "var(--green)",  pct: ms / 200 * 25 };
  if (ms < 500)   return { label: "Good",        color: "#4ade80",       pct: 25 + ((ms - 200) / 300) * 25 };
  if (ms < 1000)  return { label: "Fair",        color: "var(--amber)",  pct: 50 + ((ms - 500) / 500) * 25 };
  return               { label: "High",         color: "var(--red)",    pct: 75 + Math.min(((ms - 1000) / 2000) * 25, 25) };
};

// ─── Small reusable pieces ─────────────────────────────────────────────────
function Dot({ state = "neutral", pulse = false }) {
  const cls = {
    ok: "dot-green", warn: "dot-amber", bad: "dot-red", neutral: "dot-neutral",
  }[state] ?? "dot-neutral";
  return <span className={`dot ${cls} ${pulse ? "anim-blink" : ""}`} />;
}

function Toast({ tone, text, onClose }) {
  const c = tone === "ok"
    ? { bg:"rgba(34,197,94,.15)", bd:"rgba(34,197,94,.4)", fg:"#4ade80" }
    : { bg:"rgba(239,68,68,.15)", bd:"rgba(239,68,68,.4)",  fg:"#fca5a5" };
  return (
    <div className="anim-slide-down" style={{
      position:"fixed", top:20, right:20, zIndex:999,
      padding:"13px 20px", borderRadius:12, maxWidth:480,
      background: c.bg, border:`1px solid ${c.bd}`, color: c.fg,
      display:"flex", alignItems:"center", gap:10, fontSize:14,
      backdropFilter:"blur(14px)", boxShadow:"0 8px 32px rgba(0,0,0,.4)",
    }}>
      <span style={{flex:1}}>{text}</span>
      <button onClick={onClose} style={{
        background:"transparent", border:0, color:"inherit",
        cursor:"pointer", fontSize:20, lineHeight:1,
      }}>×</button>
    </div>
  );
}

function SectionHeader({ children }) {
  return (
    <div style={{
      fontSize:11, fontWeight:700, letterSpacing:".07em", textTransform:"uppercase",
      color:"var(--text-3)", marginBottom:12,
      paddingBottom:8, borderBottom:"1px solid var(--border)",
    }}>{children}</div>
  );
}

function Field({ label, hint, children, style }) {
  return (
    <div style={style}>
      <label className="field-label">{label}</label>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </div>
  );
}

function Collapse({ title, defaultOpen = false, children }) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="card" style={{overflow:"hidden"}}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width:"100%", padding:"15px 20px",
          background:"transparent", border:0, color:"inherit",
          display:"flex", alignItems:"center", justifyContent:"space-between",
          cursor:"pointer",
          borderBottom: open ? "1px solid var(--border)" : "none",
        }}
      >
        <span style={{fontWeight:600, fontSize:14}}>{title}</span>
        <svg width={16} height={16} viewBox="0 0 24 24" fill="none"
          stroke="var(--text-3)" strokeWidth={2} strokeLinecap="round"
          style={{ transform: open ? "rotate(180deg)" : "none", transition:"transform .15s" }}
        >
          <polyline points="6 9 12 15 18 9"/>
        </svg>
      </button>
      {open && <div style={{padding:20}}>{children}</div>}
    </div>
  );
}

// ─── Latency gauge ─────────────────────────────────────────────────────────
function LatencyGauge({ ms }) {
  const info = latencyInfo(ms);
  const val  = fmtLatency(ms);
  return (
    <div>
      <div style={{
        display:"flex", justifyContent:"space-between",
        alignItems:"baseline", marginBottom:8,
      }}>
        <span style={{
          fontSize:28, fontWeight:700,
          color: info.color,
          fontVariantNumeric:"tabular-nums",
        }}>
          {val ?? "—"}
        </span>
        <span style={{fontSize:12, color: info.color, fontWeight:600}}>
          {info.label}
        </span>
      </div>
      <div className="latency-bar-track">
        <div className="latency-bar-fill" style={{
          width: `${info.pct}%`,
          background: info.color,
        }} />
      </div>
      <div style={{fontSize:11, color:"var(--text-3)", marginTop:6}}>
        Under 500 ms = near real-time
      </div>
    </div>
  );
}

// ─── Stat card ─────────────────────────────────────────────────────────────
function StatCard({ icon, title, value, sub, state = "neutral", children }) {
  return (
    <div className="card" style={{padding:20, display:"flex", flexDirection:"column", gap:12}}>
      <div style={{
        display:"flex", alignItems:"center", justifyContent:"space-between",
      }}>
        <div style={{display:"flex", alignItems:"center", gap:8}}>
          <span style={{fontSize:20}}>{icon}</span>
          <span style={{
            fontSize:11.5, fontWeight:700, textTransform:"uppercase",
            letterSpacing:".05em", color:"var(--text-3)",
          }}>{title}</span>
        </div>
        <Dot state={state} pulse={state === "ok"} />
      </div>
      {children || (
        <>
          <div style={{
            fontSize:26, fontWeight:700,
            fontVariantNumeric:"tabular-nums",
            color:"var(--text-1)", lineHeight:1.1,
          }}>{value}</div>
          {sub && <div style={{fontSize:12.5, color:"var(--text-2)"}}>{sub}</div>}
        </>
      )}
    </div>
  );
}

// ─── Manual ad break panel ─────────────────────────────────────────────────
function ManualAdBreak({ running }) {
  const [duration, setDuration] = useState(15);
  const [active,   setActive]   = useState(false);
  const [toast,    setToast]     = useState(null);

  const trigger = async () => {
    try {
      const r = await fetch(`${API}/api/ad/start`, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify({ duration_ms: duration * 1000 }),
      });
      if (!r.ok) throw new Error(await r.text());
      setActive(true);
      setToast({ ok:true, text:`Overlay on for ${duration}s` });
      setTimeout(() => setActive(false), duration * 1000);
    } catch(e) {
      setToast({ ok:false, text: e.message });
    }
  };

  const stop = async () => {
    await fetch(`${API}/api/ad/stop`, { method:"POST" });
    setActive(false);
    setToast({ ok:true, text:"Overlay stopped" });
  };

  return (
    <div className="card" style={{padding:20}}>
      <SectionHeader>🎬  Manual Ad Break</SectionHeader>
      <p style={{fontSize:13, color:"var(--text-2)", marginBottom:16}}>
        Insert an ad break overlay right now for a set duration.
        No SCTE-35 signal needed.
      </p>

      {toast && (
        <div style={{
          marginBottom:12, padding:"8px 12px", borderRadius:8, fontSize:12.5,
          background: toast.ok ? "var(--green-bg)" : "var(--red-bg)",
          color: toast.ok ? "#4ade80" : "#fca5a5",
          border:`1px solid ${toast.ok ? "rgba(34,197,94,.3)" : "rgba(239,68,68,.3)"}`,
        }}>
          {toast.text}
        </div>
      )}

      <div style={{marginBottom:16}}>
        <label className="field-label">Duration</label>
        <div style={{display:"flex", alignItems:"center", gap:12}}>
          <input
            type="range" min={5} max={120} step={5}
            value={duration}
            onChange={e => setDuration(Number(e.target.value))}
            style={{flex:1, accentColor:"var(--blue)", cursor:"pointer",
                    background:"transparent", border:"none", padding:0}}
            disabled={!running}
          />
          <span style={{
            minWidth:52, textAlign:"center", fontWeight:700, fontSize:15,
            color:"var(--text-1)", fontVariantNumeric:"tabular-nums",
          }}>
            {duration}s
          </span>
        </div>
        <div style={{
          display:"flex", justifyContent:"space-between",
          fontSize:11, color:"var(--text-3)", marginTop:3,
        }}>
          <span>5s</span><span>30s</span><span>60s</span><span>2m</span>
        </div>
      </div>

      <div style={{display:"flex", gap:8}}>
        {!active ? (
          <button className="btn" style={{
            flex:1, justifyContent:"center", fontWeight:600,
            background:"var(--blue-bg)", borderColor:"rgba(75,140,255,.4)", color:"#93b9ff",
          }}
            onClick={trigger} disabled={!running}>
            ▶ Start {duration}s ad break
          </button>
        ) : (
          <button className="btn" style={{
            flex:1, justifyContent:"center", fontWeight:600,
            background:"var(--amber-bg)", borderColor:"rgba(245,158,11,.4)", color:"#fcd34d",
          }}
            onClick={stop}>
            ■ Stop ad break now
          </button>
        )}
      </div>

      {!running && (
        <p style={{fontSize:11.5, color:"var(--text-3)", marginTop:10}}>
          Start the pipeline first to use manual ad breaks.
        </p>
      )}
    </div>
  );
}

// ─── Stream quality stats ───────────────────────────────────────────────────
function StreamQuality({ pipeline }) {
  if (!pipeline) return null;

  const ingest  = pipeline.ingest  ?? {};
  const encoder = pipeline.encoder ?? {};
  const inStats  = ingest.stats  ?? {};
  const encStats = encoder.stats ?? {};

  const row = (label, val, unit, warn) => (
    <div style={{
      display:"flex", justifyContent:"space-between", alignItems:"center",
      padding:"7px 0", borderBottom:"1px solid var(--border)",
    }}>
      <span style={{fontSize:13, color:"var(--text-2)"}}>{label}</span>
      <span style={{
        fontSize:13, fontWeight:600, fontVariantNumeric:"tabular-nums",
        color: warn ? "var(--amber)" : "var(--text-1)",
        fontFamily:"var(--mono)",
      }}>
        {val != null && val !== 0 ? `${val}${unit}` : <span style={{color:"var(--text-3)"}}>—</span>}
      </span>
    </div>
  );

  const fmtBr = (kbps) => kbps > 1000 ? `${(kbps/1000).toFixed(1)} Mbps` : kbps > 0 ? `${Math.round(kbps)} kbps` : null;
  const speedWarn = encStats.speed > 0 && encStats.speed < 0.95;

  return (
    <div className="card" style={{padding:20}}>
      <SectionHeader>📊  Stream Quality</SectionHeader>
      <div style={{display:"grid", gridTemplateColumns:"1fr 1fr", gap:0}}>

        {/* Input column */}
        <div style={{paddingRight:16, borderRight:"1px solid var(--border)"}}>
          <div style={{
            fontSize:11, fontWeight:700, color:"var(--blue)",
            textTransform:"uppercase", letterSpacing:".05em", marginBottom:8,
          }}>Input (source)</div>
          {row("FPS",     inStats.fps > 0 ? inStats.fps.toFixed(1) : null, " fps")}
          {row("Bitrate", fmtBr(inStats.bitrate_kbps), "")}
          {row("Frames",  inStats.frames > 0 ? inStats.frames.toLocaleString() : null, "")}
          {row("Dropped", inStats.drop_frames || null, " frames", inStats.drop_frames > 0)}
        </div>

        {/* Output column */}
        <div style={{paddingLeft:16}}>
          <div style={{
            fontSize:11, fontWeight:700, color:"var(--green)",
            textTransform:"uppercase", letterSpacing:".05em", marginBottom:8,
          }}>Output (relay)</div>
          {row("FPS",     encStats.fps > 0 ? encStats.fps.toFixed(1) : null, " fps")}
          {row("Bitrate", fmtBr(encStats.bitrate_kbps), "")}
          {row("Speed",   encStats.speed > 0 ? encStats.speed.toFixed(2) : null, "×", speedWarn)}
          {row("Dropped", encStats.drop_frames || null, " frames", encStats.drop_frames > 0)}
        </div>
      </div>

      {speedWarn && (
        <div style={{
          marginTop:12, padding:"8px 12px", borderRadius:8, fontSize:12.5,
          background:"var(--amber-bg)", color:"#fcd34d",
          border:"1px solid rgba(245,158,11,.3)",
        }}>
          ⚠ Encoding speed below 1× — stream may lag behind live
        </div>
      )}
    </div>
  );
}

// ─── Health row ────────────────────────────────────────────────────────────
function HealthRow({ label, ok, restarts, note }) {
  return (
    <div style={{
      display:"flex", alignItems:"center", justifyContent:"space-between",
      padding:"8px 12px", borderRadius:8,
      background: ok ? "var(--green-bg)" : "var(--bg-card2)",
      border:`1px solid ${ok ? "rgba(34,197,94,.2)" : "var(--border)"}`,
    }}>
      <div style={{display:"flex", alignItems:"center", gap:9}}>
        <Dot state={ok ? "ok" : "neutral"} />
        <span style={{fontSize:13, fontWeight:500}}>{label}</span>
      </div>
      <span style={{fontSize:12, color:"var(--text-3)"}}>
        {note || (restarts > 0 ? `${restarts} restart${restarts > 1 ? "s" : ""}` : ok ? "OK" : "—")}
      </span>
    </div>
  );
}

// ─── Step heading ──────────────────────────────────────────────────────────
function StepHeading({ step, emoji, title }) {
  return (
    <div style={{display:"flex", alignItems:"center", gap:14}}>
      <div style={{
        width:36, height:36, borderRadius:10, flexShrink:0,
        background:"var(--blue-bg)", border:"1px solid rgba(75,140,255,.35)",
        display:"flex", alignItems:"center", justifyContent:"center",
        fontSize:15, fontWeight:700, color:"var(--blue)",
      }}>{step}</div>
      <div style={{fontWeight:600, fontSize:15}}>
        {emoji}&nbsp;&nbsp;{title}
      </div>
    </div>
  );
}

// ─── Setup form (stopped state) ────────────────────────────────────────────
function SetupForm({ form, setForm }) {
  const update = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const applyPreset = (name) => {
    const p = PRESETS[name];
    if (p) setForm(f => ({ ...f, preset: name, ...p }));
    else   setForm(f => ({ ...f, preset: "custom" }));
  };

  return (
    <div style={{display:"flex", flexDirection:"column", gap:14}}>

      {/* Step 1 — Input */}
      <div className="card" style={{padding:24}}>
        <StepHeading step="1" emoji="📡" title="Where is your video coming from?" />
        <div style={{
          display:"grid", gridTemplateColumns:"220px 1fr",
          gap:14, marginTop:18,
        }}>
          <Field label="Quick preset">
            <select value={form.preset} onChange={e => applyPreset(e.target.value)}>
              {Object.keys(PRESETS).map(n => <option key={n}>{n}</option>)}
              <option value="custom">Custom…</option>
            </select>
          </Field>
          <Field label="Input stream address"
            hint="The SRT, UDP, or RTMP address of your source stream">
            <input
              value={form.input_url}
              onChange={e => update("input_url", e.target.value)}
              placeholder="srt://host:port?mode=caller&latency=200"
            />
          </Field>
        </div>
      </div>

      {/* Step 2 — Output */}
      <div className="card" style={{padding:24}}>
        <StepHeading step="2" emoji="📺" title="Where should the relay send the video?" />
        <div style={{
          display:"grid", gridTemplateColumns:"1fr 180px",
          gap:14, marginTop:18,
        }}>
          <Field label="Output stream address"
            hint="Use 0.0.0.0 to accept incoming connections on all network interfaces">
            <input
              value={form.output_url}
              onChange={e => update("output_url", e.target.value)}
            />
          </Field>
          <Field label="Protocol">
            <select value={form.output_format}
              onChange={e => update("output_format", e.target.value)}>
              <option value="mpegts">SRT / UDP</option>
              <option value="flv">RTMP</option>
            </select>
          </Field>
        </div>
      </div>

      {/* Step 3 — Overlay */}
      <div className="card" style={{padding:24}}>
        <StepHeading step="3" emoji="🎬" title="What should appear on screen during ad breaks?" />
        <div style={{marginTop:18}}>
          <Field label="Overlay file path"
            hint="Full path to an image or video file with a transparent background (e.g. a PNG logo or MOV with alpha channel)">
            <input
              value={form.overlay_path}
              onChange={e => update("overlay_path", e.target.value)}
              placeholder="/path/to/overlay.mov"
            />
          </Field>
          <div style={{
            display:"grid",
            gridTemplateColumns:"repeat(4,1fr)",
            gap:12, marginTop:14,
          }}>
            <Field label="Position — Left (px)">
              <input type="number" value={form.overlay_x}
                onChange={e => update("overlay_x", e.target.value)} />
            </Field>
            <Field label="Position — Top (px)">
              <input type="number" value={form.overlay_y}
                onChange={e => update("overlay_y", e.target.value)} />
            </Field>
            <Field label="Width" hint="Leave blank = original size">
              <input type="number" value={form.overlay_w}
                onChange={e => update("overlay_w", e.target.value)}
                placeholder="auto" />
            </Field>
            <Field label="Height" hint="Leave blank = original size">
              <input type="number" value={form.overlay_h}
                onChange={e => update("overlay_h", e.target.value)}
                placeholder="auto" />
            </Field>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Advanced settings ─────────────────────────────────────────────────────
function AdvancedForm({ form, setForm }) {
  const update = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const triggers = new Set(form.triggered_segmentation_types ?? []);

  const toggleTrigger = (id) => {
    const s = new Set(triggers);
    s.has(id) ? s.delete(id) : s.add(id);
    update("triggered_segmentation_types", Array.from(s).sort((a, b) => a - b));
  };

  return (
    <div style={{display:"flex", flexDirection:"column", gap:24}}>

      <div>
        <SectionHeader>Overlay timing</SectionHeader>
        <div style={{display:"grid", gridTemplateColumns:"1fr 1fr", gap:14}}>
          <Field label="Show overlay for (milliseconds)"
            hint="Leave blank to use the duration embedded in each ad marker">
            <input type="number" value={form.overlay_duration_ms}
              onChange={e => update("overlay_duration_ms", e.target.value)}
              placeholder="auto — uses the ad marker's built-in duration" />
          </Field>
        </div>
      </div>

      <div>
        <SectionHeader>Which ad events trigger the overlay?</SectionHeader>
        <p style={{fontSize:13, color:"var(--text-2)", marginBottom:14}}>
          Tap to toggle. Blue = start events (show overlay). Orange = end events (hide overlay).
          Grey = currently disabled.
        </p>
        <div style={{display:"flex", flexWrap:"wrap", gap:8}}>
          {Object.entries(SEG_NAMES).map(([id, name]) => {
            const numId  = Number(id);
            const active = triggers.has(numId);
            const isEnd  = IS_END_TYPE.has(numId);
            return (
              <button key={id} type="button" onClick={() => toggleTrigger(numId)}
                style={{
                  padding:"7px 13px", borderRadius:8, fontSize:13,
                  fontWeight:500, cursor:"pointer",
                  background: active
                    ? isEnd ? "var(--amber-bg)" : "var(--blue-bg)"
                    : "var(--bg-card2)",
                  color: active
                    ? isEnd ? "#fcd34d" : "#93b9ff"
                    : "var(--text-3)",
                  border: active
                    ? isEnd ? "1px solid rgba(245,158,11,.45)" : "1px solid rgba(75,140,255,.45)"
                    : "1px solid var(--border)",
                  transition:"all .12s",
                }}>
                {isEnd ? "■ " : "▶ "}{name}
              </button>
            );
          })}
        </div>
      </div>

      <div>
        <SectionHeader>Encoder quality</SectionHeader>
        <div style={{display:"grid", gridTemplateColumns:"1fr 1fr", gap:14}}>
          <Field label="Speed / quality"
            hint="Faster = less CPU usage, slightly lower quality. 'veryfast' is the sweet spot for live streams.">
            <select value={form.encoder_preset}
              onChange={e => update("encoder_preset", e.target.value)}>
              {["ultrafast","superfast","veryfast","fast","medium"].map(p => (
                <option key={p} value={p}>
                  {p === "veryfast" ? `${p}  (recommended)` : p}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Output bitrate"
            hint="Higher = better picture quality but uses more bandwidth. '4M' is good for HD.">
            <input value={form.encoder_bitrate}
              onChange={e => update("encoder_bitrate", e.target.value)}
              placeholder="4M" />
          </Field>
        </div>
      </div>

    </div>
  );
}

// ─── Event log ─────────────────────────────────────────────────────────────
function EventLog({ events, onClear }) {
  return (
    <div className="card" style={{overflow:"hidden"}}>
      <div style={{
        padding:"15px 20px",
        display:"flex", alignItems:"center", justifyContent:"space-between",
        borderBottom:"1px solid var(--border)",
      }}>
        <div>
          <div style={{fontWeight:600, fontSize:14}}>Ad Marker Log</div>
          <div style={{fontSize:12.5, color:"var(--text-2)", marginTop:2}}>
            {events.length === 0
              ? "No markers detected yet"
              : `${events.length} marker${events.length !== 1 ? "s" : ""} received`}
          </div>
        </div>
        {events.length > 0 && (
          <button className="btn-ghost btn" onClick={onClear}>Clear</button>
        )}
      </div>

      {events.length === 0 ? (
        <div style={{
          padding:"52px 0", textAlign:"center",
          display:"flex", flexDirection:"column", alignItems:"center", gap:10,
        }}>
          <span style={{fontSize:40}}>📭</span>
          <div style={{fontSize:14, color:"var(--text-2)", fontWeight:500}}>
            Waiting for ad markers…
          </div>
          <div style={{fontSize:12.5, color:"var(--text-3)", maxWidth:360}}>
            When your source stream includes ad insertion signals, they will appear
            here in plain language.
          </div>
        </div>
      ) : (
        <div style={{maxHeight:440, overflowY:"auto"}}>
          {events.map((e, i) => (
            <EventRow key={e.id ?? i} event={e} fresh={i === 0} />
          ))}
        </div>
      )}
    </div>
  );
}

function EventRow({ event, fresh }) {
  const typeId   = event.segmentation_type_id;
  const typeName = SEG_NAMES[typeId] ?? (event.segmentation_type || "Signal Received");
  const isEnd    = IS_END_TYPE.has(typeId);
  const dur      = fmtDuration(event.duration_seconds);

  return (
    <div className={fresh ? "anim-event-in" : ""} style={{
      display:"flex", alignItems:"center", gap:14,
      padding:"13px 20px",
      borderBottom:"1px solid var(--border)",
      background: event.overlay_applied ? "rgba(75,140,255,.04)" : "transparent",
    }}>
      {/* Event type icon */}
      <div style={{
        width:38, height:38, borderRadius:9, flexShrink:0,
        display:"flex", alignItems:"center", justifyContent:"center",
        fontSize:16,
        background: isEnd ? "var(--bg-card2)" : "var(--blue-bg)",
        border:`1px solid ${isEnd ? "var(--border)" : "rgba(75,140,255,.28)"}`,
      }}>
        {isEnd ? "■" : "▶"}
      </div>

      {/* Text */}
      <div style={{flex:1, minWidth:0}}>
        <div style={{
          display:"flex", alignItems:"center", gap:8, flexWrap:"wrap",
        }}>
          <span style={{fontWeight:600, fontSize:13.5}}>{typeName}</span>

          {dur && (
            <span style={{
              fontSize:12, color:"var(--text-3)",
              background:"var(--bg-card2)",
              padding:"2px 8px", borderRadius:999,
            }}>
              {dur}
            </span>
          )}

          {event.overlay_applied ? (
            <span style={{
              fontSize:12, color:"#4ade80",
              background:"var(--green-bg)",
              padding:"2px 9px", borderRadius:999,
              border:"1px solid rgba(34,197,94,.25)",
            }}>
              Overlay shown ✓
            </span>
          ) : (
            <span style={{
              fontSize:12, color:"var(--text-3)",
              background:"var(--bg-card2)",
              padding:"2px 9px", borderRadius:999,
            }}>
              Not triggered
            </span>
          )}
        </div>

        {event.overlay_reason && event.overlay_reason !== "duplicate" && (
          <div style={{fontSize:12, color:"var(--text-3)", marginTop:3}}>
            {event.overlay_reason}
          </div>
        )}
      </div>

      {/* Time */}
      <span style={{
        fontSize:12.5, color:"var(--text-3)",
        fontFamily:"var(--mono)", flexShrink:0,
      }}>
        {fmtTime(event.time)}
      </span>
    </div>
  );
}

// ─── Root component ────────────────────────────────────────────────────────
export default function App() {
  const [form,   setForm]   = useState(DEFAULTS);
  const [status, setStatus] = useState(null);
  const [events, setEvents] = useState([]);
  const [busy,   setBusy]   = useState(false);
  const [toast,  setToast]  = useState(null);
  const [, setTick]         = useState(0);
  const prevPkts = useRef({ count: 0, t: Date.now() });
  const [pktRate, setPktRate] = useState(0);

  // Tick every second to update uptime display
  useEffect(() => {
    const t = setInterval(() => setTick(x => x + 1), 1000);
    return () => clearInterval(t);
  }, []);

  // Poll backend status every 1.5 s
  useEffect(() => {
    const poll = async () => {
      try {
        const r = await fetch(`${API}/api/status`);
        if (!r.ok) return;
        const j = await r.json();
        setStatus(j);
        const pkts = j.detector?.stats?.ts_packets ?? 0;
        const now  = Date.now();
        const dt   = (now - prevPkts.current.t) / 1000;
        if (dt > 0 && prevPkts.current.count > 0)
          setPktRate(Math.max(0, (pkts - prevPkts.current.count) / dt));
        prevPkts.current = { count: pkts, t: now };
      } catch { /* backend offline */ }
    };
    poll();
    const id = setInterval(poll, 1500);
    return () => clearInterval(id);
  }, []);

  // WebSocket for live event feed
  useEffect(() => {
    let ws, retry;
    const connect = () => {
      ws = new WebSocket(WS_BASE + "/ws/events");
      ws.onmessage = (m) => {
        try {
          const msg = JSON.parse(m.data);
          if (msg.type === "marker")
            setEvents(ev => [msg.data, ...ev].slice(0, 300));
        } catch {}
      };
      ws.onclose = () => { retry = setTimeout(connect, 2500); };
    };
    connect();
    const ka = setInterval(() => ws?.readyState === 1 && ws.send("ping"), 20000);
    return () => { clearTimeout(retry); clearInterval(ka); ws?.close(); };
  }, []);

  // Backfill event log on mount
  useEffect(() => {
    fetch(`${API}/api/markers?limit=200`)
      .then(r => r.json())
      .then(j => setEvents(j.events.reverse()))
      .catch(() => {});
  }, []);

  const start = async () => {
    setBusy(true); setToast(null);
    try {
      const body = { ...form };
      delete body.preset;
      ["overlay_w","overlay_h","overlay_duration_ms"].forEach(k => {
        body[k] = body[k] === "" ? null : Number(body[k]);
      });
      ["overlay_x","overlay_y"].forEach(k => { body[k] = Number(body[k]); });
      const r = await fetch(`${API}/api/start`, {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body: JSON.stringify(body),
      });
      if (!r.ok) throw new Error((await r.text()).slice(0, 400));
      setToast({ tone:"ok", text:"Stream relay started successfully." });
    } catch(e) {
      setToast({ tone:"err", text: String(e.message || e) });
    } finally { setBusy(false); }
  };

  const stop = async () => {
    setBusy(true);
    try { await fetch(`${API}/api/stop`, { method:"POST" }); }
    finally { setBusy(false); }
  };

  const stopOverlay = async () => {
    try {
      await fetch(`${API}/api/ad/stop`, { method:"POST" });
      setToast({ tone:"ok", text:"Overlay stopped" });
    } catch(e) {
      setToast({ tone:"err", text:`Failed to stop overlay: ${e.message}` });
    }
  };

  const clearEvents = () =>
    fetch(`${API}/api/markers`, { method:"DELETE" }).then(() => setEvents([]));

  // ── Derived state ──────────────────────────────────────────────────────────
  const running    = !!status?.running;
  const ingestOk   = !!status?.pipeline?.ingest?.running;
  const encoderOk  = !!status?.pipeline?.encoder?.running;
  const overlayOn  = !!status?.overlay_active;
  const scteLocked = !!status?.detector?.scte_pid;
  const stats      = status?.detector?.stats ?? {};
  const latencyMs  = running ? (status?.pipeline?.stream_latency_ms ?? null) : null;
  const lastEvAgo  = stats.last_event_at
    ? Math.max(0, Math.floor(Date.now() / 1000 - stats.last_event_at))
    : null;

  const overallState = !running    ? "neutral"
    : ingestOk && encoderOk        ? "ok"
    : "warn";

  // ── Render ─────────────────────────────────────────────────────────────────
  return (
    <div style={{minHeight:"100vh", background:"var(--bg-page)"}}>
      {toast && (
        <Toast tone={toast.tone} text={toast.text} onClose={() => setToast(null)} />
      )}

      {/* ──────────── Header ──────────── */}
      <header style={{
        position:"sticky", top:0, zIndex:50,
        background:"rgba(11,15,23,.9)", backdropFilter:"blur(14px)",
        borderBottom:"1px solid var(--border)",
      }}>
        <div style={{
          maxWidth:1280, margin:"0 auto",
          padding:"12px 24px",
          display:"flex", alignItems:"center",
          justifyContent:"space-between", gap:16,
        }}>
          {/* Logo */}
          <div style={{display:"flex", alignItems:"center", gap:12}}>
            <div style={{
              width:38, height:38, borderRadius:10,
              background:"linear-gradient(135deg,#4b8cff,#9333ea)",
              display:"flex", alignItems:"center", justifyContent:"center",
              boxShadow:"0 4px 16px rgba(75,140,255,.35)", flexShrink:0,
            }}>
              <svg width={20} height={20} viewBox="0 0 24 24" fill="white">
                <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
              </svg>
            </div>
            <div>
              <div style={{fontWeight:700, fontSize:15, lineHeight:1.2}}>
                Stream Ad Overlay
              </div>
              <div style={{fontSize:11.5, color:"var(--text-3)"}}>
                Broadcast relay with automatic ad markers
              </div>
            </div>
          </div>

          {/* Status + action button */}
          <div style={{display:"flex", alignItems:"center", gap:10}}>
            <div style={{
              display:"flex", alignItems:"center", gap:8,
              padding:"8px 14px", borderRadius:999,
              background:"var(--bg-card2)", border:"1px solid var(--border)",
            }}>
              <Dot state={overallState} pulse={running} />
              <span style={{fontSize:13, fontWeight:600}}>
                {!running               ? "Offline"
                  : ingestOk && encoderOk ? "Live"
                  : "Starting…"}
              </span>
              {running && (
                <span style={{
                  fontSize:12, color:"var(--text-3)",
                  borderLeft:"1px solid var(--border)",
                  paddingLeft:10, marginLeft:2,
                }}>
                  {fmtUptime(status?.started_at)}
                </span>
              )}
            </div>

            {running ? (
              <button className="btn-stop btn" onClick={stop} disabled={busy}>
                <svg width={14} height={14} viewBox="0 0 24 24" fill="currentColor">
                  <rect x="6" y="6" width="12" height="12" rx="1"/>
                </svg>
                Stop
              </button>
            ) : (
              <button className="btn-go btn" onClick={start} disabled={busy}>
                {busy ? (
                  <svg width={16} height={16} viewBox="0 0 24 24" fill="none"
                    stroke="currentColor" strokeWidth={2} className="anim-spin"
                    style={{borderRadius:"50%"}}>
                    <circle cx="12" cy="12" r="9" strokeDasharray="28" strokeDashoffset="8"/>
                  </svg>
                ) : (
                  <svg width={16} height={16} viewBox="0 0 24 24" fill="currentColor">
                    <polygon points="5 3 19 12 5 21 5 3"/>
                  </svg>
                )}
                Go Live
              </button>
            )}
          </div>
        </div>
      </header>

      {/* ──────────── Main ──────────── */}
      <main style={{
        maxWidth:1280, margin:"0 auto",
        padding:"28px 24px 80px",
      }}>

        {/* ═══════════ LIVE VIEW ═══════════ */}
        {running && (
          <div className="anim-fade-in" style={{
            display:"flex", flexDirection:"column", gap:20,
          }}>

            {/* 4-stat row */}
            <div style={{
              display:"grid",
              gridTemplateColumns:"repeat(auto-fit, minmax(218px, 1fr))",
              gap:14,
            }}>
              <StatCard icon="📡" title="Input Signal"
                value={ingestOk ? "Connected" : "Connecting…"}
                sub={ingestOk
                  ? `${Math.round(pktRate).toLocaleString()} packets / sec`
                  : "Waiting for source stream…"}
                state={ingestOk ? "ok" : "warn"}
              />

              <StatCard icon="📺" title="Output Stream"
                value={encoderOk ? "Broadcasting" : "Starting…"}
                sub={encoderOk
                  ? "Relay is live and encoding"
                  : "Video encoder warming up…"}
                state={encoderOk ? "ok" : "warn"}
              />

              <StatCard icon="⏱" title="Stream Delay"
                state={
                  latencyMs == null ? "neutral"
                  : latencyMs < 500  ? "ok"
                  : latencyMs < 1000 ? "warn"
                  : "bad"
                }>
                <LatencyGauge ms={latencyMs} />
              </StatCard>

              <StatCard icon="🎯" title="Ad Markers Detected"
                value={(stats.scte_sections ?? 0).toLocaleString()}
                sub={
                  lastEvAgo != null
                    ? lastEvAgo < 5  ? "Just detected ✓"
                    : lastEvAgo < 60 ? `${lastEvAgo}s ago`
                    : `${Math.floor(lastEvAgo / 60)}m ago`
                    : scteLocked
                    ? "Ad signal found — awaiting markers"
                    : "No ad signal in source yet"
                }
                state={stats.scte_sections > 0 ? "ok" : "neutral"}
              />
            </div>

            {/* Overlay-active banner */}
            {overlayOn && (
              <div style={{
                padding:"14px 20px", borderRadius:12,
                background:"rgba(245,158,11,.1)",
                border:"1px solid rgba(245,158,11,.35)",
                display:"flex", alignItems:"center", gap:14,
              }}>
                <span style={{fontSize:24}}>🎬</span>
                <div style={{flex:1}}>
                  <div style={{
                    fontWeight:600, color:"#fcd34d", fontSize:14,
                  }}>
                    Overlay is ON — Ad break in progress
                  </div>
                  <div style={{fontSize:12.5, color:"var(--text-2)", marginTop:2}}>
                    {status?.last_overlay_result?.reason || "Triggered by ad marker"}
                  </div>
                </div>
                <button className="btn btn-sm"
                  onClick={stopOverlay}
                  style={{flexShrink:0}}>
                  Turn off overlay
                </button>
              </div>
            )}

            {/* Main 2-column layout */}
            <div style={{
              display:"grid",
              gridTemplateColumns:"1fr 280px",
              gap:14, alignItems:"start",
            }}>

              {/* Event log */}
              <EventLog events={events} onClear={clearEvents} />

              {/* Right sidebar */}
              <div style={{display:"flex", flexDirection:"column", gap:14}}>

                {/* Manual ad break insertion */}
                <ManualAdBreak running={running} />

                {/* System health */}
                <div className="card" style={{padding:20}}>
                  <SectionHeader>System health</SectionHeader>
                  <div style={{display:"flex", flexDirection:"column", gap:8}}>
                    <HealthRow label="Input reader" ok={ingestOk}
                      restarts={status?.pipeline?.ingest?.restarts} />
                    <HealthRow label="Video encoder" ok={encoderOk}
                      restarts={status?.pipeline?.encoder?.restarts} />
                    <HealthRow label="Ad signal"
                      ok={scteLocked}
                      note={scteLocked
                        ? `Found (${status.detector.scte_pid})`
                        : "Searching…"} />
                    <HealthRow label="Overlay mode"
                      ok={true}
                      note="Encoder restart" />
                  </div>
                </div>

                {/* Stream quality stats */}
                <StreamQuality pipeline={status?.pipeline} />

                {/* Watch output hint */}
                <Collapse title="▶  Watch the output stream" defaultOpen={false}>
                  <p style={{fontSize:13, color:"var(--text-2)", marginBottom:10}}>
                    Run this in a terminal to preview your output:
                  </p>
                  <pre style={{
                    background:"var(--bg-card2)", padding:12, borderRadius:8,
                    fontSize:11.5, whiteSpace:"pre-wrap", margin:0,
                    color:"var(--text-1)", fontFamily:"var(--mono)",
                  }}>
{`ffplay -fflags nobuffer \\
  -flags low_delay \\
  "${form.output_url
    .replace("0.0.0.0","127.0.0.1")
    .replace("listener","caller")}"`}
                  </pre>
                </Collapse>

              </div>
            </div>

          </div>
        )}

        {/* ═══════════ STOPPED VIEW ═══════════ */}
        {!running && (
          <div className="anim-fade-in" style={{
            display:"flex", flexDirection:"column", gap:20,
          }}>

            {/* Hero banner */}
            <div className="card" style={{
              padding:"36px 40px",
              background:"linear-gradient(135deg,#111827 0%,#0e1929 100%)",
              display:"flex", alignItems:"center",
              justifyContent:"space-between", gap:32, flexWrap:"wrap",
            }}>
              <div>
                <h1 style={{fontSize:26, fontWeight:700, marginBottom:8}}>
                  Ready to go live
                </h1>
                <p style={{fontSize:14, color:"var(--text-2)", maxWidth:500, lineHeight:1.6}}>
                  Fill in the three steps below to set up your stream relay.
                  When you click "Go Live", this tool will automatically detect
                  ad break signals and show your overlay in real time.
                </p>
              </div>
              <button className="btn-go btn" onClick={start} disabled={busy}
                style={{fontSize:17, padding:"15px 40px", flexShrink:0}}>
                {busy ? "Starting…" : "▶  Go Live"}
              </button>
            </div>

            {/* 3-step setup */}
            <SetupForm form={form} setForm={setForm} />

            {/* Advanced settings collapsed */}
            <Collapse title="⚙  Advanced Settings" defaultOpen={false}>
              <AdvancedForm form={form} setForm={setForm} />
            </Collapse>

            {/* Show recent events even when stopped */}
            {events.length > 0 && (
              <EventLog events={events} onClear={clearEvents} />
            )}

          </div>
        )}

      </main>
    </div>
  );
}
