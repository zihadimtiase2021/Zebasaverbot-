"""Shared look & feel (HTML/CSS/JS strings, standard library only) for the setup panel and the admin panel."""
from __future__ import annotations

import html

def e(s) -> str:
    return html.escape(str(s), quote=True)


# --------------------------------------------------------------------------------------------- design
CSS = """
:root{--bg:#f4f6fb;--card:#fff;--text:#0f172a;--muted:#64748b;--line:#e2e8f0;--brand:#2563eb;--brand2:#7c3aed;--ok:#16a34a;
--err:#dc2626;--warn:#d97706;--input:#f8fafc;--shadow:0 10px 30px rgba(15,23,42,.08);color-scheme:light}
@media(prefers-color-scheme:dark){:root{--bg:#0b1020;--card:#121a2f;--text:#e8edf7;--muted:#94a3b8;--line:#243049;--brand:#60a5fa;
--brand2:#a78bfa;--ok:#4ade80;--err:#f87171;--warn:#fbbf24;--input:#0e1526;--shadow:0 10px 30px rgba(0,0,0,.4);color-scheme:dark}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--text);font:16px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
padding:max(20px,env(safe-area-inset-top)) 16px max(28px,env(safe-area-inset-bottom))}
.wrap{max-width:640px;margin:0 auto}
header{display:flex;align-items:center;gap:12px;margin:8px 0 22px}
.logo{width:42px;height:42px;border-radius:12px;display:grid;place-items:center;color:#fff;
background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:0 6px 16px rgba(37,99,235,.35)}
header h1{font-size:20px;margin:0;line-height:1.2}header p{margin:0;color:var(--muted);font-size:13px}
.steps{display:flex;list-style:none;padding:0;margin:0 0 18px;gap:8px}
.steps li{flex:1;display:flex;align-items:center;gap:8px;color:var(--muted);font-size:13px;font-weight:600;min-width:0}
.steps .dot{flex:none;width:26px;height:26px;border-radius:50%;display:grid;place-items:center;font-size:12px;
border:2px solid var(--line);background:var(--card)}
.steps .lbl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.steps li.active{color:var(--text)}.steps li.active .dot{border-color:var(--brand);color:var(--brand)}
.steps li.done .dot{background:var(--ok);border-color:var(--ok);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:22px;box-shadow:var(--shadow)}
.card h2{margin:0 0 4px;font-size:19px}.sub{color:var(--muted);margin:0 0 18px;font-size:14px}
label{display:block;font-weight:600;font-size:14px;margin:14px 0 6px}
.hint{color:var(--muted);font-size:12.5px;margin:5px 0 0;font-weight:400}
input,select,textarea{width:100%;padding:12px 13px;border-radius:11px;border:1px solid var(--line);background:var(--input);
color:var(--text);font:inherit;font-size:16px;outline:none;transition:border-color .15s,box-shadow .15s}
input:focus,select:focus,textarea:focus{border-color:var(--brand);box-shadow:0 0 0 4px color-mix(in srgb,var(--brand) 20%,transparent)}
textarea{min-height:96px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;resize:vertical}
.field{position:relative}.field .eye{position:absolute;right:6px;top:50%;transform:translateY(-50%);width:auto;padding:6px 10px;margin:0;
background:none;border:0;color:var(--muted);font-size:12px;font-weight:600;cursor:pointer;box-shadow:none}
.field input{padding-right:62px}
button,.btn{display:inline-flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:13px 16px;margin-top:18px;border:0;
border-radius:12px;font:inherit;font-weight:700;font-size:16px;color:#fff;cursor:pointer;text-decoration:none;
background:linear-gradient(135deg,var(--brand),var(--brand2));box-shadow:0 8px 18px rgba(37,99,235,.28)}
button:active{transform:translateY(1px)}button[disabled]{opacity:.6;cursor:wait}
button.ghost,.btn.ghost{background:transparent;color:var(--text);border:1px solid var(--line);box-shadow:none}
button.small{width:auto;margin:0;padding:7px 12px;font-size:13px;border-radius:9px}
.alert{border-radius:12px;padding:11px 14px;margin:0 0 16px;font-size:14px;border:1px solid}
.alert.err{color:var(--err);background:color-mix(in srgb,var(--err) 10%,transparent);border-color:color-mix(in srgb,var(--err) 35%,transparent)}
.alert.warn{color:var(--warn);background:color-mix(in srgb,var(--warn) 10%,transparent);border-color:color-mix(in srgb,var(--warn) 35%,transparent)}
.alert.info{color:var(--brand);background:color-mix(in srgb,var(--brand) 9%,transparent);border-color:color-mix(in srgb,var(--brand) 30%,transparent)}
fieldset{border:1px solid var(--line);border-radius:14px;padding:4px 16px 16px;margin:18px 0 0}
legend{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);padding:0 8px}
details{margin-top:18px;border:1px solid var(--line);border-radius:14px;padding:0 16px}
summary{cursor:pointer;padding:14px 0;font-weight:700;list-style:none;display:flex;justify-content:space-between}
summary::after{content:"+";color:var(--muted)}details[open] summary::after{content:"-"}details[open]{padding-bottom:14px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}@media(max-width:480px){.grid{grid-template-columns:1fr}}
.kv{border:1px solid var(--line);border-radius:12px;padding:10px 12px;margin:10px 0;background:var(--input)}
.kv .k{font-size:12px;font-weight:700;color:var(--muted);display:flex;justify-content:space-between;align-items:center;gap:8px}
.kv .v{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px;word-break:break-all;margin-top:4px;max-height:84px;overflow:auto}
.list{list-style:none;padding:0;margin:0}.list li{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);font-size:14px}
.list li:last-child{border:0}.okc{color:var(--ok)}.bad{color:var(--err)}
.center{text-align:center}.big{font-size:44px;line-height:1;margin:6px 0 10px}
footer{color:var(--muted);font-size:12px;text-align:center;margin-top:18px}
.row{display:flex;align-items:center;justify-content:space-between;gap:14px;padding:14px 0;border-bottom:1px solid var(--line)}
.row:last-child{border:0}.row .t{font-weight:600}.row .d{color:var(--muted);font-size:13px;margin-top:2px}
.sw{position:relative;flex:none;width:52px;height:30px}
.sw input{position:absolute;inset:0;width:100%;height:100%;margin:0;padding:0;border:0;background:none;box-shadow:none;opacity:0;cursor:pointer;z-index:2}
.sw span{position:absolute;inset:0;background:var(--line);border-radius:30px;transition:background .2s}
.sw span::after{content:"";position:absolute;left:3px;top:3px;width:24px;height:24px;border-radius:50%;background:#fff;transition:transform .2s;box-shadow:0 1px 3px rgba(0,0,0,.3)}
.sw input:checked+span{background:linear-gradient(135deg,var(--brand),var(--brand2))}
.sw input:checked+span::after{transform:translateX(22px)}
.stat{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.stat div{background:var(--input);border:1px solid var(--line);border-radius:12px;padding:10px 6px;text-align:center}
.stat b{display:block;font-size:20px}.stat small{color:var(--muted);font-size:11.5px}
.num{width:88px;text-align:center;padding:9px}
.pill{display:inline-block;padding:3px 10px;border-radius:99px;background:var(--input);border:1px solid var(--line);font-size:12.5px;word-break:break-all}
.toast{position:fixed;left:50%;bottom:calc(24px + env(safe-area-inset-bottom));transform:translateX(-50%);z-index:50;background:var(--card);color:var(--text);
border:1px solid var(--line);border-left:4px solid var(--brand);padding:11px 16px;border-radius:12px;box-shadow:var(--shadow);font-size:14px;max-width:90vw;display:none}
.toast.ok{border-left-color:var(--ok)}.toast.err{border-left-color:var(--err)}
.spin{width:18px;height:18px;border:3px solid rgba(255,255,255,.4);border-top-color:#fff;border-radius:50%;animation:s .8s linear infinite;display:none}
button[disabled] .spin{display:inline-block}@keyframes s{to{transform:rotate(360deg)}}
"""

