#!/usr/bin/env python3
"""Run real-Chrome acceptance against a static publication at root and nested paths."""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import shutil
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


ACCEPTANCE_PAGE = r'''<!doctype html>
<html><head><meta charset="utf-8"><link rel="icon" href="data:,"><title>Phase 4C browser acceptance</title><style>
html,body,#stage,iframe{width:100%;height:100%;margin:0;border:0;overflow:hidden}#result{display:none}
</style></head><body><div id="stage"><iframe id="site" title="Published exhibition website"></iframe></div><pre id="result"></pre>
<script>
const params=new URLSearchParams(location.search),prefix=params.get('base')||'',missing=params.get('missing')==='1',requestedRoute=Number(params.get('route')||0);
const result={status:'RUNNING',base:prefix||'/',missingAsset:missing,checks:[],errors:[],consoleErrors:[],unhandledRejections:[],failedRequests:[],routeChecks:[],httpRequests:[]};
function check(name,ok,details={}){result.checks.push({name,ok:Boolean(ok),...details});if(!ok)result.status='FAIL';}
function sleep(ms){return new Promise(resolve=>setTimeout(resolve,ms));}
function finish(){if(result.status==='RUNNING')result.status=result.checks.every(x=>x.ok)?'PASS':'FAIL';document.getElementById('result').textContent=JSON.stringify(result);}
async function run(){
 const frame=document.getElementById('site');frame.src=`${prefix}/`;
 const deadline=Date.now()+210000;let doc=null,hooks=null,errorState=false;
 while(Date.now()<deadline){
   try{doc=frame.contentDocument;hooks=frame.contentWindow.__exhibitionArchiveTestHooks;const status=doc?.getElementById('modelStatus')?.textContent||'';
     if(missing&&status.includes('could not be loaded')){errorState=true;break;}
     if(!missing&&status.startsWith('Ready')&&hooks&&doc.querySelector('#viewerHost canvas'))break;
   }catch{}
   await sleep(100);
 }
 result.loadedUrl=frame.contentWindow.location.href;
 if(missing){
   const status=doc?.getElementById('modelStatus')?.textContent||'';
   check('missing model gives a visible loading error',errorState&&status.includes('(404)'),{message:status});
   const diagnostics=frame.contentWindow.__publicationBrowserDiagnostics||{};
   result.failedRequests=diagnostics.failedRequests||[];result.consoleErrors=diagnostics.consoleErrors||[];result.errors=diagnostics.errors||[];result.unhandledRejections=diagnostics.unhandledRejections||[];
   check('missing model request returns HTTP 404',result.failedRequests.some(x=>x.status===404&&x.url.includes('exhibition.ply')),{failedRequests:result.failedRequests});
   check('missing model error is handled without an unhandled rejection',result.unhandledRejections.length===0,{unhandledRejections:result.unhandledRejections});
   finish();return;
 }
 if(!hooks||!doc?.getElementById('modelStatus')?.textContent.startsWith('Ready')){
   const diagnostics=frame.contentWindow.__publicationBrowserDiagnostics||{};result.status='FAIL';result.errors.push(`The viewer did not reach Ready with a loaded model before the timeout (status: ${doc?.getElementById('modelStatus')?.textContent||'missing'}).`);result.errors.push(...(diagnostics.errors||[]));result.consoleErrors=diagnostics.consoleErrors||[];result.failedRequests=diagnostics.failedRequests||[];result.unhandledRejections=diagnostics.unhandledRejections||[];finish();return;
 }
 const win=frame.contentWindow,healthResponse=await fetch(`${prefix}/api/health.json`),exhibitionResponse=await fetch(`${prefix}/config/exhibition.json`),toursResponse=await fetch(`${prefix}/api/tours.json`);
 const health=await healthResponse.json(),exhibition=await exhibitionResponse.json(),tours=await toursResponse.json();
 const canvas=doc.querySelector('#viewerHost canvas:not(#selectionOverlay)'),canvasInfo=hooks.canvasMetadata();
 check('accepted model identity metadata is loaded',healthResponse.ok&&health.sha256===exhibition.model.sha256&&health.modelBytes===exhibition.model.size_bytes,{sha256:health.sha256,bytes:health.modelBytes,gaussianCount:exhibition.model.gaussian_count});
 check('PlayCanvas creates a rendered WebGL canvas',Boolean(canvas&&canvasInfo.drawingBuffer[0]>0&&canvasInfo.drawingBuffer[1]>0),{renderer:canvasInfo.renderer,drawingBuffer:canvasInfo.drawingBuffer});
 check('viewer uses the accepted navigation policy',hooks.getPolicy()==='architectural-envelope'&&hooks.isValidPosition(hooks.getPose().position),{policy:hooks.getPolicy(),position:hooks.getPose().position});
 const plyUrl=new URL(health.contentUrl,frame.contentWindow.location.href);
 check('model URL resolves beneath the selected hosting prefix',plyUrl.pathname.startsWith(prefix?`${prefix}/`:'/')&&!plyUrl.pathname.includes('//')&&plyUrl.pathname.endsWith('/assets/exhibition.ply'),{url:plyUrl.pathname});

 const nav=hooks.getBoundary(),normal=nav.floor.normal.map(x=>x/Math.hypot(...nav.floor.normal)),offset=nav.floor.offset;
 const height=point=>point.reduce((sum,x,i)=>sum+x*nav.floor.normal[i],offset);
 const initial=hooks.getPose().position;
 const floorTarget=initial.map((x,i)=>x-normal[i]*100),floorResult=hooks.resolveMove(initial,floorTarget);
 check('floor crossing is clamped to the accepted floor clearance',hooks.isValidPosition(floorResult)&&Math.abs(height(floorResult)-nav.floor.clearance)<0.01,{height:height(floorResult),clearance:nav.floor.clearance});
 const ceilingTarget=initial.map((x,i)=>x+normal[i]*100),ceilingResult=hooks.resolveMove(initial,ceilingTarget),ceilingLimit=nav.ceiling.heightAlongFloorNormal-nav.ceiling.clearance;
 check('ceiling crossing is clamped to the accepted ceiling clearance',hooks.isValidPosition(ceilingResult)&&Math.abs(height(ceilingResult)-ceilingLimit)<0.01,{height:height(ceilingResult),limit:ceilingLimit});
 const boundary=nav.horizontalBoundary,poly=boundary.vertices2d,basis=boundary.planeBasis,area=poly.reduce((sum,p,i)=>sum+p[0]*poly[(i+1)%poly.length][1]-poly[(i+1)%poly.length][0]*p[1],0),wallResults=[];
 function at(uv,h){const p=basis.pointOnPlaneCommonViewer.map((x,i)=>x+basis.basisU[i]*uv[0]+basis.basisV[i]*uv[1]),correction=h-(p.reduce((sum,x,i)=>sum+x*nav.floor.normal[i],nav.floor.offset));return p.map((x,i)=>x+nav.floor.normal[i]*correction);}
 const level=height(initial),margin=boundary.cameraClearanceUnits;
 for(let i=0;i<poly.length;i++){
   const a=poly[i],b=poly[(i+1)%poly.length],dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy)||1;
   const outward=area>=0?[dy/len,-dx/len]:[-dy/len,dx/len],mid=[(a[0]+b[0])/2,(a[1]+b[1])/2],outside=[mid[0]+outward[0]*(margin+0.45),mid[1]+outward[1]*(margin+0.45)];
   const start=at([mid[0]-outward[0]*(margin+0.30),mid[1]-outward[1]*(margin+0.30)],level),target=at(outside,level);
   if(!hooks.isValidPosition(start)) continue;
   const resolved=hooks.resolveMove(start,target),block=hooks.getLastBlock();
   wallResults.push({edge:i,ok:!hooks.isValidPosition(target)&&hooks.isValidPosition(resolved)&&block?.reason==='horizontal-enclosure'});
 }
 check('enclosure wall tests stop at the accepted horizontal boundary',wallResults.length>0&&wallResults.every(x=>x.ok),{tested:wallResults.length,edgeCount:poly.length,results:wallResults});

 let routeGeometry=true,routeSegments=0;
 for(const route of tours.routes||[]){
   let prior=null,routeValid=true;
   for(const waypoint of route.waypoints||[]){const valid=hooks.isValidPosition(waypoint.position)&&(!prior||hooks.isSegmentAllowed(prior,waypoint.position));routeValid=routeValid&&valid;routeSegments++;prior=waypoint.position;}
   routeGeometry=routeGeometry&&routeValid;result.routeChecks.push({id:route.id,title:route.title,waypoints:route.waypoints?.length||0,valid:routeValid});
 }
 check('all published guided route waypoints remain inside the enclosure',routeGeometry&&routeSegments>0,{routeCount:tours.routes?.length||0,waypointCount:routeSegments});

 const mode=doc.getElementById('mode'),modeChange=value=>{mode.value=value;mode.dispatchEvent(new win.Event('change',{bubbles:true}));return hooks.getMode()===value;};
 let orbitChange=modeChange('artwork-orbit');
 check('Artwork Orbit mode transition is available',orbitChange,{mode:hooks.getMode()});
 let exploreChange=modeChange('visitor-explore');
 check('Visitor Explore mode transition restores free navigation',exploreChange&&hooks.getPolicy()==='architectural-envelope',{mode:hooks.getMode(),policy:hooks.getPolicy()});

 async function keyFor(key){const target=doc.getElementById('viewerHost'),lower=key.toLowerCase();target.focus({preventScroll:true});target.dispatchEvent(new win.KeyboardEvent('keydown',{key,bubbles:true,cancelable:true}));const captured=hooks.getKeyState().includes(lower);for(let i=0;i<7;i++)hooks.advanceInputForAcceptance(0.05);target.dispatchEvent(new win.KeyboardEvent('keyup',{key,bubbles:true,cancelable:true}));return {captured,released:!hooks.getKeyState().includes(lower)};}
 const beforeVertical=hooks.getPose().position,verticalBefore=height(beforeVertical),upInput=await keyFor('e'),afterUp=hooks.getPose().position,verticalAfter=height(afterUp);
 const downInput=await keyFor('q'),afterDown=hooks.getPose().position,verticalDown=height(afterDown);
 check('visitor controls move camera vertically in both XYZ directions',verticalAfter>verticalBefore+0.05&&verticalDown<verticalAfter-0.05&&upInput.captured&&upInput.released&&downInput.captured&&downInput.released,{before:verticalBefore,up:verticalAfter,down:verticalDown,upInput,downInput});
 const pose=hooks.getPose(),look=pose.target.map((x,i)=>x-pose.position[i]),lookDot=look.reduce((sum,x,i)=>sum+x*normal[i],0),forward=look.map((x,i)=>x-normal[i]*lookDot),flen=Math.hypot(...forward)||1;for(let i=0;i<3;i++)forward[i]/=flen;
 const right=[forward[1]*normal[2]-forward[2]*normal[1],forward[2]*normal[0]-forward[0]*normal[2],forward[0]*normal[1]-forward[1]*normal[0]],directions=[['w',forward],['s',forward.map(x=>-x)],['d',right],['a',right.map(x=>-x)]];
 directions.sort((a,b)=>Math.hypot(...hooks.resolveMove(pose.position,pose.position.map((x,i)=>x+a[1][i]*0.6)).map((x,i)=>x-pose.position[i]))-Math.hypot(...hooks.resolveMove(pose.position,pose.position.map((x,i)=>x+b[1][i]*0.6)).map((x,i)=>x-pose.position[i]))).reverse();
 let horizontalTravel=0,horizontalKey=null;
 for(const [key] of directions){const p0=hooks.getPose().position,input=await keyFor(key),p1=hooks.getPose().position;const delta=p1.map((x,i)=>x-p0[i]);const along=delta.reduce((sum,x,i)=>sum+x*normal[i],0);horizontalTravel=Math.hypot(...delta.map((x,i)=>x-normal[i]*along));if(horizontalTravel>0.05&&input.captured&&input.released){horizontalKey=key;break;}}
 check('visitor controls allow horizontal movement without station snapping',horizontalTravel>0.05,{key:horizontalKey,distance:horizontalTravel});

 if((tours.routes||[]).length){
   const route=tours.routes[Math.max(0,Math.min(tours.routes.length-1,requestedRoute))];modeChange('visitor-explore');const routeSelect=doc.getElementById('tour');routeSelect.value=route.id;doc.getElementById('playTour').click();
   let routeClock=win.performance.now();for(let i=0;i<120&&!doc.getElementById('decision').textContent.includes('Completed ');i++){routeClock+=100;hooks.advanceRouteForAcceptance(routeClock);}
   const completed=doc.getElementById('decision').textContent.includes('Completed '),expected=route.waypoints[route.waypoints.length-1].position,actual=hooks.getPose().position,endpointDistance=Math.hypot(...actual.map((x,i)=>x-expected[i]));
   check('a saved guided route plays through to its accepted viewpoint',completed&&endpointDistance<0.05&&hooks.getMode()==='guided-tour',{title:route.title,decision:doc.getElementById('decision').textContent,pose:actual,expected,endpointDistance});
   const modeTransitions=modeChange('artwork-orbit')&&modeChange('visitor-explore');
   check('guided route exits cleanly through visitor mode transitions',modeTransitions&&hooks.getPolicy()==='architectural-envelope',{mode:hooks.getMode(),policy:hooks.getPolicy()});
   modeChange('guided-tour');
 }
 const diagnostics=frame.contentWindow.__publicationBrowserDiagnostics||{};
 result.errors=diagnostics.errors||[];result.consoleErrors=diagnostics.consoleErrors||[];result.unhandledRejections=diagnostics.unhandledRejections||[];result.failedRequests=diagnostics.failedRequests||[];
 check('viewer has no browser runtime errors',result.errors.length===0&&result.unhandledRejections.length===0,{errors:result.errors,unhandledRejections:result.unhandledRejections});
 check('viewer has no failed HTTP requests',result.failedRequests.length===0,{failedRequests:result.failedRequests});
 check('viewer has no console errors',result.consoleErrors.length===0,{consoleErrors:result.consoleErrors});
 finish();
}
run().catch(error=>{result.status='FAIL';result.errors.push(String(error?.stack||error));finish();});
</script></body></html>'''


