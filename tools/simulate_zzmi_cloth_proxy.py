"""Replay a ZZMI garment on a continuous leg proxy, outside the game.

The exported render mesh supplies only the follower geometry. The proxy is
fitted to cross sections of the exported collision body and is the only mesh
that enters the position solver. All lengths use the mod's coordinate units.
This standalone experiment requires NumPy and SciPy.
"""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import re
import sys

import numpy as np
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from blueprint.drag_cloth_proxy_core import BodySections, Followers, SleeveSolver, islands, unit

UNMAPPED = 0xFFFFFFFF


def ini_sections(path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    return dict(re.findall(r"^\[([^\n\]]+)\][ \t]*\n(.*?)(?=^\[|\Z)", text, re.M | re.S))


def field(section, key):
    value = re.search(rf"^\s*{re.escape(key)}\s*=\s*([^\n;]+)", section, re.M)
    return value[1].strip() if value else ""


def mod_asset(root, value):
    path = (root / value.replace("\\", "/")).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError(f"Expected an asset inside the mod: {value}")
    return path


def index_triangles(root, ini, draw):
    arrays = []
    for key, section in ini.items():
        if re.fullmatch(rf"Resource_LOD0\.{draw}_.*_Index", key, re.I):
            dtype = "<u2" if "R16_UINT" in field(section, "format") else "<u4"
            arrays.append(np.fromfile(mod_asset(root, field(section, "filename")), dtype).reshape(-1, 3))
    if not arrays:
        raise ValueError(f"No exported LOD0 index buffers found for {draw}")
    return np.concatenate(arrays)


def load_garment(root, group, zone):
    label = f"g{group}"
    group_folders = sorted({path.parent for path in (root / "res" / "drag_cloth").rglob(f"{label}/config.buf")})
    if len(group_folders) != 1:
        raise ValueError(f"Expected one {label} cloth group, found {len(group_folders)}")
    folder = group_folders[0]
    cfg = np.fromfile(folder / "config.buf", "<f4").reshape(-1, 4)
    positions = np.fromfile(folder.parent / "bind.buf", "<f4").reshape(int(cfg[0, 1]), -1)[:, :3].astype(float)
    wear = np.fromfile(folder / "map.buf", "<u4")
    flags = np.fromfile(folder / "control_zones.buf", "<f4").reshape(-1, 4)
    axes = np.fromfile(folder / "control_axes.buf", "<f4").reshape(-1, 4)
    zones = np.flatnonzero(flags[:, 0] > 0).tolist()
    zone = zones[0] if zone is None and zones else zone
    if zone not in zones:
        raise ValueError(f"Zone {zone} is not active in {label}; available: {zones}")
    ini = ini_sections(root / f"{root.name}.ini")
    config_path = (folder / "config.buf").relative_to(root).as_posix()
    config_name, = [key for key, section in ini.items()
                    if field(section, "filename").replace("\\", "/") == config_path]
    draw = re.search(r"SSMTCloth_([0-9a-f]{8})_", config_name, re.I)
    if not draw:
        raise ValueError(f"Could not resolve the DrawIB for {config_name}")
    select, = [value for key, value in ini.items()
               if key.startswith(f"CustomShaderSSMTClothG{group}Select_")
               and field(value, "cs-t82") == f"ref {config_name}"]
    body_name = field(select, "cs-t87").removeprefix("ref ")
    body = np.fromfile(mod_asset(root, field(ini[body_name], "filename")), "<u4").reshape(-1, 3)
    surface = np.fromfile(folder / "surface_mask.buf", "<u4") > 0
    if len(surface) == len(body) and surface.any():
        body = body[surface]
    all_faces = index_triangles(root, ini, draw[1])
    all_faces = all_faces[(all_faces < len(wear)).all(axis=1)]
    faces = all_faces[(wear[all_faces] != UNMAPPED).all(axis=1)]
    if not len(faces):
        raise ValueError(f"No render triangles match {label}'s wear map")
    rows = np.unique(faces)
    faces = np.searchsorted(rows, faces)
    axis = axes[zone, :3].astype(float)
    travel = float(axes[zone, 3])
    if travel <= 0:
        travel = max(0.0, float((1 - cfg[4, 0]) * cfg[6, 3]))
    controls = {index: (axes[index, :3].astype(float), float(axes[index, 3]), int(flags[index, 0]))
                for index in zones}
    return positions[rows], faces, positions[body], cfg, zone, axis, travel, controls, draw[1]


def write_obj(path, points, faces):
    with path.open("w", encoding="utf-8") as file:
        for x,y,z in points:
            file.write(f"v {x:.8f} {y:.8f} {z:.8f}\n")
        for a,b,c in faces+1:
            file.write(f"f {a} {b} {c}\n")


def encode(array, dtype):
    return base64.b64encode(np.asarray(array, dtype=dtype).tobytes()).decode("ascii")


def write_preview(path, payload):
    # Keep the preview dependency-free and display the actual exported mesh.
    before = """<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\">
<title>衣物代理模拟</title>
<style>
body{margin:0;background:#10151b;color:#eaf0f5;font:14px system-ui}main{max-width:1120px;margin:auto;padding:20px}h1{font-size:20px;margin:0 0 14px}label{margin-right:20px}canvas{width:100%;height:min(73vh,700px);background:#18232e;touch-action:none;border:1px solid #354657;margin-top:12px}output{display:block;margin-top:10px;color:#c8d5e4}input[type=range]{width:min(48vw,440px);vertical-align:middle}
</style><main><h1>衣物代理模拟</h1><label>行程 <input id=stage type=range min=0 step=1 value=0></label><label><input id=mesh type=checkbox checked>显示代理网格</label><label><input id=body type=checkbox>显示碰撞轮廓</label><canvas id=view></canvas><output id=status></output></main>
<script>const data="""
    after = """;
const bytes=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0)).buffer;
const f32=s=>new Float32Array(bytes(s)), u32=s=>new Uint32Array(bytes(s));
const clothFaces=u32(data.faces), guideFaces=u32(data.guide_faces), stiff=u32(data.rigid);
const canvas=document.querySelector('#view'), ctx=canvas.getContext('2d'), stage=document.querySelector('#stage');
const mesh=document.querySelector('#mesh'), body=document.querySelector('#body'), status=document.querySelector('#status');
stage.max=data.frames.length-1;let yaw=.42,tilt=-.23,held=false,prev;
const bodyPoints=f32(data.body);
function project(p,w,h){
 const points=[];const k=Math.min(w,h)*.76/data.extent;
 for(let i=0;i<p.length;i+=3){const x=p[i]-data.center[0],y=p[i+1]-data.center[1],z=p[i+2]-data.center[2];const a=x*Math.cos(yaw)-y*Math.sin(yaw),b=x*Math.sin(yaw)+y*Math.cos(yaw);points.push([w*.5+a*k,h*.5+(b*Math.sin(tilt)-z*Math.cos(tilt))*k,b*Math.cos(tilt)+z*Math.sin(tilt)]);}
 return points;
}
function draw(){
 const w=canvas.clientWidth,h=canvas.clientHeight;const device=window.devicePixelRatio||1;
 canvas.width=w*device;canvas.height=h*device;ctx.scale(device,device);
 const m=data.frames[+stage.value], layers=[];
 function surface(vertices,indices,id){const p=project(vertices,w,h);for(let i=0;i<indices.length;i+=3){const a=indices[i],b=indices[i+1],c=indices[i+2];layers.push([p[a],p[b],p[c],(p[a][2]+p[b][2]+p[c][2])/3,id===0?(stiff[a]?1:0):id]);}}
 if(body.checked)surface(bodyPoints,guideFaces,3);
 surface(f32(m.cloth),clothFaces,0);
 if(mesh.checked)surface(f32(m.guide),guideFaces,2);
 layers.sort((a,b)=>a[3]-b[3]);
 for(const [a,b,c,depth,kind] of layers){ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.lineTo(c[0],c[1]);ctx.closePath();
  if(kind<2){const back=(b[0]-a[0])*(c[1]-a[1])<(b[1]-a[1])*(c[0]-a[0]);ctx.fillStyle=kind===1?(back?'#aa6c27':'#e6a64b'):(back?'#35709d':'#55a6d5');ctx.fill();ctx.strokeStyle='#1b3d57';ctx.lineWidth=.36;ctx.stroke();}
  else if(kind===2){ctx.strokeStyle='rgba(114,244,183,.56)';ctx.lineWidth=.55;ctx.stroke();}
  else{ctx.fillStyle='rgba(166,185,196,.20)';ctx.fill();}
 }
 status.textContent=`${data.group} · 区域 ${data.zone} · ${Math.round(m.progress*100)}% · 衣片 ${data.soft_count} 顶点 · 刚性附件 ${data.rigid_count} 顶点 · 拖动画布旋转`;
}
canvas.onpointerdown=e=>{held=true;prev=e;canvas.setPointerCapture(e.pointerId)};canvas.onpointerup=canvas.onpointercancel=()=>held=false;
canvas.onpointermove=e=>{if(held){yaw+=(e.clientX-prev.clientX)/140;tilt=Math.max(-1.4,Math.min(1.4,tilt+(e.clientY-prev.clientY)/140));prev=e;draw();}};
for(const input of [stage,mesh,body])input.oninput=draw;
new ResizeObserver(draw).observe(canvas);draw();</script></html>"""
    path.write_text(before + json.dumps(payload, separators=(",", ":")) + after, encoding="utf-8")


def body_mesh(root, results):
    ini = ini_sections(root / f"{root.name}.ini")
    colliders = {}
    for report, _, _, _, _, collider in results:
        colliders.setdefault(report["draw"], []).append(collider)
    meshes = []
    for name, section in ini.items():
        match = re.fullmatch(r"Resource([0-9a-f]{8})Position", name, re.I)
        if match is None:
            continue
        draw = match[1]
        stride = int(field(section, "stride"))
        vertices = np.fromfile(mod_asset(root, field(section, "filename")), "<f4").reshape(-1, stride // 4)[:, :3]
        faces = index_triangles(root, ini, draw)
        if draw in colliders:
            labels = islands(vertices, faces)
            samples = np.ascontiguousarray(np.concatenate(colliders[draw]).reshape(-1, 3), dtype="<f4")
            keys = np.ascontiguousarray(vertices).view("V12").ravel()
            overlap = np.isin(keys, samples.view("V12").ravel())
            used = np.unique(labels[overlap])
            faces = faces[np.isin(labels[faces[:, 0]], used)]
        if len(faces) > 3:
            used = np.unique(faces)
            meshes.append((vertices[used], np.searchsorted(used, faces)))
    return meshes


def preview_mesh(points, faces, cell):
    _, inverse = np.unique(np.rint(points / cell).astype(np.int32), axis=0, return_inverse=True)
    size = len(np.unique(inverse))
    points = np.column_stack([np.bincount(inverse, weights=axis) for axis in points.T])
    points /= np.bincount(inverse, minlength=size)[:, None]
    remapped = inverse[faces]
    remapped = remapped[(np.diff(np.sort(remapped, axis=1), axis=1) > 0).all(axis=1)]
    _, keep = np.unique(np.sort(remapped, axis=1), axis=0, return_index=True)
    return points, remapped[keep]


def crossing_edges(points, faces, obstacles):
    """Count unique mesh edges that cross another triangle surface."""
    from scipy.spatial import cKDTree

    edges = np.unique(np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1), axis=0)
    start, end = points[edges].transpose(1, 0, 2)
    direction = end - start
    mid = (start + end) * .5
    centers = obstacles.mean(axis=1)
    radius = np.linalg.norm(obstacles - centers[:, None], axis=2).max()
    neighborhoods = cKDTree(centers).query_ball_point(mid, radius + .5 * np.linalg.norm(direction, axis=1))
    candidates = np.repeat(np.arange(len(edges)), np.fromiter(map(len, neighborhoods), int))
    if not len(candidates):
        return 0
    a, b, c = obstacles[np.concatenate(neighborhoods).astype(int)].transpose(1, 0, 2)
    ab, ac = b - a, c - a
    ray = direction[candidates]
    ray_cross = np.cross(ray, ac)
    det = np.sum(ab * ray_cross, axis=1)
    valid = np.abs(det) > 1e-12
    inverse = np.divide(1, det, out=np.zeros_like(det), where=valid)
    delta = start[candidates] - a
    u = np.sum(delta * ray_cross, axis=1) * inverse
    cross_start = np.cross(delta, ab)
    v = np.sum(ray * cross_start, axis=1) * inverse
    t = np.sum(ac * cross_start, axis=1) * inverse
    hits = valid & (u >= 0) & (v >= 0) & (u + v <= 1) & (t > 1e-6) & (t < 1 - 1e-6)
    return len(np.unique(candidates[hits]))


def write_whole_preview(path, payload):
    template = """<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>整身穿脱模拟</title>
<style>
body{margin:0;background:#10161e;color:#e9f2fa;font:15px system-ui}main{max-width:1200px;margin:auto;padding:22px}
h1{font-size:25px;margin:0 0 16px}header{display:flex;flex-wrap:wrap;align-items:center;gap:12px;margin-bottom:14px}
button,select{background:#202e3c;color:#e9f2fa;border:1px solid #3d5368;border-radius:5px;padding:7px 11px;font:inherit}
button[aria-pressed=true]{background:#b8e7ff;color:#102535;border-color:#b8e7ff}
article{display:grid;grid-template-columns:minmax(0,1fr) minmax(245px,27%);gap:14px}
canvas{width:100%;height:min(72vh,690px);min-height:440px;background:#182633;touch-action:none;border-radius:8px}
aside{background:#182633;border-radius:8px;padding:18px 14px;font-size:14px}table{border-collapse:collapse;width:100%}
td,th{padding:9px 4px;text-align:left;border-bottom:1px solid #344555}summary{margin-top:14px;color:#aec5d8;line-height:1.5}
.legend{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:6px}
@media(max-width:780px){article{grid-template-columns:1fr}}
</style><main><h1 id="title">整身穿脱模拟</h1><header id="options"><label><input id="showProxy" type="checkbox">代理网格</label>
<label><input id="showBody" type="checkbox" checked>身体</label><label>聚焦 <select id="focus"><option value="-1">全身</option></select></label></header>
<article><canvas id="view" aria-label="整身穿脱模型，拖动旋转"></canvas><aside><div id="status"></div>
<table><thead><tr><th>区域</th><th>行程</th><th>边长峰值</th><th>新增压入</th></tr></thead><tbody id="stats"></tbody></table>
<summary>拖动模型旋转，滚轮缩放。可为每组单独选档。边长峰值包含上一档到本档的过渡子步。各档数据来自导出模型的离线模拟。</summary>
<summary id="contact"></summary>
</aside></article></main><script>const data=PAYLOAD;
const get=id=>document.getElementById(id),canvas=get('view'),ctx=canvas.getContext('2d'),focus=get('focus');
document.title=get('title').textContent=data.name+' · 整身穿脱模拟';
const bytes=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0)).buffer;
const f32=s=>new Float32Array(bytes(s)),u32=s=>new Uint32Array(bytes(s));
const palette=['107,182,231','237,139,132','102,211,176','174,155,239','227,192,119'];
const skin=data.body.map(([p,f])=>[f32(p),u32(f)]);
let yaw=.24,tilt=-.07,zoom=1,pointer=null,queued=false;
const stages=data.groups.map(()=>0);
const buttons=data.levels.map((value,i)=>{
 const button=document.createElement('button');button.textContent=i===0&&value===0?'起始':Math.round(value*100)+'%';
 button.onclick=()=>{stages.fill(i);update()};return button;
});
get('options').prepend(...buttons);
data.groups.forEach((g,i)=>{
 g.faces=u32(g.faces);g.guideFaces=u32(g.guideFaces);g.rigid=u32(g.rigid);
 g.frames=g.frames.map(([cloth,proxy])=>[f32(cloth),f32(proxy)]);
 const option=document.createElement('option');option.value=i;option.textContent='区域 '+g.zones.join(' / ');focus.append(option);
});
const update=()=>{
 buttons.forEach((button,i)=>button.setAttribute('aria-pressed',stages.every((stage,j)=>data.groups[j].fixed||stage===i)));
 get('status').textContent=data.groups.length+' 组 / '+data.regionCount+' 区域';
 get('stats').innerHTML=data.groups.map((g,i)=>{
  const m=g.metrics[stages[i]],gap=m.extra_body_penetration;
  const options=data.levels.map((level,j)=>'<option value="'+j+'" '+(j===stages[i]?'selected':'')+'>'+Math.round(level*100)+'%</option>').join('');
  const control=g.fixed?'仅碰撞':'<select aria-label="区域 '+g.zones.join(' / ')+' 的行程" data-group="'+i+'">'+options+'</select>';
  return '<tr><td><span class="legend" style="background:rgb('+palette[i%palette.length]+')"></span>'+g.zones.join(' / ')+'</td>'+
   '<td>'+control+'</td><td>'+
   m.peak_display_edge_ratio.toFixed(2)+'×</td><td>'+(gap<1e-6?'≈ 0':gap.toExponential(1))+'</td></tr>';
 }).join('');
 get('contact').textContent=data.groups.filter(g=>g.fixed).map(g=>{
  const total=data.groups.reduce((n,m,i)=>n+(m.metrics[stages[i]].fixed_crossing_edges?.[g.group]||0),0);
  return '与区域 '+g.zones.join(' / ')+' 相交的衣物边共有 '+total+' 条。';
 }).join(' ');
 schedule();
};
get('stats').onchange=e=>{stages[e.target.dataset.group]=+e.target.value;update()};
function project(points,w,h,center,extent){
 const result=[],scale=Math.min(w*.90,h*.91)*zoom/extent,cs=Math.cos(yaw),sn=Math.sin(yaw),ct=Math.cos(tilt),st=Math.sin(tilt);
 for(let i=0;i<points.length;i+=3){
  const x=points[i]-center[0],y=points[i+1]-center[1],z=points[i+2]-center[2],q=x*sn+y*cs;
  result.push([w*.5+(x*cs-y*sn)*scale,h*.5+(q*st-z*ct)*scale,-q*ct-z*st]);
 }
 return result;
}
function draw(){
 queued=false;const w=canvas.clientWidth,h=canvas.clientHeight,dpr=window.devicePixelRatio||1;
 canvas.width=w*dpr;canvas.height=h*dpr;ctx.scale(dpr,dpr);
 const group=data.groups[+focus.value],center=group?group.center:data.center,extent=group?group.extent:data.extent;
 const triangles=[];
 function surface(points,faces,color,rigid=null,wire=false){
  const p=project(points,w,h,center,extent);for(let i=0;i<faces.length;i+=3){
   const a=p[faces[i]],b=p[faces[i+1]],c=p[faces[i+2]];
   triangles.push([a,b,c,(a[2]+b[2]+c[2])/3,rigid&&rigid[faces[i]]?'246,201,116':color,wire]);
  }
 }
 if(get('showBody').checked)skin.forEach(([p,f],i)=>surface(p,f,i===0?'164,167,172':'122,133,151'));
 data.groups.forEach((g,i)=>{
  if(group&&group!==g)return;
  surface(g.frames[stages[i]][0],g.faces,palette[i%palette.length],g.rigid);
  if(get('showProxy').checked)surface(g.frames[stages[i]][1],g.guideFaces,'131,248,179',null,true);
 });
 triangles.sort((a,b)=>a[3]-b[3]);
 for(const [a,b,c,depth,color,wire] of triangles){
  ctx.beginPath();ctx.moveTo(a[0],a[1]);ctx.lineTo(b[0],b[1]);ctx.lineTo(c[0],c[1]);ctx.closePath();
  if(wire){ctx.lineWidth=.65;ctx.strokeStyle='rgba('+color+',.78)';ctx.stroke();continue;}
  const face=(b[0]-a[0])*(c[1]-a[1])<(b[1]-a[1])*(c[0]-a[0]);
  ctx.fillStyle='rgb('+color+')';ctx.fill();
  ctx.fillStyle=face?'rgba(12,27,42,.10)':'rgba(12,27,42,.29)';ctx.fill();
 }
}
function schedule(){if(!queued){queued=true;requestAnimationFrame(draw)}}
canvas.onpointerdown=e=>{pointer=e;canvas.setPointerCapture(e.pointerId)};
canvas.onpointerup=canvas.onpointercancel=()=>pointer=null;
canvas.onpointermove=e=>{if(pointer){yaw+=(e.clientX-pointer.clientX)/135;tilt=Math.max(-1.2,Math.min(1.2,tilt+(e.clientY-pointer.clientY)/135));pointer=e;schedule();}};
canvas.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.min(5,Math.max(.5,zoom*Math.exp(-e.deltaY/850)));schedule()},{passive:false});
get('showBody').oninput=get('showProxy').oninput=schedule;focus.onchange=()=>{zoom=1;schedule()};
new ResizeObserver(schedule).observe(canvas);update();</script></html>"""
    path.write_text(template.replace("PAYLOAD", json.dumps(payload, separators=(",", ":")), 1), encoding="utf-8")


def simulate_whole(root, output, levels, args):
    groups = sorted({int(path.parent.name[1:]) for path in (root / "res" / "drag_cloth").rglob("config.buf")
                     if re.fullmatch(r"g\d+", path.parent.name)})
    if not groups:
        raise ValueError("No exported wear groups found")
    results = [simulate(root, output / f"g{group}", group, levels, args) for group in groups]
    fixed = [result for result in results if result[0]["collision_only"]]
    matched = {zone for report, *_ in fixed for zone in report["zones"]}
    if set(args.collision_only or ()) - matched:
        raise ValueError("Collision-only IDs must cover a whole exported wear group")
    for stationary, fixed_faces, _, _, fixed_poses, _ in fixed:
        fixed_points = fixed_poses[0][0]
        for report, faces, _, _, poses, _ in results:
            if report["collision_only"]:
                continue
            for frame, (positions, _) in zip(report["frames"], poses):
                hits = crossing_edges(positions, faces, fixed_points[fixed_faces])
                hits += crossing_edges(fixed_points, fixed_faces, positions[faces])
                frame.setdefault("fixed_crossing_edges", {})[stationary["group"]] = hits
    body = body_mesh(root, results)
    points = np.concatenate([p for p, _ in body])
    payload = dict(name=root.name, levels=levels, center=((points.min(0) + points.max(0)) * .5).tolist(),
                   extent=float(np.ptp(points, axis=0).max()), groups=[],
                   regionCount=sum(len(result[0]["zones"]) for result in results))
    body_preview = [preview_mesh(points, faces, .006) for points, faces in body]
    payload["body"] = [[encode(v, "<f4"), encode(f, "<u4")] for v, f in body_preview]
    for report, faces, guide, rigid, poses, _ in results:
        rest = np.concatenate([positions for positions, _ in poses])
        payload["groups"].append(dict(group=report["group"], zones=report["zones"],
            fixed=report["collision_only"], faces=encode(faces, "<u4"),
            guideFaces=encode(guide, "<u4"), rigid=encode(rigid, "<u4"), metrics=report["frames"],
            frames=[[encode(p, "<f4"), encode(q, "<f4")] for p, q in poses],
            center=((rest.min(0) + rest.max(0)) * .5).tolist(), extent=float(np.ptp(rest, axis=0).max())))
    write_whole_preview(output / "preview.html", payload)
    for label, (vertices, faces) in enumerate(body):
        write_obj(output / f"body-{label}.obj", vertices, faces)
    reports = [result[0] for result in results]
    (output / "metrics.json").write_text(json.dumps(reports, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output / "preview.html")


def simulate(root, output, group, levels, args):
    world, faces, collider, cfg, zone, arrow, travel, controls, draw = load_garment(root, group, args.zone)
    travel = travel if args.travel is None else args.travel
    if not all(0 <= v <= 1 for v in levels) or not np.isfinite(travel) or travel <= 0:
        raise ValueError("Use a positive travel and progress values between 0 and 1")
    sliding = 1.5 < cfg[14, 3] < 2.5
    motion = args.motion or ("slide" if sliding else "compress")
    collision_only = set(controls).issubset(args.collision_only or ())
    active_levels = [0.0] * len(levels) if collision_only else levels
    up = unit(cfg[6, :3].astype(float))
    side = unit(cfg[7, :3] - np.dot(cfg[7, :3], up)*up)
    frame = np.column_stack((side, np.cross(up, side), up))
    pivot = world.mean(axis=0)
    vertices = (world - pivot) @ frame
    body_triangles = (collider - pivot) @ frame
    labels = islands(vertices, faces)
    lo, hi = vertices[:, 2].min(), vertices[:, 2].max()
    if zone in (args.limb_slide or ()):
        if collision_only or len(controls) != 1:
            raise ValueError("Limb slide requires one moving region per proxy")
        motion = "track"
        if args.travel is None:
            body_min, body_max = body_triangles[:, :, 2].min(), body_triangles[:, :, 2].max()
            travel = lo - (body_min + .06 * (body_max - body_min))
            if travel <= 0:
                raise ValueError("The collider does not extend below the garment")
    arrows = None
    if args.whole and len(controls) == 2 and all(control[1] > 0 and control[2] == 1 for control in controls.values()):
        vectors = np.array([direction * length for direction, length, _ in controls.values()]) @ frame
        arrows = vectors[np.argsort(-vectors[:, 0])]
    if motion == "compress" and max(active_levels) * (travel if arrows is None else max(-arrows[:, 2])) >= (hi-lo)*.97:
        raise ValueError("Compression would collapse the proxy's two ends. Use a shorter travel or --motion slide.")
    if args.rigid == "auto":
        rigid = [int(i) for i in np.unique(labels) if np.linalg.norm(np.ptp(vertices[labels==i],axis=0)) < (hi-lo)*.16]
    else:
        rigid = [] if args.rigid == "none" else [int(v) for v in args.rigid.split(",")]
        if set(rigid) - set(labels):
            raise ValueError("A requested rigid island does not exist")
    lower = lo - (travel * max(active_levels) if motion != "compress" else 0)
    span = np.ptp(vertices[:, 0]) * .48 if sliding or arrows is not None else None
    body = BodySections(body_triangles, vertices, lower - .001, hi + .001, 64, span, extend_lower=motion == "track")
    positive = body.gap(vertices)
    gap = max(float(cfg[2, 2]), .002)
    clearance = float(np.clip(np.median(np.maximum(positive, gap)), gap, .012))
    solver = SleeveSolver(body, lo, hi, 15, 40, clearance, travel, np.radians(args.twist), motion, arrows)
    followers = Followers(vertices, labels, solver, rigid)
    edges = np.unique(np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1), axis=0)
    a, b = edges.T
    base = np.linalg.norm(vertices[a] - vertices[b], axis=1)
    meaningful = base > 1e-5
    rigid_edges = ~followers.soft[edges].any(axis=1) & meaningful
    report = dict(group=f"g{group}", draw=draw, zone=zone, zones=list(controls) if arrows is not None else [zone],
                  collision_only=collision_only,
                  motion=motion, travel=travel, arrow=arrow.tolist(),
                  controls={index: {"vector": direction.tolist(), "travel": length, "mode": mode}
                            for index, (direction, length, mode) in controls.items()},
                  proxy_up=up.tolist(), twist_deg=args.twist, display_vertices=len(vertices),
                  display_triangles=len(faces), render_islands=len(np.unique(labels)),
                  proxy_vertices=len(solver.rest), proxy_edges=len(solver.edges), rigid_islands=rigid,
                  islands=[{"id": int(i), "vertices": int((labels == i).sum()),
                            "axis_span": float(np.ptp(vertices[labels == i,2]))} for i in np.unique(labels)], frames=[])
    output.mkdir(parents=True, exist_ok=False)
    preview_frames, poses = [], []
    previous_pose, previous_display = solver.rest, vertices
    upper = vertices[:, 2] >= hi - .10 * (hi - lo)
    for i, (progress, active) in enumerate(zip(levels, active_levels)):
        proxy_step = display_step = peak_ratio = extra_penetration = 0.0
        inverted = 0
        min_gap = float("inf")
        for pose in (solver.rest,) if collision_only else solver.advance(active):
            positions = vertices if collision_only else followers.publish(pose)
            if not (np.isfinite(positions).all() and np.isfinite(pose).all()):
                raise RuntimeError(f"Non-finite proxy or render positions at {progress:.0%}")
            lengths = np.linalg.norm(positions[a] - positions[b], axis=1)
            ratios = lengths[meaningful] / base[meaningful]
            proxy_step = max(proxy_step, float(np.linalg.norm(pose - previous_pose, axis=1).max()))
            display_step = max(display_step, float(np.linalg.norm(positions - previous_display, axis=1).max()))
            peak_ratio = max(peak_ratio, float(ratios.max()))
            inverted = max(inverted, solver.inverted_faces(pose))
            min_gap = min(min_gap, float(body.gap(pose).min()))
            extra_penetration = max(extra_penetration, float(np.maximum(followers.floor - body.gap(positions), 0).max()))
            previous_pose, previous_display = pose, positions
        metrics = dict(progress=progress, applied_progress=active,
                       upper_edge_displacement=float(np.mean((positions - vertices)[upper, 2])),
                       max_proxy_step=proxy_step, max_display_step=display_step,
                       min_proxy_body_gap=min_gap, max_inverted_proxy_faces=inverted,
                       peak_display_edge_ratio=peak_ratio, display_edge_ratio_p95=float(np.quantile(ratios, .95)),
                       display_edge_ratio_max=float(ratios.max()),
                       rigid_edge_length_error=float(np.max(np.abs(lengths[rigid_edges]-base[rigid_edges]), initial=0)),
                       extra_body_penetration=extra_penetration,
                       repeat_output_error=float(np.max(np.abs(positions-followers.publish(solver.current)))))
        if progress == 0:
            metrics["rest_display_error"] = float(np.linalg.norm(positions - vertices, axis=1).max())
        report["frames"].append(metrics)
        prefix = f"{i:02d}-{progress:.0%}"
        display, proxy = positions @ frame.T + pivot, solver.current @ frame.T + pivot
        write_obj(output / f"{prefix}-render.obj", display, faces)
        write_obj(output / f"{prefix}-proxy.obj", proxy, solver.faces)
        poses.append((display, proxy))
        preview_frames.append(dict(progress=progress, cloth=encode(positions,"<f4"),guide=encode(solver.current,"<f4")))
        print(json.dumps(metrics, ensure_ascii=False), flush=True)
    collision_preview = body.points(solver.rest[:,2], solver.theta, 0)
    write_preview(output/"preview.html", dict(group=report["group"],zone=zone,frames=preview_frames,
                  faces=encode(faces,"<u4"),guide_faces=encode(solver.faces,"<u4"),body=encode(collision_preview,"<f4"),
                  rigid=encode(~followers.soft,"<u4"),center=vertices.mean(0).tolist(),extent=float(np.ptp(vertices,axis=0).max()),
                  rigid_count=int((~followers.soft).sum()),soft_count=int(followers.soft.sum())))
    (output / "metrics.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(output)
    return report, faces, solver.faces, ~followers.soft, poses, collider


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mod", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--whole", action="store_true", help="Simulate all active wear groups together")
    parser.add_argument("--group", type=int, default=2, help="Exported cloth group number")
    parser.add_argument("--zone", type=int)
    parser.add_argument("--collision-only", type=int, action="append", help="Keep this exported zone stationary")
    parser.add_argument("--limb-slide", type=int, action="append", help="Follow the limb down to its lower usable section in this experiment")
    parser.add_argument("--levels", help="Normalized progress sequence")
    parser.add_argument("--travel", type=float, help="Override the exported arrow's travel in model units")
    parser.add_argument("--motion", choices=("compress", "slide"))
    parser.add_argument("--twist", type=float, default=0.0, help="Optional guide twist in degrees at full progress")
    parser.add_argument("--rigid", default="auto", help="auto, none, or comma-separated welded island IDs")
    args = parser.parse_args(argv)
    root = args.mod.resolve(strict=True)
    output = args.out.resolve()
    if output.is_relative_to(root):
        parser.error("The research output must be outside the live mod")
    if not np.isfinite(args.twist):
        parser.error("Use a finite twist angle")
    levels = args.levels or ("0,.25,.5,.75" if args.whole else "0,.25,.5,.75,1,0")
    levels = [float(v) for v in levels.split(",")]
    if args.whole:
        if args.zone is not None:
            parser.error("--whole uses all active regions without --zone")
        simulate_whole(root, output, levels, args)
    else:
        simulate(root, output, args.group, levels, args)


if __name__ == "__main__":
    main()