JS = """
document.querySelectorAll('form:not([data-ajax])').forEach(f=>f.addEventListener('submit',()=>{const b=f.querySelector('button[type=submit]');
if(b){setTimeout(()=>{b.disabled=true},0)}}));
document.querySelectorAll('[data-eye]').forEach(b=>b.addEventListener('click',()=>{const i=document.getElementById(b.dataset.eye);
const s=i.type==='password';i.type=s?'text':'password';b.textContent=s?'Hide':'Show'}));
document.querySelectorAll('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{const t=document.getElementById(b.dataset.copy);
const v=t.textContent;try{await navigator.clipboard.writeText(v)}catch(_){const r=document.createRange();
r.selectNodeContents(t);getSelection().removeAllRanges();getSelection().addRange(r);document.execCommand('copy')}
const o=b.textContent;b.textContent='Copied';setTimeout(()=>b.textContent=o,1400)}));
"""

LOGO = ('<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M4 21h16"/></svg>')
SPIN = '<span class="spin"></span>'


def layout(body: str, step: int = 0, subtitle: str = "Setup panel", footer: str = "Secure session &middot; nothing is stored in logs",
           script: str = "") -> str:
    names = ["Connect Telegram", "Configure", "Launch"]
    items = "".join(
        f'<li class="{"done" if i < step else "active" if i == step else ""}"><span class="dot">{"&#10003;" if i < step else i}</span>'
        f'<span class="lbl">{n}</span></li>' for i, n in enumerate(names, 1))
    steps = f'<ol class="steps">{items}</ol>' if step else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><meta name="robots" content="noindex">
<title>Zeba</title><style>{CSS}</style></head><body><div class="wrap">
<header><div class="logo">{LOGO}</div><div><h1>Zeba Saver Bot</h1><p>{subtitle}</p></div></header>
{steps}{body}<footer>{footer}</footer></div><div class="toast" id="toast"></div><script>{JS}</script><script>{script}</script></body></html>"""


def alert(msg: str, kind: str = "err") -> str:
    return f'<div class="alert {kind}">{e(msg)}</div>' if msg else ""


def pw_field(name: str, label: str, hint: str = "", required: bool = False, extra: str = "") -> str:
    fid = f"f_{name}"
    return (f'<label for="{fid}">{e(label)}</label><div class="field"><input id="{fid}" name="{name}" type="password" '
            f'autocomplete="off" {"required" if required else ""} {extra}><button type="button" class="eye" data-eye="{fid}">Show</button></div>'
            + (f'<p class="hint">{hint}</p>' if hint else ""))


def kv(key: str, value: str, idx: int) -> str:
    return (f'<div class="kv"><div class="k"><span>{e(key)}</span><button type="button" class="ghost small" data-copy="v{idx}">Copy</button></div>'
            f'<div class="v" id="v{idx}">{e(value)}</div></div>')
