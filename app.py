"""
AI Form Assistant - web UI + auto scanner/filler for Google Forms.

Run:
    pip install flask playwright
    playwright install chromium
    python app.py
Then open http://127.0.0.1:5000

Only use on forms you own or have permission to test.
"""
import hmac
import os
import random
import re
import threading
import time
import uuid
import webbrowser
from datetime import date, timedelta
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request
from playwright.sync_api import sync_playwright

# ---------------- safety limits ----------------
MAX_RESPONSES = 200      # hard cap per run
MIN_DELAY_S = 3          # minimum pause between submissions
MAX_CONSECUTIVE_FAILS = 3

app = Flask(__name__)

# Hosted mode (set HOSTED=1 and APP_PASSWORD=... on the server).
# If APP_PASSWORD is missing, the site stays locked so it can never be open to everyone.
HOSTED = os.environ.get("HOSTED") == "1"
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")


@app.before_request
def require_password():
    if not HOSTED:
        return None
    auth = request.authorization
    if APP_PASSWORD and auth and hmac.compare_digest(auth.password or "", APP_PASSWORD):
        return None
    return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="Form Assistant"'})


JOBS = {}
LOCK = threading.Lock()
ACTIVE = {"id": None}


class Stopped(Exception):
    pass


class Blocked(Exception):
    """Sign-in wall, CAPTCHA, closed form... stop the whole run."""


# ---------------- random value helpers ----------------
FIRST = ["Amit", "Priya", "Rahul", "Sneha", "Arjun", "Neha", "Karan", "Pooja", "Vikram", "Anjali"]
LAST = ["Sharma", "Patel", "Singh", "Gupta", "Mehta", "Joshi", "Kulkarni", "Reddy", "Nair", "Khan"]
CITIES = ["Pune", "Mumbai", "Delhi", "Bengaluru", "Chennai", "Hyderabad", "Nagpur"]
WORDS = ["Good", "Satisfied", "Average", "Very helpful", "Okay", "Excellent", "Needs improvement"]


def auto_text(title):
    t = title.lower()
    if "email" in t or "e-mail" in t:
        return f"{random.choice(FIRST).lower()}{random.randint(10, 999)}@example.com"
    if any(k in t for k in ("phone", "mobile", "contact number", "whatsapp")):
        return str(random.randint(7000000000, 9999999999))
    if re.search(r"\bage\b", t):
        return str(random.randint(18, 60))
    if "name" in t:
        return f"{random.choice(FIRST)} {random.choice(LAST)}"
    if "city" in t or "location" in t:
        return random.choice(CITIES)
    return random.choice(WORDS)


def allowed_options(options, cfg):
    wanted = cfg.get("options")
    allowed = [o for o in options if o in wanted] if wanted else options
    return allowed or options


# ---------------- page scanning / filling ----------------
def clean_title(raw):
    first = raw.strip().split("\n")[0].strip()
    return re.sub(r"\s*\*\s*$", "", first).strip()


def get_title(block):
    h = block.locator('[role="heading"]')
    raw = h.first.inner_text() if h.count() else block.inner_text()
    return clean_title(raw)


def get_options(block, role):
    els = block.locator(f'[role="{role}"]')
    opts = []
    for i in range(els.count()):
        el = els.nth(i)
        if el.get_attribute("data-value") == "__other_option__":
            continue
        label = el.get_attribute("aria-label") or el.get_attribute("data-value") or ""
        if label.strip():
            opts.append((i, label.strip()))
    return els, opts


