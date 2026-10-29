from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

from .adapters import public_json, read_json, sha256_file, write_json
from .workflow import output_path, project_path


PRODUCT_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = PRODUCT_ROOT.parent


def _path(root: Path, config: dict, group: str, key: str) -> Path:
    raw = config["integrations"][group][key]
    p = Path(raw).expanduser()
    return (p if p.is_absolute() else root / p).resolve()


def _subtree(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        return {k: _subtree(v, keys) for k, v in value.items() if k in keys}
    if isinstance(value, list):
        return [_subtree(x, keys) for x in value]
    return value


def _write_json(out: Path, name: str, value: Any) -> None:
    write_json(out / "api" / f"{name}.json", public_json(value))


def _static_api(root: Path, config: dict, website: Path, model_record: dict, state) -> dict[str, Any]:
    nav_cfg = config["integrations"]["navigation"]
    navigation_artifact = None
    generated_navigation = nav_cfg.get("mode") == "generate_from_sfm"
    if generated_navigation:
        approved_path = output_path(root, config) / "work" / "navigation-approved.json"
        navigation_stage = state.stage("navigation")
        if not approved_path.is_file() or not navigation_stage or navigation_stage.get("status") != "ADOPTED":
            raise ValueError("Public viewer packaging is blocked: generated navigation has no explicitly approved artifact")
        navigation_artifact = read_json(approved_path)
        if navigation_artifact.get("status") != "NAVIGATION_APPROVED_BY_REVIEW" or navigation_artifact.get("provenance", {}).get("navigation_input_fingerprint") != navigation_stage.get("input_fingerprint"):
            raise ValueError("Public viewer packaging is blocked: approved navigation is stale or not review-approved")
        compatibility = navigation_artifact.get("viewer_compatibility") or {}
        envelope = compatibility.get("envelope")
        policy = compatibility.get("runtime_policy")
        if not envelope or not policy or not navigation_artifact.get("validation", {}).get("public_ready"):
            raise ValueError("Public viewer packaging is blocked: approved navigation artifact is incomplete")
    else:
        envelope = read_json(_path(root, config, "navigation", "envelope"))
        policy = read_json(_path(root, config, "navigation", "runtime_policy"))
    baseline = Path(model_record["path"])
    expected = model_record["sha256"]
    health = {"ok": True, "modelBytes": baseline.stat().st_size, "sha256": expected, "expectedSha256": expected,
              "exactBaselineMatch": model_record.get("selection") == "accepted baseline", "modelSelection": model_record.get("selection"),
              "contentUrl": "../assets/exhibition.ply", "boundaryId": envelope.get("id"),
              "boundarySource": envelope.get("horizontalBoundary", {}).get("classification"), "legacyActiveWallCount": 0,
              "indexedSourceImages": 0, "projectId": config["project_id"], "publication": "STATIC; no source photographs or private review endpoints"}
    _write_json(website, "health", health)
    _write_json(website, "boundary", envelope)
    _write_json(website, "boundary-generated", envelope)

    if generated_navigation:
        compatibility = navigation_artifact["viewer_compatibility"]
        stations = compatibility.get("stations", {"stations": []})
        centers = compatibility.get("camera_centers", {"stations": []})
        tours = compatibility.get("guided_tours", {"routes": []})
        candidate_registry = {"candidates": []}
        footprints = {"candidates": [{"id": region["id"], "algorithm": region["algorithm"], "marginUnits": 0.0,
                                      "areaUnits2": abs(sum(region["polygon_uv"][i][0] * region["polygon_uv"][(i+1) % len(region["polygon_uv"])][1] - region["polygon_uv"][(i+1) % len(region["polygon_uv"])][0] * region["polygon_uv"][i][1] for i in range(len(region["polygon_uv"]))) / 2),
                                      "trustedStationInclusion": {"insideOrOn": region["evidence"]["trusted_station_count"], "total": region["evidence"]["trusted_station_count"]},
                                      "polygon2d": region["polygon_uv"]} for region in navigation_artifact.get("regions", [])]}
        distribution = {"threeNearestNeighborEdges": []}
        transform = {"transform": navigation_artifact.get("coordinate_frame", {})}
        hang = {}
    else:
        stations = read_json(_path(root, config, "navigation", "stations"))
        centers = read_json(_path(root, config, "navigation", "camera_centers"))
        tours = read_json(_path(root, config, "navigation", "guided_tours"))
        candidate_registry = read_json(_path(root, config, "navigation", "candidate_registry"))
        footprints = read_json(_path(root, config, "navigation", "footprints"))
        distribution = read_json(_path(root, config, "navigation", "distribution"))
        transform = read_json(_path(root, config, "navigation", "transform"))
        hang = read_json(_path(root, config, "navigation", "hanging_text_geometry"))
    artwork_config = config.get("integrations", {}).get("artwork", {})
    confirmed_path = project_path(root, config, artwork_config["confirmed_artworks"]) if artwork_config.get("confirmed_artworks") else output_path(root, config) / "work" / "confirmed-artworks.json"
    confirmed = read_json(confirmed_path)

    # The accepted Phase 2ER app requires separate station records for safe
    # spawn/view direction and camera-center records for its map. Only those
    # fields are exported; source paths and investigative notes stay private.
    public_station_keys = ("stationId", "centerCommonViewer", "representativeForwardCommonViewer", "normalVisitorPositionCandidate", "regionId")
    public_stations = [{key: station[key] for key in public_station_keys if key in station} for station in stations.get("stations", [])]
    public_camera_stations = [{key: station[key] for key in ("stationId", "planeCoordinates") if key in station} for station in centers.get("stations", [])]
    _write_json(website, "stations", {"schemaVersion": 1, "defaultStationId": nav_cfg.get("default_station_id") or stations.get("defaultStationId"), "stations": public_stations})
    _write_json(website, "cameras", {"schemaVersion": 1, "principalImageKeys": [], "stations": public_camera_stations})
    _write_json(website, "tours", {"schemaVersion": 1, "routes": tours.get("routes", [])})
    candidate_ids = config.get("viewer", {}).get("public_geometry_view_ids")
    selected_candidates = [x for x in candidate_registry.get("candidates", []) if candidate_ids and x.get("candidateId") in candidate_ids]
    candidate_map = {item["candidateId"]: f"geometry-view-{index + 1:02d}" for index, item in enumerate(selected_candidates)}
    public_candidate_keys = ("commonCenter", "commonHalfExtents", "commonRotationRows", "preferredStationId")
    candidates = []
    for item in selected_candidates:
        candidate = {key: item[key] for key in public_candidate_keys if key in item}
        candidate["candidateId"] = candidate_map[item["candidateId"]]
        candidate["publicLabel"] = f"Geometry view {len(candidates) + 1}"
        candidates.append(candidate)
    default_candidate = config.get("viewer", {}).get("default_geometry_view_id")
    _write_json(website, "candidates", {"schemaVersion": 1, "defaultCandidateId": candidate_map.get(default_candidate, candidates[0]["candidateId"] if candidates else None), "candidates": candidates})
    # Map layers are reduced to values directly consumed by the read-only map.
    footprint_public = {"schemaVersion": 1, "candidates": [{k: x.get(k) for k in ("id", "algorithm", "marginUnits", "areaUnits2", "trustedStationInclusion", "polygon2d")} for x in footprints.get("candidates", [])], "concaveAlphaSweep": []}
    _write_json(website, "footprints", footprint_public)
    _write_json(website, "distribution", {"schemaVersion": 1, "threeNearestNeighborEdges": distribution.get("threeNearestNeighborEdges", [])})
    _write_json(website, "transform", transform.get("transform", {}))
    _write_json(website, "artifact-evidence", {"schemaVersion": 1, "status": "GEOMETRY_VIEWPOINT_ONLY; no artwork identity or semantic label is published", "candidate": candidates[0] if candidates else {}})
    _write_json(website, "ghost-annotations", {"schemaVersion": 1, "annotations": []})

    config_file = {"schema_version": "exhibition-archive.public-viewer.v1", "project_id": config["project_id"],
                   "title": config.get("viewer", {}).get("title", config["project_id"]), "theme": config.get("viewer", {}).get("theme", "dark"),
                   "model": {"url": "../assets/exhibition.ply", "sha256": expected, "size_bytes": baseline.stat().st_size,
                             "gaussian_count": model_record.get("gaussian_count")},
                   "artwork_catalogue_status": confirmed.get("count", len(confirmed.get("artworks", []))),
                   "public_catalogue": "Only reviewer-confirmed artwork records are listed in config/artworks.json.",
                   "navigation_policy": "Explicitly reviewed camera-pose navigation artifact; scene units are nonmetric unless calibrated." if generated_navigation else "Imported accepted camera-pose enclosure; navigation geometry is nonmetric and not surveyed."}
    write_json(website / "config" / "exhibition.json", config_file)
    if generated_navigation:
        public_policy = {key: value for key, value in policy.items() if key != "review"}
        public_navigation = {"schema_version": navigation_artifact["schema_version"], "artifact_version": navigation_artifact["artifact_version"],
                             "id": navigation_artifact["id"], "status": navigation_artifact["status"],
                             "coordinate_frame": navigation_artifact["coordinate_frame"],
                             "regions": [{"id": region["id"], "polygon_uv": region["polygon_uv"], "algorithm": region["algorithm"],
                                          "classification": region["classification"], "vertical_bounds": region["vertical_bounds"]}
                                         for region in navigation_artifact.get("regions", [])],
                             "transitions": [{"from_region": item["from_region"], "to_region": item["to_region"],
                                              "portal": {"segment_uv": item["portal"]["segment_uv"]},
                                              "approved": bool(item.get("approved")),
                                              "classification": item.get("classification", "REVIEWED_PORTAL")}
                                             for item in navigation_artifact.get("transitions", [])],
                             "camera_clearance": navigation_artifact.get("camera_clearance"),
                             "viewer_compatibility": {"envelope": envelope, "runtime_policy": public_policy},
                             "validation": {"status": navigation_artifact["validation"].get("status"), "public_ready": True}}
        write_json(website / "config" / "navigation.json", public_json(public_navigation))
    else:
        write_json(website / "config" / "navigation.json", public_json({"envelope": envelope, "runtime_policy": policy}))
    write_json(website / "config" / "artworks.json", public_json(confirmed))
    return {"health": health, "public_artwork_count": config_file["artwork_catalogue_status"], "static_api_count": 11}


def _relativeize_app(text: str) -> str:
    text = text.replace("'playTour','artwork'", "'playTour','pauseTour','resumeTour','nextTourStop','artwork'")
    text = text.replace("let frameId = 0, lastFrameAt = 0, tourRaf = 0;", "let frameId = 0, lastFrameAt = 0, tourRaf = 0, tourPlayback = null;")
    text = text.replace("import { createViewer } from '/vendor/index.js';", "import { createViewer } from './vendor/index.js';")
    text = text.replace("'/api/health'", "'../api/health.json'")
    text = text.replace("'/api/boundary'", "'../api/boundary.json'")
    text = text.replace("'/api/boundary/generated'", "'../api/boundary-generated.json'")
    for name in ("stations", "tours", "candidates", "artifact-evidence", "cameras", "footprints", "distribution", "ghost-annotations", "transform"):
        text = text.replace(f"'/api/{name}'", f"'../api/{name}.json'")
    text = text.replace("'/assets/spirula20k.ply'", "'../assets/exhibition.ply'")
    # Public archive visitors have no unrestricted developer flight control.
    text = text.replace('<option value="debug">Debug Fly · unrestricted developer mode</option>', "")
    # Keep the accepted Phase 2ER implementation intact on disk, while routing
    # generated multi-region artifacts through a region/portal-aware swept test.
    start = text.index("function validPosition(p, margin=")
    end = text.index("\nfunction clampVertical(point)", start)
    generic_policy = r'''function navigationRegions() {
  const rows=boundary.horizontalBoundary.regions??[];
  return rows.length?rows.map(x=>({id:x.regionId??x.id,vertices:x.vertices2d,floor:x.floor,ceiling:x.ceiling} )):
    [{id:'region-default',vertices:boundary.horizontalBoundary.vertices2d,floor:boundary.floor,ceiling:{value:boundary.ceiling.heightAlongFloorNormal,clearance:boundary.ceiling.clearance}}];
}
function regionAt(point) {const xy=horizontalCoordinates(point);return navigationRegions().find(r=>pointInPolygon(xy,r.vertices))??null;}
function regionHeights(region) {return {low:Number(region.floor?.value??0)+Number(region.floor?.clearance??boundary.floor.clearance),high:Number(region.ceiling?.value??boundary.ceiling.heightAlongFloorNormal)-Number(region.ceiling?.clearance??boundary.ceiling.clearance)}}
function validPosition(p, margin=boundary.horizontalBoundary.cameraClearanceUnits) {
  if(!p||p.length!==3||!p.every(Number.isFinite))return false;
  const region=regionAt(p);if(!region)return false;
  const h=floorDistance(p),bounds=regionHeights(region);if(h<bounds.low-1e-6||h>bounds.high+1e-6)return false;
  const xy=horizontalCoordinates(p);return nearestBoundaryEdge(xy,region.vertices).distance>=margin-1e-6;
}
function geometryTolerance(...points){let scale=1;for(const p of points)for(const v of p.slice(0,2))scale=Math.max(scale,Math.abs(v));return Math.max(1e-10,scale*1e-12)}
function orient2(a,b,c){return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])}
function segmentParams(a,b,c,d){
  const tol=geometryTolerance(a,b,c,d),rx=b[0]-a[0],ry=b[1]-a[1],sx=d[0]-c[0],sy=d[1]-c[1],rl=Math.hypot(rx,ry),sl=Math.hypot(sx,sy),den=rx*sy-ry*sx,qx=c[0]-a[0],qy=c[1]-a[1],at=tol*Math.max(1,rl,sl);
  if(rl<=tol)return pointSegmentDistance(a,c,d)<=tol?[0]:[];
  if(Math.abs(den)>at){const t=(qx*sy-qy*sx)/den,u=(qx*ry-qy*rx)/den,pt=tol/Math.max(rl,1);return t>=-pt&&t<=1+pt&&u>=-pt&&u<=1+pt?[Math.max(0,Math.min(1,t))]:[]}
  if(Math.abs(qx*ry-qy*rx)>at)return [];
  const rr=rx*rx+ry*ry,t0=(qx*rx+qy*ry)/rr,t1=((d[0]-a[0])*rx+(d[1]-a[1])*ry)/rr,lo=Math.max(0,Math.min(t0,t1)),hi=Math.min(1,Math.max(t0,t1)),pt=tol/Math.max(rl,1);
  if(hi<lo-pt)return [];if(hi<=lo+pt)return [Math.max(0,Math.min(1,(lo+hi)*.5))];return [Math.max(0,Math.min(1,lo)),Math.max(0,Math.min(1,hi))]
}
function segmentParam(a,b,c,d){return segmentParams(a,b,c,d)[0]??null}
function segmentDistance2(a,b,c,d){if(segmentParams(a,b,c,d).length)return 0;return Math.min(pointSegmentDistance(a,c,d),pointSegmentDistance(b,c,d),pointSegmentDistance(c,a,b),pointSegmentDistance(d,a,b))}
function portalOnBoundary(portal,region,tol){const [p,q]=portal,dx=q[0]-p[0],dy=q[1]-p[1],len=Math.hypot(dx,dy);if(len<=tol)return false;const den=len*len,intervals=[];for(let i=0;i<region.vertices.length;i++){const a=region.vertices[i],b=region.vertices[(i+1)%region.vertices.length];if(Math.max(Math.abs(orient2(p,q,a)),Math.abs(orient2(p,q,b)))>tol*Math.max(1,len))continue;const x=((a[0]-p[0])*dx+(a[1]-p[1])*dy)/den,y=((b[0]-p[0])*dx+(b[1]-p[1])*dy)/den,lo=Math.max(0,Math.min(x,y)),hi=Math.min(1,Math.max(x,y));if(hi>=lo)intervals.push([lo,hi])}intervals.sort((a,b)=>a[0]-b[0]);let covered=0;for(const [lo,hi] of intervals){if(lo>covered+tol/Math.max(len,1))return false;covered=Math.max(covered,hi);if(covered>=1-tol/Math.max(len,1))return true}return false}
function validPortalTransition(tr){const portal=tr?.portal?.segment_uv;if(!tr?.approved||!Array.isArray(portal)||portal.length!==2||portal.some(p=>!Array.isArray(p)||p.length!==2||!p.every(Number.isFinite)))return false;const regions=navigationRegions(),from=regions.find(r=>r.id===tr.from_region),to=regions.find(r=>r.id===tr.to_region);if(!from||!to||from.id===to.id)return false;const tol=geometryTolerance(...portal);return portalOnBoundary(portal,from,tol)&&portalOnBoundary(portal,to,tol)}
function edgeRemainders(a,b,portals,tol){const dx=b[0]-a[0],dy=b[1]-a[1],len=Math.hypot(dx,dy),den=dx*dx+dy*dy;if(len<=tol)return [[a,b]];const cuts=[];for(const [p,q] of portals){if(Math.max(Math.abs(orient2(a,b,p)),Math.abs(orient2(a,b,q)))>tol*Math.max(1,len))continue;const x=((p[0]-a[0])*dx+(p[1]-a[1])*dy)/den,y=((q[0]-a[0])*dx+(q[1]-a[1])*dy)/den,lo=Math.max(0,Math.min(x,y)),hi=Math.min(1,Math.max(x,y));if(hi>=lo)cuts.push([lo,hi])}cuts.sort((x,y)=>x[0]-y[0]);const merged=[];for(const [lo,hi] of cuts){const last=merged.at(-1);if(last&&lo<=last[1]+tol/Math.max(len,1))last[1]=Math.max(last[1],hi);else merged.push([lo,hi])}const out=[];let cursor=0;for(const [lo,hi] of merged){if(lo>cursor)out.push([[a[0]+dx*cursor,a[1]+dy*cursor],[a[0]+dx*lo,a[1]+dy*lo]]);cursor=Math.max(cursor,hi)}if(cursor<1)out.push([[a[0]+dx*cursor,a[1]+dy*cursor],b]);return out}
function segmentAllowed(from,to) {
  if(!from||!to||from.length!==3||to.length!==3||![...from,...to].every(Number.isFinite)){lastBlock={reason:'invalid-pose',edgeIndex:null};return false;}
  const a=horizontalCoordinates(from),b=horizontalCoordinates(to),regions=navigationRegions(),transitions=boundary.horizontalBoundary.transitions??[],cuts=new Set([0,1]);
  for(const r of regions)for(let i=0;i<r.vertices.length;i++)for(const t of segmentParams(a,b,r.vertices[i],r.vertices[(i+1)%r.vertices.length]))cuts.add(t)
  for(const tr of transitions)if(tr.portal?.segment_uv)for(const t of segmentParams(a,b,tr.portal.segment_uv[0],tr.portal.segment_uv[1]))cuts.add(t)
  const ordered=[...cuts].sort((x,y)=>x-y),samples=new Set(ordered);for(let i=0;i<ordered.length-1;i++)samples.add((ordered[i]+ordered[i+1])/2);
  let previous=null,sequence=[];
  for(const t of [...samples].sort((x,y)=>x-y)){
    const p=vAdd(from,vScale(vSub(to,from),t)),xy=horizontalCoordinates(p),region=regions.find(r=>pointInPolygon(xy,r.vertices));
    if(!region){lastBlock={reason:'horizontal-enclosure',attempted:[...to],accepted:[...from],edgeIndex:null};return false;}
    const h=floorDistance(p),bounds=regionHeights(region);if(h<bounds.low-1e-6||h>bounds.high+1e-6){lastBlock={reason:h<bounds.low?'floor':'ceiling',attempted:[...to],accepted:[...from],edgeIndex:null};return false;}
    const nearPortal=transitions.some(x=>validPortalTransition(x)&&[x.from_region,x.to_region].includes(region.id)&&segmentParam(a,b,x.portal.segment_uv[0],x.portal.segment_uv[1])!==null&&pointSegmentDistance(xy,x.portal.segment_uv[0],x.portal.segment_uv[1])<=boundary.horizontalBoundary.cameraClearanceUnits+1e-6);
    if(!nearPortal&&nearestBoundaryEdge(xy,region.vertices).distance<boundary.horizontalBoundary.cameraClearanceUnits-1e-6){lastBlock={reason:'horizontal-enclosure',attempted:[...to],accepted:[...from],edgeIndex:nearestBoundaryEdge(xy,region.vertices).edgeIndex};return false;}
    if(!sequence.length||sequence[sequence.length-1]!==region.id)sequence.push(region.id);
    previous=region.id;
  }
  for(let i=0;i<sequence.length-1;i++){
    const first=sequence[i],second=sequence[i+1],transition=transitions.find(x=>validPortalTransition(x)&&new Set([x.from_region,x.to_region]).has(first)&&new Set([x.from_region,x.to_region]).has(second)&&x.from_region!==x.to_region);
    if(!transition){lastBlock={reason:'region-transition',attempted:[...to],accepted:[...from],edgeIndex:null};return false;}
    const portal=transition.portal?.segment_uv;if(!portal||segmentParam(a,b,portal[0],portal[1])===null){lastBlock={reason:'region-transition-portal',attempted:[...to],accepted:[...from],edgeIndex:null};return false;}
  }
  const clearance=Number(boundary.horizontalBoundary.cameraClearanceUnits??0),tol=geometryTolerance(a,b);
  for(let i=0;i<ordered.length-1;i++){
    const lo=ordered[i],hi=ordered[i+1];if(hi-lo<=1e-14)continue;
    const mid=(lo+hi)/2,midPose=vAdd(from,vScale(vSub(to,from),mid)),xy=horizontalCoordinates(midPose),region=regions.find(r=>pointInPolygon(xy,r.vertices));if(!region)return false;
    const p0=vAdd(from,vScale(vSub(to,from),lo)),p1=vAdd(from,vScale(vSub(to,from),hi)),bounds=regionHeights(region),h0=floorDistance(p0),h1=floorDistance(p1);
    if(!Number.isFinite(bounds.low)||!Number.isFinite(bounds.high)||bounds.high<bounds.low||Math.min(h0,h1)<bounds.low-tol||Math.max(h0,h1)>bounds.high+tol){lastBlock={reason:Math.min(h0,h1)<bounds.low?'floor':'ceiling',attempted:[...to],accepted:[...from],edgeIndex:null};return false;}
    if(clearance>0){const pa=horizontalCoordinates(p0),pb=horizontalCoordinates(p1),openPortals=transitions.filter(x=>validPortalTransition(x)&&[x.from_region,x.to_region].includes(region.id)).map(x=>x.portal.segment_uv);
      for(let edge=0;edge<region.vertices.length;edge++){const ea=region.vertices[edge],eb=region.vertices[(edge+1)%region.vertices.length];for(const [ra,rb] of edgeRemainders(ea,eb,openPortals,tol))if(segmentDistance2(pa,pb,ra,rb)<clearance-Math.max(tol,1e-8)){lastBlock={reason:'horizontal-enclosure',attempted:[...to],accepted:[...from],edgeIndex:edge};return false;}}
    }
  }
  return true;
}'''
    text = text[:start] + generic_policy + text[end:]
    old_clamp = """function clampVertical(point) {
  const n=boundary.floor.normal, h=floorDistance(point);
  const low=boundary.floor.clearance, high=boundary.ceiling.heightAlongFloorNormal-boundary.ceiling.clearance;
  if (h<low) return vAdd(point,vScale(n,low-h));
  if (h>high) return vAdd(point,vScale(n,high-h));
  return point;
}"""
    new_clamp = """function clampVertical(point) {
  const region=regionAt(point)??navigationRegions()[0],n=boundary.floor.normal,h=floorDistance(point),bounds=regionHeights(region);
  if(h<bounds.low)return vAdd(point,vScale(n,bounds.low-h));
  if(h>bounds.high)return vAdd(point,vScale(n,bounds.high-h));
  return point;
}"""
    text = text.replace(old_clamp, new_clamp)
    needle = "  const xy=horizontalCoordinates(hit),poly=boundary.horizontalBoundary.vertices2d,edge=nearestBoundaryEdge(xy,poly).edgeIndex;"
    text = text.replace(needle, "  if(navigationRegions().length>1)return hit;\n" + needle)
    # Public websites never serve the private original photo collection. Keep
    # the accepted diagnostic source-preview element informative without fetch.
    start = text.index("function sourcePhotoFor(scene) {")
    end = text.index("\nasync function setDiagnosticPreset", start)
    text = text[:start] + "function sourcePhotoFor(scene) { el.sourcePhoto.textContent='Original photographs are retained in the restricted archive source set; this public viewer does not expose them.'; }" + text[end:]
    start = text.index("function spawnPose() {")
    end = text.index("\nfunction goToStation", start)
    text = text[:start] + "function spawnPose() { const preferred=stationsDoc.stations.find(s=>s.stationId===stationsDoc.defaultStationId)??stationsDoc.stations.find(s=>s.normalVisitorPositionCandidate)??stationsDoc.stations[0]; return stationPose(preferred); }" + text[end:]
    start = text.index("function sceneSpec(scene) {")
    end = text.index("\nfunction rotateAround", start)
    generic_scene = """function sceneSpec(scene) {
  const c=getCandidate(scene)??candidateDoc.candidates.find(x=>x.candidateId===candidateDoc.defaultCandidateId)??candidateDoc.candidates[0];
  if(!c) return null;
  const station=stationsDoc.stations.find(s=>s.stationId===c.preferredStationId);
  const front=station?normalize(vSub(station.centerCommonViewer,c.commonCenter)):normalize(vScale(c.commonRotationRows?.[2]??[0,0,1],-1));
  return {id:c.candidateId,center:c.commonCenter,front,halfExtents:c.commonHalfExtents,axes:c.commonRotationRows};
}"""
    text = text[:start] + generic_scene + text[end:]
    start = text.index("async function saveBoundaryConfig() {")
    end = text.index("\nfunction resetAutomaticBoundary()", start)
    text = text[:start] + "async function saveBoundaryConfig() { throw new Error('Published archive navigation is read-only.'); }" + text[end:]
    start = text.index("function resetAutomaticBoundary() {")
    end = text.index("\nfunction ", start + 10)
    text = text[:start] + "function resetAutomaticBoundary() { return Promise.reject(new Error('Published archive navigation is read-only.')); }" + text[end:]
    start = text.index("function populateControls() {")
    end = text.index("\nfunction ", start + 10)
    generic_controls = """function populateControls() {
  const stations=stationsDoc.stations;
  for(const station of stations){const option=document.createElement('option');option.value=station.stationId;option.textContent=`Trusted camera station · ${station.stationId}`;el.station.append(option);}
  const defaultStation=stations.find(s=>s.stationId===stationsDoc.defaultStationId)??stations.find(s=>s.normalVisitorPositionCandidate)??stations[0];if(defaultStation)el.station.value=defaultStation.stationId;
  for(const route of tourDoc.routes){const option=document.createElement('option');option.value=route.id;option.textContent=`${route.title} · ${route.waypoints.length} poses`;el.tour.append(option);}
  for(const c of candidateDoc.candidates){const option=document.createElement('option');option.value=c.candidateId;option.textContent=c.publicLabel??`Geometry viewpoint · ${c.candidateId}`;el.artwork.append(option);}
  if(candidateDoc.defaultCandidateId)el.artwork.value=candidateDoc.defaultCandidateId;
  const hasGeometryView=candidateDoc.candidates.length>0;
  el.artwork.disabled=!hasGeometryView;el.orbitArtwork.disabled=!hasGeometryView;
  const artworkLabel=document.querySelector('label[for="artwork"]');if(artworkLabel)artworkLabel.textContent='Geometry view';
  el.orbitArtwork.textContent=hasGeometryView?'Orbit geometry view':'No geometry viewpoint configured';
  if(!hasGeometryView)el.orbitArtwork.title='A reviewed geometry viewpoint is required before Orbit can start.';
  for(const c of footprintDoc.candidates){const option=document.createElement('option');option.value=c.id;option.textContent=`${c.algorithm.replaceAll('_',' ')} · margin ${c.marginUnits.toFixed(3)} · ${c.areaUnits2.toFixed(1)} u² · ${c.trustedStationInclusion.insideOrOn}/${c.trustedStationInclusion.total} cameras`;el.candidateSelect.append(option);}
  el.candidateSelect.value=boundary.horizontalBoundary.candidateId;
}"""
    text = text[:start] + generic_controls + text[end:]
    text = text.replace("const selected=el.artwork.value;\n  const spec=sceneSpec(selected==='hanging-text'?'hanging-text':selected==='attention-008'?'photo-wall':selected==='attention-018'?'central-table':'attention-003');", "const selected=el.artwork.value;\n  const spec=sceneSpec(selected);")
    start = text.index("function startArtworkOrbit() {")
    end = text.index("\nfunction saveBookmark()", start)
    generic_orbit = """function startArtworkOrbit() {
  const spec=sceneSpec(el.artwork.value);
  if(!spec){logDecision('Geometry Orbit unavailable: no approved geometry viewpoint is configured.',false);return false;}
  const pose=getPose(),direction=spec.front;
  let position=vAdd(spec.center,vScale(direction,2.8));
  if(!validPosition(position))position=pose.position;
  const orbitPose={position,target:spec.center,fov:pose.fov};
  if(!applyPose(orbitPose)){logDecision('Geometry Orbit rejected: this view would leave the approved architecture.',false);return false;}
  orbitCenter=[...spec.center];setMode('artwork-orbit');
  if(!applyPose(orbitPose)){orbitCenter=null;logDecision('Geometry Orbit anchor failed shared boundary validation.',false);return false;}
  logDecision(`Geometry Orbit focused on ${spec.id}; no artwork identity is assigned.`,true);updatePoseHud();return true;
}"""
    text = text[:start] + generic_orbit + text[end:]
    text = text.replace("const principal=new Set(['IMG_0542','IMG_0503','IMG_0748','IMG_0540','IMG_0633','IMG_0578']);", "const principal=new Set(cameraDoc?.principalImageKeys??[]);")
    text = text.replace("station.imageKeys[0]??'trusted camera'", "station.imageKeys?.[0]??'trusted camera'")
    text = text.replace("'phase2er-free-gallery-bookmark-v1'", "'exhibition-archive-free-gallery-bookmark-v1'")
    text = text.replace("el.serverStatus.textContent=`${health.modelBytes.toLocaleString()} bytes · ${health.indexedSourceImages} local photos`;", "el.serverStatus.textContent=`Static archive · ${health.modelBytes.toLocaleString()} model bytes`;" )
    text = text.replace("el.modelStatus.textContent=`Ready · ${health.modelBytes.toLocaleString()} bytes · exact preserved ROI-off baseline`;", "el.modelStatus.textContent=`Ready · ${health.modelBytes.toLocaleString()} bytes · selected archive model identity verified`;" )
    text = text.replace("Phase 2EQ rendering settings unchanged", "archive viewer rendering settings")
    text = text.replace("Phase 2EQ", "accepted enclosure")
    text = text.replace("Phase2EQ", "accepted enclosure")
    text = text.replace("phase2er-camera-derived-boundary.json", "exhibition-archive-navigation.json")
    text = text.replace("phase2er-diagnostic-camera.json", "exhibition-archive-diagnostic-camera.json")
    text = text.replace("phase2er-camera-render-metadata.json", "exhibition-archive-camera-render-metadata.json")
    text = text.replace("phase2er-human-acceptance.json", "exhibition-archive-human-acceptance.json")
    text = text.replace("phase2er-", "exhibition-archive-").replace("__phase2erTestHooks", "__exhibitionArchiveTestHooks")
    text = re.sub(r"IMG_\d+(?: \d+)?", "source-image", text)
    text = text.replace("'hanging-text'", "'geometry-view'").replace("'photo-wall'", "'geometry-view'").replace("'central-table'", "'geometry-view'")
    old_clear = "function clearTour() { if(tourRaf) cancelAnimationFrame(tourRaf); tourRaf=0; }"
    new_clear = "function clearTour() { if(tourRaf) cancelAnimationFrame(tourRaf); tourRaf=0; tourPlayback=null; updateTourButtons(); }"
    text = text.replace(old_clear, new_clear)
    start = text.index("async function playTour() {")
    end = text.index("\nconst acceptanceTasks", start)
    text = text[:start] + r'''function updateTourButtons() {
  if(!el.playTour)return;
  const active=!!tourPlayback,paused=active&&tourPlayback.paused;
  el.playTour.disabled=active;el.pauseTour.disabled=!active||paused;el.resumeTour.disabled=!active||!paused;el.nextTourStop.disabled=!active;
}
function tourSegmentDuration(from,to) { return clamp(length(vSub(to.position,from.position))/1.25*1000,450,3500); }
function poseBetween(from,to,t) {
  const mix=(a,b)=>a+(b-a)*t;
  return {position:from.position.map((x,i)=>mix(x,to.position[i])),
    target:from.target.map((x,i)=>mix(x,to.target[i])),fov:mix(from.fov??70.75,to.fov??70.75)};
}
function finishTour(message,ok=true) { clearTour();logDecision(message,ok);updatePoseHud(); }
function scheduleTourSegment(fromPose,duration) {
  tourPlayback.fromPose=fromPose;tourPlayback.segmentDuration=Math.max(1,duration);tourPlayback.startedAt=performance.now();
  const tick=now=>{
    if(!tourPlayback||tourPlayback.paused||activeMode!=='guided-tour')return;
    const target=tourPlayback.route.waypoints[tourPlayback.targetIndex],t=clamp((now-tourPlayback.startedAt)/tourPlayback.segmentDuration,0,1);
    if(!applyPose(poseBetween(tourPlayback.fromPose,target,t))){finishTour(`Guided tour stopped at stop ${tourPlayback.targetIndex+1}: the current architectural boundary rejected the route.`,false);return;}
    if(t>=1){
      tourPlayback.targetIndex++;
      if(tourPlayback.targetIndex>=tourPlayback.route.waypoints.length){finishTour(`Completed ${tourPlayback.route.title}; every stop passed the shared architectural boundary policy.`,true);return;}
      const pose=getPose();scheduleTourSegment(pose,tourSegmentDuration(pose,tourPlayback.route.waypoints[tourPlayback.targetIndex]));return;
    }
    tourRaf=requestAnimationFrame(tick);
  };
  tourRaf=requestAnimationFrame(tick);updateTourButtons();
}
function playTour() {
  const route=tourDoc.routes.find(x=>x.id===el.tour.value);
  if(!route||!Array.isArray(route.waypoints)||route.waypoints.length<2){logDecision('Guided tour rejected: select a route with at least two stops.',false);return false;}
  for(let i=0;i<route.waypoints.length;i++){
    const waypoint=route.waypoints[i];
    if(!waypoint?.position||!waypoint?.target||!waypoint.position.every(Number.isFinite)||!waypoint.target.every(Number.isFinite)){
      logDecision(`Guided tour rejected: stop ${i+1} has invalid camera coordinates.`,false);return false;
    }
    if(i>0&&!segmentAllowed(route.waypoints[i-1].position,waypoint.position)){
      logDecision(`Guided tour rejected: the path from stop ${i} to stop ${i+1} leaves the approved architectural boundary.`,false);updatePoseHud();return false;
    }
  }
  setMode('guided-tour');
  const initial=route.waypoints[0];
  if(!applyPose(initial)){logDecision('Guided route start rejected by the architectural boundary policy.',false);return false;}
  tourPlayback={route,targetIndex:1,paused:false};
  logDecision(`Playing ${route.title} through the shared architectural boundary policy.`,true);
  scheduleTourSegment(initial,tourSegmentDuration(initial,route.waypoints[1]));return true;
}
function pauseTour() {
  if(!tourPlayback||tourPlayback.paused)return false;
  if(tourRaf)cancelAnimationFrame(tourRaf);tourRaf=0;tourPlayback.paused=true;
  logDecision(`Paused ${tourPlayback.route.title} at stop ${tourPlayback.targetIndex+1}.`,true);updateTourButtons();return true;
}
function resumeTour() {
  if(!tourPlayback||!tourPlayback.paused)return false;
  const pose=getPose(),target=tourPlayback.route.waypoints[tourPlayback.targetIndex];
  tourPlayback.paused=false;logDecision(`Resumed ${tourPlayback.route.title} toward stop ${tourPlayback.targetIndex+1}.`,true);
  scheduleTourSegment(pose,tourSegmentDuration(pose,target));return true;
}
function nextTourStop() {
  if(!tourPlayback)return false;
  if(tourRaf)cancelAnimationFrame(tourRaf);tourRaf=0;
  const target=tourPlayback.route.waypoints[tourPlayback.targetIndex];
  if(!applyPose(target)){finishTour(`Guided tour stopped: stop ${tourPlayback.targetIndex+1} failed the architectural boundary check.`,false);return false;}
  tourPlayback.targetIndex++;
  if(tourPlayback.targetIndex>=tourPlayback.route.waypoints.length){finishTour(`Completed ${tourPlayback.route.title} by advancing to its final stop.`,true);return true;}
  const paused=tourPlayback.paused;logDecision(`Advanced to stop ${tourPlayback.targetIndex}; ${paused?'tour paused':'tour continues'}.`,true);
  if(!paused)scheduleTourSegment(getPose(),tourSegmentDuration(getPose(),tourPlayback.route.waypoints[tourPlayback.targetIndex]));else updateTourButtons();
  return true;
}''' + text[end:]
    text = text.replace("el.playTour.addEventListener('click',()=>void playTour());el.orbitArtwork.addEventListener('click',startArtworkOrbit);",
                        "el.playTour.addEventListener('click',()=>void playTour());el.pauseTour.addEventListener('click',pauseTour);el.resumeTour.addEventListener('click',resumeTour);el.nextTourStop.addEventListener('click',nextTourStop);el.orbitArtwork.addEventListener('click',startArtworkOrbit);")
    return text


def build_website(root: Path, config: dict, state, output_dir: Path) -> dict:
    website = output_path(root, config) / "website"
    staging = website.with_name("website.staging")
    if staging.exists():
        shutil.rmtree(staging)
    for folder in ("viewer", "viewer/vendor", "assets", "api", "config"):
        (staging / folder).mkdir(parents=True, exist_ok=True)
    source_viewer = PRODUCT_ROOT / "templates" / "viewer"
    for name in ("index.html", "style.css"):
        shutil.copy2(source_viewer / name, staging / "viewer" / name)
    app = _relativeize_app((source_viewer / "app.js").read_text(encoding="utf-8"))
    (staging / "viewer" / "app.js").write_text(app, encoding="utf-8")
    for name in ("index.js", "index.css"):
        shutil.copy2(source_viewer / "vendor" / name, staging / "viewer" / "vendor" / name)
    # Keep the exact accepted navigation implementation; hide controls that
    # could edit the enclosure or expose private artifact-diagnosis UI.
    html = (staging / "viewer" / "index.html").read_text(encoding="utf-8")
    html = html.replace("<link rel=\"stylesheet\" href=\"/vendor/index.css\">", "<link rel=\"stylesheet\" href=\"./vendor/index.css\">")
    html = html.replace("<link rel=\"stylesheet\" href=\"./style.css\">", "<link rel=\"stylesheet\" href=\"./style.css\">")
    html = html.replace("<script type=\"module\" src=\"./app.js\"></script>", "<script type=\"module\" src=\"./app.js\"></script>")
    html = html.replace("<title>Chuen Lung — Phase 2ER Camera-derived Enclosure</title>",
                        f"<title>{config.get('viewer', {}).get('title', config['project_id'])} — Exhibition Archive</title><link rel=\"icon\" href=\"data:,\">")
    html = html.replace('<option value="debug">Debug Fly · unrestricted developer mode</option>', "")
    html = html.replace('<label for="tour">Guided route</label><select id="tour"></select><button id="playTour">Play route</button>',
                        '<label for="tour">Guided route</label><select id="tour"></select><button id="playTour">Start route</button><div class="row"><button id="pauseTour" disabled>Pause</button><button id="resumeTour" disabled>Resume</button><button id="nextTourStop" disabled>Next stop</button></div>')
    html = html.replace('<label for="artwork">Artwork Orbit focus</label><select id="artwork"></select><button id="orbitArtwork">Orbit selected artwork</button>',
                        '<label for="artwork">Geometry view</label><select id="artwork"></select><button id="orbitArtwork" disabled>No geometry viewpoint configured</button>')
    html = html.replace("PRESERVED ROI-OFF BASELINE · READ ONLY", "SELECTED ARCHIVE MODEL · READ ONLY")
    html = html.replace("Spirula · 20,000 iterations", "3D Gaussian Splatting model")
    html = html.replace("Camera-derived visitor enclosure", "Explore the exhibition")
    html = html.replace("<script type=\"module\" src=\"./app.js\"></script>", "<script type=\"module\" src=\"./app.js\"></script>")
    html = html.replace("<div class=\"eyebrow\">CHUEN LUNG EXHIBITION · PHASE 2ER</div><h1>Explore the exhibition</h1>", f"<div class=\"eyebrow\">EXHIBITION ARCHIVE · {config['project_id']}</div><h1>{config.get('viewer', {}).get('title', config['project_id'])}</h1>")
    html = html.replace("<a href=\"#ghostReview\">Ghost review</a>", "")
    html = html.replace("<a href=\"#acceptance\">Acceptance</a>", "")
    html = html.replace('aria-label="Draw a polygon over the hanging-text ghost region"', 'aria-label="Viewer annotation overlay"')
    html = re.sub(r'<section id="ghostReview">.*?</section>', '''<section id="ghostReview" hidden>
      <select id="diagnosticScene"><option value="geometry-view">Geometry view</option></select><div id="diagnosticPose"></div><button id="captureDiagnostic"></button><button id="annotationToggle"></button><button id="annotationClear"></button><button id="annotationSave"></button><button id="annotationExport"></button><input id="annotationLabel"><div id="annotationStatus"></div><div id="annotationList"></div><div id="sourcePhoto"></div>
    </section>''', html, flags=re.S)
    html = re.sub(r'<section id="acceptance">.*?</section>', '''<section id="acceptance" hidden><div id="acceptanceChecklist"></div><button id="exportAcceptance"></button><button id="resetAcceptance"></button></section>''', html, flags=re.S)
    html = re.sub(r'<section>\s*<div class="eyebrow">RUNTIME DIAGNOSTICS</div>.*?</section>', '''<section id="runtimeDiagnostics" hidden><div id="runtimeSummary"></div><button id="exportCamera"></button></section>''', html, count=1, flags=re.S)
    html = html.replace("The Phase 2EQ floor and ceiling constraints are retained.", "The accepted floor and ceiling constraints are retained.")
    html = html.replace("Phase 2EQ", "accepted enclosure")
    html = html.replace("Debug Fly is separate; returning restores a valid interior pose.", "Visitor movement stays inside the approved navigation boundary.")
    html = html.replace("<script type=\"module\" src=\"./app.js\"></script>", "<script type=\"module\" src=\"./app.js\"></script>")
    (staging / "viewer" / "index.html").write_text(html, encoding="utf-8")
    public_css = "\n#ghostReview,#acceptance,#runtimeDiagnostics{display:none!important}#boundary .editor-row,#boundary .row,#boundary .toggle-grid,#boundary #candidateSelect,#boundary label[for=candidateSelect],#boundary #vertexSummary,#boundary #boundarySaveStatus{display:none!important}#boundaryMap{pointer-events:none;cursor:default!important}"
    with (staging / "viewer" / "style.css").open("a", encoding="utf-8") as f: f.write(public_css)
    from .model_registry import selected_model_record
    model_record = selected_model_record(root, config, state)
    baseline = Path(model_record["path"])
    target_model = staging / "assets" / "exhibition.ply"
    shutil.copy2(baseline, target_model)
    _static_api(root, config, staging, model_record, state)
    root_page = f'''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="0;url=viewer/"><title>{config.get("viewer", {}).get("title", config["project_id"])}</title><body><p><a href="viewer/">Open the exhibition archive viewer</a></p></body></html>'''
    (staging / "index.html").write_text(root_page, encoding="utf-8")
    artwork_config = config.get("integrations", {}).get("artwork", {})
    confirmed_path = project_path(root, config, artwork_config["confirmed_artworks"]) if artwork_config.get("confirmed_artworks") else output_path(root, config) / "work" / "confirmed-artworks.json"
    confirmed_count = len(read_json(confirmed_path).get("artworks", []))
    readme = f'''# {config.get("viewer", {}).get("title", config["project_id"])} — static archive website\n\nServe this directory with any static HTTP server and open `index.html`. The model URL, viewer modules, and configuration references are relative.\n\nThe selected model is identified by SHA-256 `{model_record["sha256"]}`. Selection provenance: {model_record.get("selection", "reviewed model")}. This website contains the accepted PlayCanvas GSplat viewer assets and read-only navigation package. It excludes original photographs, private source paths, and internal reviewer notes. Only reviewer-confirmed catalogue records are included; current confirmed count: {confirmed_count}.\n\nUse an HTTP server rather than `file://`. Large PLY transfers depend on the deployment host's file-size, caching, and byte-range behavior. The local Python server used for QA serves the complete PLY response.\n'''
    (staging / "README.md").write_text(readme, encoding="utf-8")
    license_text = """MIT License

Copyright (c) 2011-2026 PlayCanvas Ltd.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the \"Software\"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED \"AS IS\", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

The bundled viewer runtime is the accepted repository-local PlayCanvas / SuperSplat viewer distribution. Upstream reference: https://github.com/playcanvas/supersplat-viewer
Engine version detected in the bundle: 2.23.0. The upstream Viewer is MIT-licensed. The exhibition artwork and source photographs have separate project-specific rights and are not covered by this software license.
"""
    (staging / "viewer" / "vendor" / "LICENSE.txt").write_text(license_text, encoding="utf-8")
    # Static package metadata is deterministic apart from source filesystem metadata.
    index = [{"path": p.relative_to(staging).as_posix(), "size_bytes": p.stat().st_size, "sha256": sha256_file(p)} for p in sorted(staging.rglob("*")) if p.is_file()]
    write_json(staging / "config" / "package-index.json", {"schema_version": "exhibition-archive.website-index.v1", "files": index})
    if website.exists():
        shutil.rmtree(website)
    staging.replace(website)
    packaged_model = website / "assets" / "exhibition.ply"
    return {"status": "COMPLETED", "artifacts": [website / "index.html", website / "viewer" / "app.js", packaged_model, website / "config" / "navigation.json"],
            "details": {"website": str(website), "model_size_bytes": packaged_model.stat().st_size, "model_sha256": sha256_file(packaged_model), "public_artwork_count": confirmed_count, "viewer_origin": "accepted Phase2ER PlayCanvas runtime and navigation app with public static data adapters", "source_photographs_included": False}}
