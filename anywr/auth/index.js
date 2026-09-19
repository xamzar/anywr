// Verify the Access JWT ourselves rather than trusting the email header:
// if the Access app were ever misconfigured, a header is spoofable, a signature isn't.
const enc = (s) => new TextEncoder().encode(s);
const b64url = (buf) => btoa(String.fromCharCode(...new Uint8Array(buf)))
  .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
const unb64url = (s) => Uint8Array.from(atob(s.replace(/-/g, "+").replace(/_/g, "/")), (c) => c.charCodeAt(0));

async function accessEmail(req, env) {
  const jwt = req.headers.get("cf-access-jwt-assertion");
  if (!jwt) return null;
  const [h, p, sig] = jwt.split(".");
  const header = JSON.parse(new TextDecoder().decode(unb64url(h)));
  const { keys } = await (await fetch(`${env.TEAM}/cdn-cgi/access/certs`, { cf: { cacheTtl: 3600 } })).json();
  const jwk = keys.find((k) => k.kid === header.kid);
  if (!jwk || header.alg !== "RS256") return null;
  const key = await crypto.subtle.importKey("jwk", jwk, { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" }, false, ["verify"]);
  if (!(await crypto.subtle.verify("RSASSA-PKCS1-v1_5", key, unb64url(sig), enc(`${h}.${p}`)))) return null;
  const c = JSON.parse(new TextDecoder().decode(unb64url(p)));
  const aud = Array.isArray(c.aud) ? c.aud : [c.aud];
  if (c.iss !== env.TEAM || !aud.includes(env.ACCESS_AUD) || c.exp < Date.now() / 1000) return null;
  return c.email || null;
}

// /start (an Access bypass path): app.py already began the Access login and
// submitted the page owner's email. Adopt that login's app-session cookie and ask
// only for the code, posting it where Access's own code page would.
async function start(url, env) {
  const [body, sig] = (url.searchParams.get("b") || "").split(".");
  let d;
  try {
    const key = await crypto.subtle.importKey("raw", enc(env.SSO_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    if (!(await crypto.subtle.verify("HMAC", key, unb64url(sig), enc(body)))) throw 0;
    d = JSON.parse(new TextDecoder().decode(unb64url(body)));
  } catch { d = null; }
  if (!d || d.x < Date.now() / 1000 || !/^[\w-]+$/.test(d.a) || !/^[\w-]+$/.test(d.n))
    return new Response("Start signing in from anywr.me.", { status: 400 });
  const html = `<!doctype html><meta name=viewport content="width=device-width,initial-scale=1"><title>anywr</title>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500&display=swap" rel=stylesheet>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100dvh;display:grid;place-items:center;padding:16px;background:#000;color:#ededed;
 font:15px/1.5 "Instrument Sans",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
form{width:min(360px,100%)}label{display:block;color:#8a8a8a;font-size:14px}
input{display:block;width:100%;margin:6px 0 24px;padding:6px 0;font:500 28px/1.2 "Instrument Sans",system-ui,sans-serif;
 letter-spacing:.2em;color:#ededed;background:none;border:0;border-bottom:1px solid #262626;border-radius:0}
input:focus{outline:none;border-bottom-color:#ededed}
button{font:500 14px/1 "Instrument Sans",system-ui,sans-serif;color:#000;background:#ededed;border:1px solid #ededed;
 border-radius:6px;padding:9px 14px;cursor:pointer}button:focus-visible{outline:2px solid #ededed;outline-offset:2px}
</style>
<form method=post action="${env.TEAM}/cdn-cgi/access/callback"><label for=code>Code from your email</label>
<input id=code name=code inputmode=numeric pattern="\\d{6}" maxlength=6 autocomplete=one-time-code required autofocus>
<input type=hidden name=nonce value="${d.n}"><button>Sign in</button></form>`;
  return new Response(html, { headers: {
    "content-type": "text/html; charset=utf-8", "cache-control": "no-store", "referrer-policy": "no-referrer",
    "set-cookie": `CF_AppSession=${d.a}; Path=/; Secure; HttpOnly; Max-Age=86400`,
  } });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    if (url.pathname === "/start") return start(url, env);
    const state = url.searchParams.get("state") || "";
    if (!/^[A-Za-z0-9_-]{20,64}$/.test(state)) return new Response("Start signing in from anywr.me.", { status: 400 });
    const email = await accessEmail(req, env);
    if (!email) return new Response("Not verified by Cloudflare Access.", { status: 403 });
    const body = b64url(enc(JSON.stringify({ e: email, s: state, x: Math.floor(Date.now() / 1000) + 120 })));
    const key = await crypto.subtle.importKey("raw", enc(env.SSO_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"]);
    const sig = b64url(await crypto.subtle.sign("HMAC", key, enc(body)));
    // Fixed destination: the state is the only thing the caller controls.
    return Response.redirect(`${env.RETURN_URL}?t=${body}.${sig}`, 302);
  },
};