def process_block(page, block, config, log):
    """Detect one question, fill it randomly, return its description."""
    title = get_title(block)
    cfg = config.get(title, {})
    q = {"title": title, "type": "unknown", "options": []}

    # Multiple-choice grid: one radiogroup per row
    rows = block.locator('[role="radiogroup"]')
    if rows.count() > 1:
        q["type"] = "grid"
        for r in range(rows.count()):
            radios = rows.nth(r).locator('[role="radio"]')
            if radios.count():
                radios.nth(random.randrange(radios.count())).click()
        return q

    # Checkbox grid: one group per row
    groups = block.locator('[role="group"]')
    if groups.count() > 1 and block.locator('[role="checkbox"]').count():
        q["type"] = "grid"
        for r in range(groups.count()):
            boxes = groups.nth(r).locator('[role="checkbox"]')
            if boxes.count():
                boxes.nth(random.randrange(boxes.count())).click()
        return q

    # Dropdown
    lb = block.locator('[role="listbox"]')
    if lb.count():
        opt_els = block.locator('[role="option"]')
        vals = []
        for i in range(opt_els.count()):
            v = (opt_els.nth(i).get_attribute("data-value") or "").strip()
            if v:
                vals.append(v)
        q.update(type="dropdown", options=vals)
        if vals:
            pick = random.choice(allowed_options(vals, cfg))
            lb.first.click()
            page.wait_for_timeout(400)
            for i in range(opt_els.count()):
                if (opt_els.nth(i).get_attribute("data-value") or "") == pick:
                    opt_els.nth(i).click()
                    break
            page.wait_for_timeout(300)
        return q

    # Radio / linear scale
    radios, r_opts = get_options(block, "radio")
    if r_opts:
        labels = [l for _, l in r_opts]
        q.update(type="choice", options=labels)
        pick = random.choice(allowed_options(labels, cfg))
        idx = next(i for i, l in r_opts if l == pick)
        radios.nth(idx).click()
        return q

    # Checkboxes
    checks, c_opts = get_options(block, "checkbox")
    if c_opts:
        labels = [l for _, l in c_opts]
        q.update(type="checkbox", options=labels)
        allowed = allowed_options(labels, cfg)
        picks = random.sample(allowed, random.randint(1, min(2, len(allowed))))
        for p in picks:
            idx = next(i for i, l in c_opts if l == p)
            checks.nth(idx).click()
        return q

    # Date
    dt = block.locator('input[type="date"]')
    if dt.count():
        q["type"] = "date"
        dt.first.fill((date.today() - timedelta(days=random.randint(0, 365))).isoformat())
        return q

    # Text / paragraph / email / number
    fields = block.locator(
        'input[type="text"]:visible, input[type="tel"]:visible, input[type="email"]:visible, '
        'input[type="number"]:visible, input[type="url"]:visible, textarea:visible'
    )
    if fields.count():
        q["type"] = "text"
        values = [v for v in cfg.get("values", []) if v.strip()]
        fields.first.fill(random.choice(values) if values else auto_text(title))
        return q

    log(f"Skipped unsupported question: {title!r}")
    return q


def check_blockers(page):
    url = page.url
    if "accounts.google.com" in url or "ServiceLogin" in url:
        raise Blocked("This form requires Google sign-in. Only forms open to anyone with the link are supported.")
    body = page.inner_text("body")[:4000].lower()
    if "unusual traffic" in body or "captcha" in body or "not a robot" in body:
        raise Blocked("Google is showing a verification page. Stopping - wait a while before trying again.")
    if "no longer accepting responses" in body:
        raise Blocked("The form is no longer accepting responses.")


def required_error_visible(page):
    err = page.get_by_text("This is a required question", exact=False)
    return err.count() > 0 and err.first.is_visible()


def run_sections(page, config, log, stop, submit, collect=None):
    """Go through every section of the form once. Submits if submit=True."""
    section = 1
    while True:
        if stop.is_set():
            raise Stopped()
        check_blockers(page)
        page.wait_for_selector('div[role="list"] > div[role="listitem"]', timeout=15000)
        blocks = page.locator('div[role="list"] > div[role="listitem"]:visible')

        for i in range(blocks.count()):
            q = process_block(page, blocks.nth(i), config, log)
            if collect is not None:
                collect.append({**q, "section": section})

        page.wait_for_timeout(400)
        nxt = page.get_by_role("button", name="Next", exact=True)
        sub = page.get_by_role("button", name="Submit", exact=True)

        if nxt.count():
            nxt.first.click()
            page.wait_for_timeout(1200)
            if required_error_visible(page):
                raise RuntimeError(f"A required question in section {section} was not filled (unsupported type or validation rule).")
            section += 1
        elif sub.count():
            if not submit:
                return
            sub.first.click()
            try:
                page.get_by_text("Your response has been recorded", exact=False).wait_for(timeout=10000)
            except Exception:
                if required_error_visible(page):
                    raise RuntimeError("A required question in the last section was not filled.")
                raise RuntimeError("Submit clicked but no confirmation appeared.")
            return
        else:
            raise RuntimeError("Neither Next nor Submit button found.")