DIAGNOSTICS = r'''<script>
window.__publicationBrowserDiagnostics={errors:[],consoleErrors:[],unhandledRejections:[],failedRequests:[]};
window.addEventListener('error',event=>window.__publicationBrowserDiagnostics.errors.push(String(event.message||event.error||'window error')));
window.addEventListener('unhandledrejection',event=>window.__publicationBrowserDiagnostics.unhandledRejections.push(String(event.reason?.stack||event.reason||'unhandled rejection')));
const phase4cOriginalConsoleError=console.error.bind(console);
console.error=(...args)=>{window.__publicationBrowserDiagnostics.consoleErrors.push(args.map(x=>String(x?.stack||x)).join(' '));phase4cOriginalConsoleError(...args);};
const phase4cOriginalFetch=window.fetch.bind(window);
window.fetch=(...args)=>phase4cOriginalFetch(...args).then(response=>{if(!response.ok)window.__publicationBrowserDiagnostics.failedRequests.push({url:response.url,status:response.status});return response;});
</script>'''


class AcceptanceServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class StaticHandler(BaseHTTPRequestHandler):
    website: Path
    prefix: str
    missing_asset: bool
    log: list[dict]
    lock: threading.Lock

    def log_message(self, _format: str, *args) -> None:
        return

    def _record(self, path: str, status: int, byte_count: int, range_header: str | None) -> None:
        with self.lock:
            self.log.append({"path": path, "status": status, "bytes": byte_count, "range": range_header})

    def _resolve(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        if path == "/_phase4c_acceptance.html":
            return None, path, parse_qs(parsed.query), True
        if self.prefix:
            if path == self.prefix:
                path = "/"
            elif path.startswith(self.prefix + "/"):
                path = path[len(self.prefix):]
            else:
                return None, path, {}, False
        relative = path.lstrip("/")
        target = (self.website / relative).resolve()
        try:
            target.relative_to(self.website.resolve())
        except ValueError:
            return None, path, {}, False
        if target.is_dir():
            target = target / "index.html"
        return target, path, {}, True

    def do_GET(self) -> None:
        requested_path = urlsplit(self.path).path
        target, path, query, allowed = self._resolve()
        if not allowed:
            self._record(path, 404, 0, self.headers.get("Range"))
            self.send_error(404)
            return
        if target is None:
            body = ACCEPTANCE_PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self._record(path, 200, len(body), self.headers.get("Range"))
            return
        if self.missing_asset and path.endswith("/assets/exhibition.ply"):
            self._record(path, 404, 0, self.headers.get("Range"))
            self.send_error(404, "Simulated missing model asset for browser acceptance")
            return
        if not target.is_file():
            self._record(path, 404, 0, self.headers.get("Range"))
            self.send_error(404)
            return
        body_override = None
        if target == self.website / "viewer" / "index.html":
            body_override = target.read_bytes().replace(b"<head>", b"<head>" + DIAGNOSTICS.encode("utf-8"), 1)
        size = len(body_override) if body_override is not None else target.stat().st_size
        range_header = self.headers.get("Range")
        start, end, status = 0, size - 1, 200
        if range_header:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", range_header.strip())
            if match:
                start = int(match.group(1))
                end = min(size - 1, int(match.group(2)) if match.group(2) else size - 1)
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    self._record(path, 416, 0, range_header)
                    return
                status = 206
        count = end - start + 1
        mime, _ = mimetypes.guess_type(str(target))
        if target.suffix == ".ply":
            mime = "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", f"{mime or 'application/octet-stream'}; charset=utf-8" if mime and mime.startswith("text/") else (mime or "application/octet-stream"))
        self.send_header("Content-Length", str(count))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        sent = 0
        if body_override is not None:
            payload = memoryview(body_override)[start:end + 1]
            self.wfile.write(payload)
            sent = len(payload)
        else:
            with target.open("rb") as handle:
                handle.seek(start)
                remaining = count
                while remaining:
                    chunk = handle.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    sent += len(chunk)
                    remaining -= len(chunk)
        self._record(path, status, sent, range_header)

    def do_HEAD(self) -> None:
        self.do_GET()


