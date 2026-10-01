/* 山火应急撤离编排系统 —— 前端逻辑（无框架，Leaflet 地图） */
let S = null;
const BATCH_COLORS = ["#ff7a45","#3b82f6","#34d399","#a78bfa","#fbbf24","#f472b6","#22d3ee"];
const THREAT_NAME = {burning:"大火逼近", threatened:"受威胁", watch:"观察", safe:"安全"};

// ---------- 通用 ----------
const $ = id => document.getElementById(id);
function toast(msg, kind="") {
  const d = document.createElement("div");
  d.className = kind; d.innerHTML = msg;
  $("toast").appendChild(d);
  setTimeout(()=>d.remove(), 6000);
}
async function api(path, opts={}) {
  const res = await fetch(path, {headers:{"Content-Type":"application/json"}, ...opts});
  const data = await res.json().catch(()=>({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}
function esc(s){ return String(s??"").replace(/[&<>"]/g, c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c])); }

// ---------- 标签页 ----------
document.querySelectorAll("nav button").forEach(b => b.onclick = () => {
  document.querySelectorAll("nav button").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  b.classList.add("active");
  $("tab-"+b.dataset.tab).classList.add("active");
  if (b.dataset.tab==="map") setTimeout(()=>map.invalidateSize(), 50);
});

// ---------- 地图 ----------
const map = L.map("map").setView([30.018, 120.128], 13);
L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
  {attribution:"© OpenStreetMap", maxZoom:18}).addTo(map);
// 离线底图时保留一个简洁的比例尺参照层
map.on("tileerror", () => { /* 底图不可用时仍可看到矢量要素 */ });
let layerGroup = L.layerGroup().addTo(map);

function edgeStyle(e){
  return e.status==="closed"
    ? {color:"#f87171", dashArray:"6 6", weight:3, opacity:.9}
    : {color:"#9aa3b2", weight:2 + (e.capacity>=3?2:0), opacity:.85};
}

function drawMap(){
  layerGroup.clearLayers();
  const node = id => S.nodes.find(n=>n.id===id);
  const bounds = [];

  // 网格威胁圆
  S.cells.forEach(c=>{
    const col = {burning:"#f87171",threatened:"#fbbf24",watch:"#60a5fa",safe:"#34d399"}[c.threat];
    const m = L.circle([c.lat,c.lng], {radius: c.threat==="burning"?900:700,
      color:col, fillColor:col, fillOpacity:.18, weight:2}).addTo(layerGroup);
    const hs = S.households.filter(h=>h.cell_id===c.id);
    m.bindPopup(`<b>${esc(c.name)}</b><br>${THREAT_NAME[c.threat]}<br>家庭 ${hs.length} 户`);
    bounds.push([c.lat,c.lng]);
  });

  // 道路
  S.edges.forEach(e=>{
    const a=node(e.a), b=node(e.b); if(!a||!b) return;
    const line = L.polyline([[a.lat,a.lng],[b.lat,b.lng]], edgeStyle(e)).addTo(layerGroup);
    line.bindPopup(`<b>${esc(e.name||e.id)}</b><br>
      ${e.status==="closed"?'<span class="bad">已封闭</span>':'开放'} ·
      每批次容量 ${e.capacity} 车次<br><span class="muted">${Math.round(e.length_m)} m</span>`);
    bounds.push([a.lat,a.lng],[b.lat,b.lng]);
  });

  // 避难点
  S.shelters.forEach(sh=>{
    const n = node(sh.node_id); if(!n) return;
    const used = sh.committed_beds;
    const m = L.marker([n.lat,n.lng], {
      icon: L.divIcon({className:"", iconSize:[22,22],
        html:`<div style="font-size:20px">${sh.accepting?"🏟️":"⛔"}</div>`})
    }).addTo(layerGroup);
    m.bindPopup(`<b>${esc(sh.name)}</b><br>
      床位 ${used}/${sh.beds} ${sh.accepting?'':'<span class="bad">暂停接收</span>'}<br>
      ${sh.accessible?'无障碍':'<span class="warn">不具备无障碍条件</span>'}<br>
      <span class="muted">${esc(sh.note)}</span>`);
  });

  // 车辆（按状态）
  S.vehicles.forEach(v=>{
    const n = node(v.home_node); if(!n) return;
    const col = v.status==="available" ? "#a78bfa" : v.status==="broken" ? "#f87171" : "#34d399";
    L.circleMarker([n.lat+0.0006,n.lng+0.0006], {radius:6,color:col,fillColor:col,fillOpacity:.9})
      .bindPopup(`<b>${esc(v.plate)}</b> ${esc(v.id)}<br>${v.seats} 座 ·
        ${v.accessible?'无障碍车':''}${v.status==="available"?"可用":v.status==="broken"?"故障":"已离场"}`)
      .addTo(layerGroup);
  });

  // 家庭：未出发灰点；已分配按批次着色；已出发⭐
  S.households.forEach((h,i)=>{
    const c = S.cells.find(x=>x.id===h.cell_id); if(!c) return;
    const ang = (i*2.399) % (2*Math.PI), r = 0.0011*(1+(i%3));
    const pos = [c.lat + Math.sin(ang)*r, c.lng + Math.cos(ang)*r];
    let color = "#9aa3b2", icon = "👤";
    if (h.departed){ color="#34d399"; icon="✅"; }
    else if (h.batch!=null) color = BATCH_COLORS[(h.batch-1)%BATCH_COLORS.length];
    const mk = L.marker(pos, {icon:L.divIcon({className:"", iconSize:[18,18],
      html:`<div style="font-size:13px;color:${color}">${icon}</div>`})}).addTo(layerGroup);
    mk.bindPopup(`<b>${esc(h.name)}</b> ${esc(h.id)}<br>${h.persons} 人
      ${h.needs_assist?'<span class="tag t-assist">行动辅助</span>':''}
      ${h.pets?`宠物 ${esc(h.pets)}`:""}<br>📞 ${esc(h.contact)}<br>
      ${h.departed?`<span class="ok">已出发 批次${h.batch} → ${esc(h.shelter_id)}</span>`
        : h.batch!=null?`已分配 批次${h.batch} → ${esc(h.shelter_id)}（车 ${esc(findVehPlate(h.order_id,h.id))}）`
        :'<span class="muted">未安排</span>'}`);
  });

  if (bounds.length) map.fitBounds(bounds, {padding:[30,30]});
}

function findVehPlate(orderId, hhId){
  const ao = S.active_order;
  if(!ao) return "";
  const a = ao.assignments.find(x=>x.household_id===hhId);
  const v = a && S.vehicles.find(x=>x.id===a.vehicle_id);
  return v ? v.plate : (a?a.vehicle_id:"");
}

// ---------- 侧栏列表 ----------
function renderMapCards(){
  $("cellList").innerHTML = S.cells.map(c=>{
    const hs = S.households.filter(h=>h.cell_id===c.id);
    const persons = hs.reduce((s,h)=>s+h.persons,0);
    return `<div class="row" style="justify-content:space-between;padding:3px 0">
      <span>${esc(c.name)}</span>
      <span><span class="tag t-${c.threat}">${THREAT_NAME[c.threat]}</span>
      <span class="muted">${hs.length}户/${persons}人</span></span></div>`;
  }).join("");

  $("shelterList").innerHTML = S.shelters.map(sh=>{
    const pct = Math.min(100, Math.round(100*sh.committed_beds/sh.beds));
    return `<div style="padding:4px 0">
      <div class="row" style="justify-content:space-between">
        <b>${esc(sh.name)}</b>
        <span>${sh.accepting?'<span class="tag t-accepting">接收中</span>':'<span class="tag t-paused">已暂停</span>'}
          ${sh.accessible?'<span class="tag t-assist">无障碍</span>':''}</span>
      </div>
      <div class="muted">剩余床位 <b class="${sh.remaining_beds===0?'bad':'ok'}">${sh.remaining_beds}</b> / ${sh.beds}
        （已承诺锁定 ${sh.committed_beds}）</div>
      <div class="shelterbar"><div class="${pct>=90?'full':''}" style="width:${pct}%"></div></div>
    </div>`;
  }).join("");

  $("vehicleList").innerHTML = S.vehicles.map(v=>
    `<div class="row" style="justify-content:space-between;padding:3px 0">
      <span>${esc(v.plate)} <span class="muted">${v.seats}座 ${v.accessible?'♿':''}</span></span>
      <span class="tag t-${v.status}">${{available:"可用",broken:"故障",departed:"已离场"}[v.status]}</span>
    </div>`).join("");
}

// ---------- 批次页 ----------
function renderBatches(){
  const ao = S.active_order;
  if(!ao){
    $("orderMeta").textContent = "（尚未发布，请在“方案比较”页生成并发布）";
    $("batchBoard").innerHTML = "";
    $("departAllBtn").disabled = true;
  } else {
    $("orderMeta").innerHTML = `命令 #${ao.order_id} · 方案 #${ao.plan_id} ·
      ${ao.profile} · ${esc(ao.published_by)} 于 ${ao.published_ts.slice(11,19)} 发布`;
    const plan = S.plans.find(p=>p.id===ao.plan_id);
    const maxB = Math.max(...ao.assignments.map(a=>a.batch));
    let html = "";
    for(let b=1;b<=maxB;b++){
      const items = ao.assignments.filter(a=>a.batch===b);
      if(!items.length) continue;
      const allDep = items.every(a=>S.households.find(h=>h.id===a.household_id)?.departed);
      const anyDep = items.some(a=>S.households.find(h=>h.id===a.household_id)?.departed);
      html += `<div class="batch-col" style="width:190px">
        <div class="row" style="justify-content:space-between">
          <b style="color:${BATCH_COLORS[(b-1)%7]}">批次 ${b}</b>
          ${allDep?'<span class="tag t-departed">已离场</span>'
            :`<button class="btn small" onclick="departBatch(${b})">发车离场</button>`}
        </div>
        <div class="batch-box" style="border-color:${BATCH_COLORS[(b-1)%7]}55">`;
      const byVeh = {};
      items.forEach(a=>{ (byVeh[a.vehicle_id] ||= []).push(a); });
      Object.entries(byVeh).forEach(([vid,as])=>{
        const v = S.vehicles.find(x=>x.id===vid);
        html += `<div class="muted" style="margin-top:4px">🚌 ${esc(v?v.plate:vid)}
          <button class="btn ghost small" style="float:right;padding:0 6px"
            ${as.every(a=>S.households.find(h=>h.id===a.household_id)?.departed)?"disabled":""}
            onclick="departVeh('${vid}')">单车发</button></div>`;
        as.forEach(a=>{
          const h = S.households.find(x=>x.id===a.household_id);
          const sh = S.shelters.find(x=>x.id===a.shelter_id);
          html += `<span class="hh-chip ${h.departed?"departed":""}">
            ${h.departed?"✅ ":""}${esc(h.name)} · ${h.persons}人
            ${h.needs_assist?'♿':''}<br><span class="muted">→ ${esc(sh?sh.name:a.shelter_id)}</span></span>`;
        });
      });
      html += `</div></div>`;
    }
    $("batchBoard").innerHTML = html;
    $("departAllBtn").disabled = !ao.assignments.some(a=>
      !S.households.find(h=>h.id===a.household_id)?.departed);
  }
  const dep = S.households.filter(h=>h.departed);
  $("departedTable").innerHTML =
    `<tr><th>出发时间</th><th>批次</th><th>家庭</th><th>人数</th><th>车辆</th><th>目的地</th></tr>`+
    dep.map(h=>{
      const a = ao && ao.assignments.find(x=>x.household_id===h.id);
      const v = a && S.vehicles.find(x=>x.id===a.vehicle_id);
      return `<tr class="departed"><td>${(h.departure_ts||"").replace("T"," ").slice(0,19)}</td>
        <td>${h.batch}</td><td>${esc(h.name)} ${h.needs_assist?'♿':''}</td><td>${h.persons}</td>
        <td>${esc(v?v.plate:(a&&a.vehicle_id))}</td><td>${esc(h.shelter_id)}</td></tr>`;
    }).join("");
  $("orderPill").textContent = ao ? `活动命令 #${ao.order_id}` : "无活动命令";
}

async function departBatch(b){
  try{
    const r = await api("/api/orders/depart",{method:"POST",body:JSON.stringify({batch:b})});
    toast(`批次 ${b} 已离场：${r.departed_households.length} 户，床位占用已承诺`,"okm");
    await loadAll();
  }catch(e){ toast("离场失败："+e.message,"err"); }
}
async function departVeh(vid){
  try{
    const r = await api("/api/orders/depart",{method:"POST",body:JSON.stringify({vehicle_id:vid})});
    toast(`车辆 ${vid} 批次 ${r.batches.join()} 已离场`,"okm");
    await loadAll();
  }catch(e){ toast("离场失败："+e.message,"err"); }
}
$("departAllBtn").onclick = async ()=>{
  const ao = S.active_order; if(!ao) return;
  const batches = [...new Set(ao.assignments.map(a=>a.batch))].sort((a,b)=>a-b);
  for(const b of batches){
    try { await api("/api/orders/depart",{method:"POST",body:JSON.stringify({batch:b})}); }
    catch(e){ /* 该批可能已全部离场 */ }
  }
  toast("所有待出发批次已依次离场","okm");
  await loadAll();
};

// ---------- 方案 ----------
$("runPlanBtn").onclick = async ()=>{
  $("runPlanBtn").disabled = true; $("runPlanBtn").textContent = "求解中…";
  try{
    const r = await api("/api/plans",{method:"POST",body:JSON.stringify({
      profile:$("profileSel").value, created_by:$("plannerName").value})});
    $("planResult").innerHTML = renderPlanResult(r, true);
    toast(`候选方案 #${r.plan_id} 生成：安置 ${r.assigned_count} 户，未安置 ${r.unassigned_count} 户`,"okm");
    await loadAll(true);
  }catch(e){ toast("规划失败："+e.message,"err"); }
  $("runPlanBtn").disabled = false; $("runPlanBtn").textContent = "运行规划（CP-SAT）";
};

function renderPlanResult(r, isNew){
  let html = `<div class="card ${r.feasible?'':'bad'}">
    <h3>方案 #${r.plan_id||""} 结果${isNew?"（新生成）":""}
      <span class="tag t-${r.optimal?'safe':'watch'}">${r.optimal?"已达最优":"可行解（限时）"}</span></h3>
    <p>成功安置 <b class="ok">${r.assigned_count}</b> 户 / 共 ${r.assigned_count+r.unassigned_count} 户待撤离；
      未安置 <b class="${r.unassigned_count?'bad':'ok'}">${r.unassigned_count}</b> 户。批次范围：
      ${r.batches[0]}–${r.batches[r.batches.length-1]}</p>`;
  if(r.batches_summary && r.batches_summary.length){
    html += `<table><tr><th>批次</th><th>户数</th><th>人数</th><th>车辆</th></tr>`+
      r.batches_summary.map(b=>`<tr><td style="color:${BATCH_COLORS[(b.batch-1)%7]}">批次 ${b.batch}</td>
        <td>${b.households}</td><td>${b.persons}</td><td>${b.vehicles.map(esc).join(", ")}</td></tr>`).join("")+
      `</table>`;
  }
  const d = r.diagnostics || {};
  if(d.bottlenecks && d.bottlenecks.length){
    html += `<h3 style="margin-top:10px">全局瓶颈</h3>`+
      d.bottlenecks.map(b=>`<div class="bottleneck">⚠️ ${esc(b)}</div>`).join("");
  }
  if(d.unassigned_households && d.unassigned_households.length){
    html += `<h3 style="margin-top:10px">无可行安置家庭及具体约束</h3>`+
      d.unassigned_households.map(h=>`<div style="margin:6px 0">
        <b>${esc(h.name)}</b> <span class="muted">${h.persons}人
        ${h.needs_assist?'♿ ':''}网格威胁：${THREAT_NAME[h.threat]||h.threat}</span>`+
        h.reasons.map(x=>{const [code,txt]=x.split(":");
          return `<div class="reason">▪ [${code}] ${esc(txt)}</div>`;}).join("")+`</div>`).join("");
  }
  return html+"</div>";
}

function renderPlans(){
  const ao = S.active_order;
  $("plansGrid").innerHTML = (S.plans||[]).map(p=>{
    const sm = p.summary || {};
    const isPub = p.status==="published";
    const stale = isPub && ao && p.id===ao.plan_id && p.base_version!==S.version;
    return `<div class="card plan-card ${isPub?'published':''}">
      <span class="badge tag t-${isPub?'accepting':'watch'}">${{published:"已发布",candidate:"候选",superseded:"已失效"}[p.status]}</span>
      <h3>方案 #${p.id}</h3>
      <div class="muted">${p.created_ts.replace("T"," ").slice(0,19)} · ${esc(p.created_by)}
        · 画像 ${p.profile}<br>基线版本 v${p.base_version}
        ${p.base_version!==S.version?'<span class="warn">（场景已变更到 v'+S.version+'，发布将被拒绝）</span>':""}
        ${isPub&&stale?'<br><span class="warn">命令发布后场景已变化；已出发部分不受影响，未出发家庭请重算</span>':''}</div>
      <p style="margin:8px 0">安置 <b class="ok">${p.num_assigned}</b> 户，
        未安置 <b class="${p.unassigned?'bad':''}">${p.unassigned}</b> 户 · 起始批次 ${p.start_batch}</p>
      <p class="muted" style="margin:4px 0">
        ${(sm.batches_summary||[]).map(b=>`第${b.batch}批 ${b.persons}人`).join("；")||"—"}</p>
      <div class="row" style="margin-top:8px">
        ${p.status==="candidate"
          ? `<button class="btn small" onclick="publishPlan(${p.id})">发布分区撤离令</button>
             <span class="muted" id="pubmsg${p.id}"></span>`
          : isPub ? `<span class="ok">✔ 当前活动命令</span>` : `<span class="muted">已被取代</span>`}
      </div></div>`;
  }).join("") || `<p class="muted">暂无候选方案。</p>`;

  // 已发布方案也展示详情
  if(ao){
    const p = S.plans.find(x=>x.id===ao.plan_id);
    if(p && !$("planResult").dataset.published){
      // 不覆盖刚生成的求解结果
    }
  }
}

async function publishPlan(id){
  if(!confirm(`确认发布方案 #${id} 为分区撤离令？\n发布后其它候选/旧命令立即失效。`)) return;
  const btn = event.target; btn.disabled = true;
  try{
    const r = await api(`/api/plans/${id}/publish`,{method:"POST",
      body:JSON.stringify({published_by:$("plannerName").value, expected_version:S.version})});
    toast(`撤离令 #${r.order_id} 已发布${r.superseded_plan?`（取代方案 #${r.superseded_plan}）`:""}`,"okm");
    await loadAll();
  }catch(e){
    toast("发布被拒绝："+e.message+"<br>请重新生成候选方案后再发布。","err");
    btn.disabled = false;
  }
}

// ---------- 资源名单 ----------
function renderResources(){
  $("hhTable").innerHTML =
    `<tr><th>ID</th><th>户主</th><th>人数</th><th>辅助</th><th>宠物</th><th>联络</th>
     <th>网格</th><th>状态</th></tr>`+
    S.households.map(h=>{
      const c = S.cells.find(x=>x.id===h.cell_id);
      const st = h.departed
        ? `<span class="tag t-departed">已出发 批${h.batch} → ${esc(h.shelter_id)}</span>`
        : h.batch!=null
          ? `<span class="tag t-watch">待出发 批${h.batch} → ${esc(h.shelter_id)}</span>`
          : '<span class="muted">未安排</span>';
      return `<tr class="${h.departed?'departed':''}"><td>${esc(h.id)}</td>
        <td>${esc(h.name)}</td><td>${h.persons}</td>
        <td>${h.needs_assist?'<span class="tag t-assist">需要</span>':''}</td>
        <td>${esc(h.pets)}</td><td>${esc(h.contact)}</td>
        <td>${esc(c?c.name:h.cell_id)}<br><span class="tag t-${c?c.threat:'watch'}">${c?THREAT_NAME[c.threat]:''}</span></td>
        <td>${st}</td></tr>`;
    }).join("");

  $("edgeTable").innerHTML =
    `<tr><th>ID</th><th>道路</th><th>端点</th><th>状态</th><th>每批次容量</th><th>长度</th></tr>`+
    S.edges.map(e=>`<tr><td>${esc(e.id)}</td><td>${esc(e.name)}</td>
      <td>${esc(e.a)} ↔ ${esc(e.b)}</td>
      <td><span class="tag t-${e.status}">${e.status==='open'?'开放':'封闭'}</span></td>
      <td>${e.capacity}</td><td>${Math.round(e.length_m)} m</td></tr>`).join("");
}

// ---------- 事件 / 导入 ----------
function renderEventTab(){
  $("evRoad").innerHTML = S.edges.map(e=>`<option value="${e.id}">${esc(e.name||e.id)}（${e.status}）</option>`).join("");
  $("evVeh").innerHTML = S.vehicles.map(v=>`<option value="${v.id}">${esc(v.plate)}（${v.status}）</option>`).join("");
  $("evShel").innerHTML = S.shelters.map(s=>`<option value="${s.id}">${esc(s.name)}（${s.accepting?'接收中':'暂停'}）</option>`).join("");
  $("evCell").innerHTML = S.cells.map(c=>`<option value="${c.id}">${esc(c.name)}（${c.threat}）</option>`).join("");
  $("eventLog").innerHTML = (S.events||[]).map(e=>
    `<div style="padding:3px 0;border-bottom:1px solid var(--line)">
      <span class="muted">${e.ts.slice(11,19)}</span>
      <span class="tag t-${e.kind==='publish'||e.kind==='depart'?'accepting':'watch'}">${e.kind}</span>
      ${esc(e.detail)}</div>`).join("");
}
async function roadEvent(){
  await api("/api/events/road",{method:"POST",body:JSON.stringify(
    {edge_id:$("evRoad").value,status:$("evRoadStatus").value})});
  toast("道路状态已更新，候选方案版本基线已失效，请重新规划","okm"); loadAll();
}
async function vehEvent(){
  await api("/api/events/vehicle",{method:"POST",body:JSON.stringify(
    {vehicle_id:$("evVeh").value,status:$("evVehStatus").value})});
  toast("车辆状态已更新","okm"); loadAll();
}
async function shelEvent(){
  await api("/api/events/shelter",{method:"POST",body:JSON.stringify(
    {shelter_id:$("evShel").value,accepting:$("evShelStatus").value==="true"})});
  toast("避难点接收状态已更新","okm"); loadAll();
}
async function fireEvent(){
  await api("/api/events/fire",{method:"POST",body:JSON.stringify(
    {cell_id:$("evCell").value,threat:$("evCellThreat").value})});
  toast("火线态势已更新：仅未出发家庭可被重新规划","okm"); loadAll();
}
async function resetDemo(){
  if(!confirm("将清空全部方案/命令/出发记录并恢复初始演示数据，确认？")) return;
  await api("/api/admin/reset",{method:"POST"});
  toast("已重置演示数据","okm"); loadAll();
}
function fillImportExample(){
  $("importJson").value = JSON.stringify({
    households:[{id:"H12",name:"测试户",persons:3,needs_assist:false,pets:"犬1",
      contact:"13900000000",cell_id:"C2",pickup_node:"N2"}],
    edges:[{id:"E13",a:"N3",b:"N8",name:"新开避险通道",status:"open",capacity:2,length_m:2600}]
  }, null, 2);
}
async function doImport(){
  let body;
  try{ body = JSON.parse($("importJson").value); }
  catch(e){ return toast("JSON 解析失败："+e.message,"err"); }
  try{
    const r = await api("/api/import",{method:"POST",body:JSON.stringify(body)});
    toast("导入完成："+JSON.stringify(r),"okm"); loadAll();
  }catch(e){ toast("导入失败："+e.message,"err"); }
}

// ---------- 主加载 ----------
async function loadAll(keepPlanResult){
  S = await api("/api/state");
  $("versionPill").textContent = "场景 v"+S.version;
  drawMap(); renderMapCards(); renderBatches(); renderResources(); renderEventTab();
  renderPlans();
}
loadAll();