# ---------------- job runner ----------------
def new_job(kind):
    jid = uuid.uuid4().hex[:8]
    JOBS[jid] = {
        "kind": kind, "status": "running", "log": [], "done": 0, "total": 0,
        "failed": 0, "questions": [], "stop": threading.Event(),
    }
    return jid


def worker(jid, url, total, dmin, dmax, config, headless, scan_only):
    job = JOBS[jid]

    def log(msg):
        job["log"].append(f"[{time.strftime('%H:%M:%S')}] {msg}")
        del job["log"][:-300]

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True if HOSTED else headless,
                args=["--no-sandbox"] if HOSTED else [],
            )
            page = browser.new_page()
            page.set_default_timeout(15000)
            try:
                page.goto(url, wait_until="domcontentloaded")

                if scan_only:
                    log("Scanning form...")
                    found = []
                    run_sections(page, {}, log, job["stop"], submit=False, collect=found)
                    job["questions"] = found
                    log(f"Found {len(found)} questions.")
                else:
                    job["total"] = total
                    fails = 0
                    for n in range(1, total + 1):
                        if job["stop"].is_set():
                            raise Stopped()
                        log(f"Response {n}/{total}")
                        try:
                            run_sections(page, config, log, job["stop"], submit=True)
                            job["done"] += 1
                            fails = 0
                            log(f"Response {n} submitted.")
                        except (Stopped, Blocked):
                            raise
                        except Exception as e:
                            fails += 1
                            job["failed"] += 1
                            log(f"Response {n} failed: {e}")
                            if fails >= MAX_CONSECUTIVE_FAILS:
                                raise RuntimeError(f"{fails} failures in a row - stopping.")

                        if n < total:
                            wait_ms = int(random.uniform(dmin, dmax) * 1000)
                            waited = 0
                            while waited < wait_ms:
                                if job["stop"].is_set():
                                    raise Stopped()
                                page.wait_for_timeout(500)
                                waited += 500
                            # fresh blank form
                            try:
                                link = page.get_by_text("Submit another response", exact=False)
                                if link.count():
                                    link.first.click()
                                    page.wait_for_selector('div[role="listitem"]', timeout=10000)
                                else:
                                    raise RuntimeError("no link")
                            except Exception:
                                page.goto(url, wait_until="domcontentloaded")
            finally:
                browser.close()
        job["status"] = "finished"
    except Stopped:
        job["status"] = "stopped"
        log("Stopped by user.")
    except Exception as e:
        job["status"] = "error"
        log(f"ERROR: {e}")
    finally:
        with LOCK:
            ACTIVE["id"] = None


def validate_url(url):
    try:
        u = urlparse(url)
    except Exception:
        return "Invalid URL."
    if u.scheme != "https":
        return "URL must start with https://"
    if u.netloc == "forms.gle":
        return None
    if u.netloc == "docs.google.com" and u.path.startswith("/forms"):
        return None
    return "Only Google Forms links (docs.google.com/forms/... or forms.gle/...) are supported."


def start(kind, **kw):
    with LOCK:
        if ACTIVE["id"] and JOBS[ACTIVE["id"]]["status"] == "running":
            return None, "Another job is still running. Stop it first."
        jid = new_job(kind)
        ACTIVE["id"] = jid
    threading.Thread(target=worker, args=(jid,), kwargs=kw, daemon=True).start()
    return jid, None


# ---------------- API ----------------
@app.post("/api/scan")
def api_scan():
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    err = validate_url(url)
    if err:
        return jsonify(error=err), 400
    jid, err = start("scan", url=url, total=0, dmin=0, dmax=0, config={},
                     headless=bool(d.get("headless", True)), scan_only=True)
    if err:
        return jsonify(error=err), 409
    return jsonify(id=jid)