def chrome_binary(value: str | None) -> str:
    candidate = value or shutil.which("google-chrome") or shutil.which("chromium") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not Path(candidate).is_file():
        raise SystemExit(f"Chrome executable not found: {candidate}")
    return candidate


def run_chrome(chrome: str, url: str, profile: Path, screenshot: Path | None, timeout: int) -> tuple[str, str, int]:
    command = [
        chrome, "--headless=new", "--no-sandbox", "--disable-dev-shm-usage", "--enable-webgl",
        "--enable-unsafe-swiftshader", "--use-gl=angle", "--use-angle=swiftshader",
        "--hide-scrollbars", "--window-size=1600,1000", "--virtual-time-budget=600000",
        f"--user-data-dir={profile}", "--dump-dom",
    ]
    if screenshot:
        command.append(f"--screenshot={screenshot}")
    command.append(url)
    completed = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout)
    return completed.stdout, completed.stderr, completed.returncode


def result_from_dom(dom: str) -> dict | None:
    match = re.search(r'<pre id="result">([\s\S]*?)</pre>', dom)
    if not match:
        return None
    try:
        return json.loads(html.unescape(match.group(1)))
    except json.JSONDecodeError:
        return None


def run_case(server: AcceptanceServer, chrome: str, output: Path, *, prefix: str, missing: bool, timeout: int) -> dict:
    handler = server.RequestHandlerClass
    handler.prefix = prefix
    handler.missing_asset = missing
    handler.log = []
    name = "missing-model" if missing else ("nested-prefix" if prefix else "root-prefix")
    screenshot = None if missing else output / f"{name}.png"
    route_index = 0 if not prefix else 4
    query = f"base={prefix}&missing={'1' if missing else '0'}&route={route_index}"
    url = f"http://127.0.0.1:{server.server_port}/_phase4c_acceptance.html?{query}"
    with tempfile.TemporaryDirectory(prefix=f"phase4c-chrome-{name}-") as profile:
        dom, browser_log, exit_code = run_chrome(chrome, url, Path(profile), screenshot, timeout)
    result = result_from_dom(dom)
    records = list(handler.log)
    http_errors = [row for row in records if row["status"] >= 400 and not (missing and row["status"] == 404 and row["path"].endswith("/assets/exhibition.ply"))]
    served_models = [row for row in records if row["path"].endswith("/assets/exhibition.ply")]
    outcome = {
        "case": name,
        "chrome_exit_code": exit_code,
        "status": result.get("status", "FAIL") if result else "FAIL",
        "checks": result.get("checks", []) if result else [],
        "route_checks": result.get("routeChecks", []) if result else [],
        "errors": result.get("errors", []) if result else ["Chrome did not return a parseable acceptance result."],
        "console_errors": result.get("consoleErrors", []) if result else [],
        "unhandled_rejections": result.get("unhandledRejections", []) if result else [],
        "failed_requests": result.get("failedRequests", []) if result else [],
        "http_error_count": len(http_errors),
        "http_errors": http_errors[:20],
        "model_requests": served_models,
        "screenshot": str(screenshot) if screenshot and screenshot.is_file() else None,
        "browser_log_tail": browser_log[-4000:],
    }
    if exit_code != 0:
        outcome["status"] = "FAIL"
        outcome["errors"].append(f"Chrome exited with code {exit_code}.")
    if http_errors:
        outcome["status"] = "FAIL"
    if not missing and not served_models:
        outcome["status"] = "FAIL"
        outcome["errors"].append("No request for the packaged PLY was served.")
    if screenshot and not outcome["screenshot"]:
        outcome["status"] = "FAIL"
        outcome["errors"].append("Chrome did not write the requested screenshot.")
    return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--website", type=Path, required=True, help="built static website directory")
    parser.add_argument("--output", type=Path, required=True, help="directory for browser evidence JSON and screenshots")
    parser.add_argument("--chrome", help="Chrome executable path; autodetected by default")
    parser.add_argument("--timeout", type=int, default=240, help="maximum seconds for each browser process")
    args = parser.parse_args()
    website = args.website.resolve()
    output = args.output.resolve()
    if not (website / "index.html").is_file():
        raise SystemExit(f"Static website entrypoint is missing: {website / 'index.html'}")
    output.mkdir(parents=True, exist_ok=True)
    index_path = website / "config" / "package-index.json"
    if not index_path.is_file():
        raise SystemExit("Website package index is missing; build the static package first.")
    binary = chrome_binary(args.chrome)
    static_handler = type("Phase4CHandler", (StaticHandler,), {
        "website": website,
        "prefix": "",
        "missing_asset": False,
        "log": [],
        "lock": threading.Lock(),
    })
    server = AcceptanceServer(("127.0.0.1", 0), static_handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    try:
        cases = [
            run_case(server, binary, output, prefix="", missing=False, timeout=args.timeout),
            run_case(server, binary, output, prefix="/nested", missing=False, timeout=args.timeout),
            run_case(server, binary, output, prefix="", missing=True, timeout=args.timeout),
        ]
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=3)
    report = {
        "schema_version": "exhibition-archive.browser-acceptance.v1",
        "status": "PASS" if all(case["status"] == "PASS" for case in cases) else "FAIL",
        "website": str(website),
        "chrome": binary,
        "cases": cases,
        "static_host": "Python standard-library HTTP server bound to 127.0.0.1; nested case maps /nested/ to the unchanged package root",
        "expected_missing_asset": "The missing-model case intentionally returns HTTP 404 for assets/exhibition.ply and validates the viewer's visible error state.",
    }
    report_path = output / "browser-acceptance.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "report": str(report_path), "cases": [{"case": case["case"], "status": case["status"], "screenshot": case["screenshot"]} for case in cases]}, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