@app.post("/api/run")
def api_run():
    d = request.get_json(force=True)
    url = (d.get("url") or "").strip()
    err = validate_url(url)
    if err:
        return jsonify(error=err), 400
    if not d.get("confirm"):
        return jsonify(error="Please confirm you own this form or have permission to test it."), 400
    try:
        total = max(1, min(int(d.get("total", 1)), MAX_RESPONSES))
        dmin = max(MIN_DELAY_S, float(d.get("delay_min", 5)))
        dmax = max(dmin, float(d.get("delay_max", 10)))
    except (TypeError, ValueError):
        return jsonify(error="Invalid number in settings."), 400
    jid, err = start("run", url=url, total=total, dmin=dmin, dmax=dmax,
                     config=d.get("config") or {}, headless=bool(d.get("headless", False)),
                     scan_only=False)
    if err:
        return jsonify(error=err), 409
    return jsonify(id=jid, total=total)


@app.get("/api/job/<jid>")
def api_job(jid):
    job = JOBS.get(jid)
    if not job:
        return jsonify(error="Unknown job"), 404
    return jsonify(kind=job["kind"], status=job["status"], log=job["log"][-150:],
                   done=job["done"], total=job["total"], failed=job["failed"],
                   questions=job["questions"])


@app.post("/api/stop/<jid>")
def api_stop(jid):
    job = JOBS.get(jid)
    if job:
        job["stop"].set()
    return jsonify(ok=True)


# ---------------- front end ----------------
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AI Form Assistant</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;background:#f5f6f8;color:#1c1e21}
  h1{font-size:22px} h2{font-size:16px;margin:0 0 10px}
  .box{background:#fff;border:1px solid #dde0e4;border-radius:10px;padding:16px;margin-bottom:14px}
  input[type=text],input[type=number],textarea{width:100%;box-sizing:border-box;padding:8px;border:1px solid #c5c9ce;border-radius:6px;font:inherit}
  .row{display:flex;gap:12px;flex-wrap:wrap;align-items:end}
  .row>div{flex:1;min-width:130px} label{font-size:13px;color:#444;display:block;margin-bottom:4px}
  button{padding:9px 16px;border:0;border-radius:6px;background:#2f6fed;color:#fff;font:inherit;cursor:pointer}
  button.stop{background:#d93025} button:disabled{opacity:.5;cursor:not-allowed}
  .card{border:1px solid #e3e5e8;border-radius:8px;padding:10px;margin-bottom:10px}
  .qt{font-weight:600;margin-bottom:6px} .tag{font-weight:400;color:#777;font-size:12px}
  .opt{display:inline-block;margin:2px 14px 2px 0;font-size:14px}
  .note{color:#777;font-size:13px}
  #log{background:#111;color:#cfe8cf;font:12px/1.5 Consolas,monospace;padding:10px;border-radius:8px;height:200px;overflow:auto;white-space:pre-wrap}
  .bar{height:10px;background:#e3e5e8;border-radius:6px;overflow:hidden;margin:10px 0}
  .bar>div{height:100%;width:0;background:#2f6fed;transition:width .3s}
  .err{color:#d93025;font-size:14px;margin-top:8px}
</style></head><body>
<h1>AI Form Assistant</h1>

<div class="box">
  <h2>1. Form link</h2>
  <input type="text" id="url" placeholder="https://docs.google.com/forms/d/e/.../viewform">
  <p style="margin:10px 0 0"><button id="scanBtn">Scan questions</button>
  <span class="note">Reads every section and lists the questions. Nothing is submitted.</span></p>
  <div class="err" id="scanErr"></div>
</div>

<div class="box" id="qbox" style="display:none">
  <h2>2. Answer options (optional)</h2>
  <p class="note">Untick options you don't want picked. For text questions, add your own answers (one per line); leave empty for auto-generated values.</p>
  <div id="questions"></div>
</div>

<div class="box">
  <h2>3. Run settings</h2>
  <div class="row">
    <div><label>Number of responses (max 200)</label><input type="number" id="total" value="5" min="1" max="200"></div>
    <div><label>Min delay (sec, &ge; 3)</label><input type="number" id="dmin" value="5" min="3"></div>
    <div><label>Max delay (sec)</label><input type="number" id="dmax" value="10" min="3"></div>
  </div>
  <p><label style="display:inline"><input type="checkbox" id="headless"> Run browser hidden (headless)</label></p>
  <p><label style="display:inline"><input type="checkbox" id="confirm"> I own this form or have permission to test it</label></p>
  <button id="runBtn">Start</button>
  <button id="stopBtn" class="stop" disabled>Stop</button>
  <div class="err" id="runErr"></div>
  <div class="bar"><div id="bar"></div></div>
  <div class="note" id="progress"></div>
</div>

<div class="box"><h2>Log</h2><div id="log"></div></div>

<script>
const $ = id => document.getElementById(id);
let currentJob = null, timer = null;

function el(tag, props, kids){
  const e = document.createElement(tag);
  Object.assign(e, props || {});
  (kids || []).forEach(k => e.append(k));
  return e;
}

async function post(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify(body || {})});
  return {ok: r.ok, data: await r.json()};
}

function renderQuestions(qs){
  const box = $("questions"); box.innerHTML = "";
  qs.forEach(q => {
    const card = el("div", {className:"card"});
    card.dataset.title = q.title;
    card.append(el("div", {className:"qt"}, [q.title + " ", el("span", {className:"tag", textContent:"(" + q.type + ", section " + q.section + ")"})]));
    if (q.options && q.options.length){
      q.options.forEach(o => {
        const cb = el("input", {type:"checkbox", checked:true, value:o});
        card.append(el("label", {className:"opt"}, [cb, " " + o]));
      });
    } else if (q.type === "text"){
      card.append(el("textarea", {rows:2, placeholder:"Custom answers, one per line (empty = auto-generate)"}));
    } else {
      card.append(el("div", {className:"note", textContent: q.type === "grid" ? "Grid: a random option is picked per row." : q.type === "date" ? "A random date from the last year is used." : "Not supported - may block the form if required."}));
    }
    box.append(card);
  });
  $("qbox").style.display = qs.length ? "block" : "none";
}

function collectConfig(){
  const cfg = {};
  document.querySelectorAll("#questions .card").forEach(c => {
    const t = c.dataset.title;
    const boxes = c.querySelectorAll("input[type=checkbox]");
    if (boxes.length) cfg[t] = {options: [...boxes].filter(b => b.checked).map(b => b.value)};
    const ta = c.querySelector("textarea");
    if (ta) cfg[t] = {values: ta.value.split("\n").map(s => s.trim()).filter(Boolean)};
  });
  return cfg;
}

function setRunning(on){
  $("scanBtn").disabled = on; $("runBtn").disabled = on; $("stopBtn").disabled = !on;
}

async function poll(){
  if (!currentJob) return;
  const r = await fetch("/api/job/" + currentJob);
  const j = await r.json();
  const log = $("log");
  log.textContent = j.log.join("\n");
  log.scrollTop = log.scrollHeight;
  if (j.kind === "run"){
    const pct = j.total ? Math.round(100 * (j.done + j.failed) / j.total) : 0;
    $("bar").style.width = pct + "%";
    $("progress").textContent = j.done + " submitted, " + j.failed + " failed, of " + j.total;
  }
  if (j.status !== "running"){
    clearInterval(timer); setRunning(false);
    if (j.kind === "scan" && j.status === "finished") renderQuestions(j.questions);
    if (j.status === "error") (j.kind === "scan" ? $("scanErr") : $("runErr")).textContent = "Job failed - see log.";
    currentJob = null;
  }
}

function track(id){
  currentJob = id; setRunning(true);
  clearInterval(timer); timer = setInterval(poll, 1000); poll();
}

$("scanBtn").onclick = async () => {
  $("scanErr").textContent = "";
  const {ok, data} = await post("/api/scan", {url: $("url").value, headless: true});
  if (!ok) { $("scanErr").textContent = data.error; return; }
  track(data.id);
};

$("runBtn").onclick = async () => {
  $("runErr").textContent = ""; $("bar").style.width = "0";
  const {ok, data} = await post("/api/run", {
    url: $("url").value, total: $("total").value, delay_min: $("dmin").value, delay_max: $("dmax").value,
    headless: $("headless").checked, confirm: $("confirm").checked, config: collectConfig()
  });
  if (!ok) { $("runErr").textContent = data.error; return; }
  track(data.id);
};

$("stopBtn").onclick = () => { if (currentJob) post("/api/stop/" + currentJob); };
</script>
</body></html>
"""


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


if __name__ == "__main__":
    if HOSTED:
        app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
    else:
        threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
        app.run(host="127.0.0.1", port=5000, debug=False)
