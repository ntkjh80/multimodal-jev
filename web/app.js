/* 빌드 도구 없이 동작하는 화면 계층.
 * 서버가 검증·실행·채점을 담당하고 브라우저는 설정과 결과만 표시합니다.
 * 사용자 입력을 HTML에 넣을 때는 반드시 esc()를 거쳐 스크립트 삽입을 막습니다.
 */
"use strict";
const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));
const state = {data:null, run:null, tab:"analysis", sample:null, filter:"all", timer:null, route:"overview"};
const names = {overview:"대시보드", models:"모델 연결", datasets:"데이터셋", experiments:"벤치마크 실행", results:"실험 결과"};
const modeNames = {text:"텍스트",image:"이미지",audio:"오디오",video:"영상",mixed:"복합"};
const statusNames = {queued:"대기",running:"실행 중",stopping:"중단 대기",paused:"일시 중단",interrupted:"재개 가능",completed:"완료",error:"오류"};
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const number = (value, digits=1) => value == null ? "—" : Number(value).toLocaleString("ko-KR",{maximumFractionDigits:digits});
const pct = value => value == null ? "—" : number(value*100,1)+"%";
const ms = value => value == null ? "—" : number(value,1)+" ms";
const price = value => value == null ? "—" : "$"+Number(value).toFixed(6);
const pill = (text, color="") => '<span class="pill '+color+'">'+esc(text)+'</span>';
const statusPill = status => pill(statusNames[status] || status, status==="completed"?"green":status==="error"?"red":"blue");

async function api(path, body) {
  const response = await fetch(path, body === undefined ? {} : {
    method:"POST", headers:{"Content-Type":"application/json","X-Bench-Token":state.data.csrf}, body:JSON.stringify(body)
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "요청에 실패했습니다.");
  return data;
}
function toast(message) {
  const el=$("#toast"); el.textContent=message; el.classList.add("visible");
  clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.classList.remove("visible"),4500);
}
async function action(button, fn) {
  if (button) button.disabled=true;
  try { await fn(); } catch(error) { toast(error.message); }
  finally { if(button) button.disabled=false; }
}
function heading(title, subtitle, buttons="") {
  return '<div class="page-head"><div><div class="eyebrow">MULTIMODAL BENCH</div><h1>'+title+'</h1><p>'+subtitle+'</p></div><div class="row">'+buttons+'</div></div>';
}
function empty(title, detail, button="") {
  return '<div class="empty"><div class="empty-icon">⊞</div><h3>'+title+'</h3><p>'+detail+'</p>'+button+'</div>';
}
function stat(label, value, detail) {
  return '<div class="stat"><div class="stat-label">'+label+'<span>↗</span></div><strong>'+value+'</strong><small>'+detail+'</small></div>';
}
function formField(label, input, hint="", full=false) {
  return '<label class="field '+(full?"full":"")+'">'+label+input+(hint?'<small>'+hint+'</small>':"")+'</label>';
}
function modal(title, content) {
  $("#dialog-content").innerHTML='<div class="dialog-head"><h2 id="dialog-title">'+title+'</h2><button class="btn" data-close aria-label="닫기">✕</button></div>'+content;
  $("[data-close]").onclick=()=>$("#dialog").close();
  if(!$("#dialog").open) $("#dialog").showModal();
}
async function refresh() {
  state.data=await api("/api/bootstrap");
  $("#model-count").textContent=state.data.models.length;
}

/* 주소 해시를 사용하므로 새로고침이나 브라우저 뒤로 가기에도 현재 화면을 유지합니다. */
async function route() {
  clearTimeout(state.timer);
  const [page,id]=(location.hash.slice(1)||"overview").split("/");
  state.route=names[page]?page:"overview";
  $$("[data-nav]").forEach(a=>a.classList.toggle("active",a.dataset.nav===state.route));
  $("#breadcrumb").textContent=names[state.route];
  try {
    await refresh();
    if(state.route==="results" && id) {
      state.run=await api("/api/runs/"+encodeURIComponent(id)); renderResult(); pollRun(id);
    } else {
      state.run=null;
      ({overview:renderOverview,models:renderModels,datasets:renderDatasets,experiments:renderExperiments,results:renderRunList})[state.route]();
    }
  } catch(error) { $("#main").innerHTML=empty("연결을 확인하세요",esc(error.message)); }
}
function runTable(runs) {
  if(!runs.length) return empty("첫 실험을 시작해 보세요","모델과 데이터셋을 고르면 토큰, 시간, 정확도가 여기에 기록됩니다.",'<a class="btn" href="#experiments">벤치마크 설정 →</a>');
  return '<div class="table-wrap"><table><thead><tr><th>실험</th><th>데이터셋</th><th>진행</th><th>상태</th><th></th></tr></thead><tbody>'+runs.map(r=>'<tr><td><strong>'+esc(r.name)+'</strong> '+(r.demo?pill("DEMO","orange"):"")+'<div class="small muted">'+new Date(r.created_at).toLocaleString("ko-KR")+'</div></td><td>'+esc(r.dataset_name)+'</td><td>'+r.completed+' / '+r.total+'</td><td>'+statusPill(r.status)+'</td><td><a href="#results/'+esc(r.id)+'">결과 ↗</a></td></tr>').join("")+'</tbody></table></div>';
}
function renderOverview() {
  const d=state.data;
  const configured=d.models.filter(m=>m.readiness==="configured").length;
  $("#main").innerHTML=heading("모델의 차이를, 직접 확인하세요.","이미지 · 오디오 · 영상의 판단 성능과 실행 효율을 한곳에서 비교합니다.",'<a class="btn primary" href="#experiments">＋ 새 벤치마크</a>')+
    '<div class="grid four stats">'+stat("등록된 모델",d.models.length,"실제 모델 설정 완료 "+configured+"개 · 연결은 실행 시 확인")+
    stat("데이터셋",d.datasets.length,"공개 포맷 변환 및 자체 데이터 지원")+stat("실험 기록",d.runs.length,"설정과 결과 스냅샷 보존")+
    stat("완료된 관측치",d.runs.reduce((s,r)=>s+r.completed,0),"성공 · 오류 · 미지원 기록 포함")+'</div>'+
    '<div class="hero"><div><div class="eyebrow">REPRODUCIBLE BY DESIGN</div><h2>같은 질문으로 비교하고,<br>결과의 차이를 살펴보세요.</h2><p>API 키가 없어도 데모로 실험 흐름을 확인할 수 있습니다.<br>데모의 확률과 토큰은 모의 값으로 명확하게 표시합니다.</p><div class="row"><button id="demo-run" class="btn primary">데모 실험 실행 <span>→</span></button><a class="btn ghost" href="/guide.html" target="_blank" rel="noopener">사용 방법 보기 ↗</a></div></div>'+
    '<div class="hero-art" aria-hidden="true"><svg viewBox="0 0 270 170"><path d="M38 43H100Q125 43 125 77H157M38 85H157M38 127H100Q125 127 125 93H157" fill="none" stroke="#afc3f5" stroke-width="2"/><rect x="5" y="23" width="65" height="40" rx="9" fill="white"/><rect x="5" y="65" width="65" height="40" rx="9" fill="white"/><rect x="5" y="107" width="65" height="40" rx="9" fill="white"/><path d="M25 49l9-13 7 8 7-5 7 10Z" fill="#91abea"/><path d="M26 81v8m6-13v18m6-23v28m6-20v12m6-8v4" stroke="#86b8bc" stroke-width="3"/><path d="M31 119l14 8-14 8Z" fill="#a497d6"/><rect x="153" y="40" width="100" height="92" rx="16" fill="#244fdd"/><path d="M173 108V87h12v21m9 0V72h12v36m9 0V60h12v48" fill="#acc2ff"/><circle cx="245" cy="41" r="15" fill="#70ccb9"/><path d="m239 41 4 4 8-9" fill="none" stroke="white" stroke-width="3"/></svg></div></div>'+
    '<div class="grid two dashboard-lower"><section class="panel"><div class="panel-head"><div><h2>최근 실험</h2><p>측정 조건부터 개별 응답까지 다시 확인하세요.</p></div><a class="small" href="#results">전체 보기 →</a></div>'+runTable(d.runs.slice(0,4))+'</section>'+
    '<section class="panel"><div class="panel-head"><div><h2>실험 준비</h2><p>설정은 세 단계로 진행합니다.</p></div>'+pill("GET STARTED","blue")+'</div><div class="steps">'+
    [["01","모델 연결","API 주소와 모델 ID를 설정합니다.","models"],["02","데이터셋 등록","정답·선택지와 미디어를 연결합니다.","datasets"],["03","같은 조건으로 실행","반복 수와 동시 요청 수를 고릅니다.","experiments"]].map(s=>'<a class="step" href="#'+s[3]+'"><span class="step-number">'+s[0]+'</span><div><h3>'+s[1]+'</h3><p>'+s[2]+'</p></div></a>').join("")+'</div></section></div>';
  $("#demo-run").onclick=e=>action(e.currentTarget,async()=>{
    const r=await api("/api/runs",{dataset_id:"demo-dataset",model_ids:["demo"],name:"첫 데모 · 차량 명령",repeats:2,concurrency:1});
    location.hash="results/"+r.id;
  });
}

/* 모델 편집에는 키 문자열 입력란이 없습니다. 비밀값은 서버 .env에서 읽습니다. */
function renderModels() {
  const readiness={demo:"모의 응답",configured:"설정 완료 · 연결 미검증",key_missing:"키 설정 필요",model_missing:"모델명 필요",endpoint_missing:"주소 설정 필요"};
  $("#main").innerHTML=heading("모델 연결","호스팅 API와 로컬 모델 서버를 같은 평가 흐름에 연결합니다.",'<button id="add-model" class="btn primary">＋ 모델 추가</button>')+
    '<div class="notice">비밀키는 프로젝트의 <code>.env</code>에 저장하세요. 이 화면에는 환경변수 이름만 입력합니다. 설정 변경 후 서버를 재시작하면 .env가 적용됩니다.</div>'+
    '<div class="grid three">'+state.data.models.map(m=>'<article class="panel model-card"><div class="row between"><span class="model-icon">'+(m.provider==="demo"?"◈":"⬡")+'</span>'+pill(readiness[m.readiness],m.readiness==="demo"?"orange":m.readiness==="configured"?"green":"")+'</div><h3>'+esc(m.name)+'</h3><div class="mono muted">'+esc(m.model||"모델 이름 미입력")+'</div><p>'+esc(m.description)+'</p><div class="chips">'+m.modalities.map(k=>pill(modeNames[k])).join("")+'</div><button class="btn" data-edit-model="'+esc(m.id)+'">연결 설정 →</button></article>').join("")+
    '</div><div class="notice section-gap">실제 API 호출은 벤치마크 실행 버튼을 눌렀을 때 발생합니다. 외부 모델을 선택하면 입력 자료가 해당 제공자로 전송됩니다. 로컬 GPU worker의 설치는 사용 설명을 참고하세요.</div>';
  $("#add-model").onclick=()=>editModel();
  $$("[data-edit-model]").forEach(b=>b.onclick=()=>editModel(state.data.models.find(m=>m.id===b.dataset.editModel)));
}
function editModel(model={}) {
  const field=(key,type="text",placeholder="")=>'<input name="'+key+'" type="'+type+'" value="'+esc(model[key]??"")+'" placeholder="'+esc(placeholder)+'" '+(type==="number"?'step="any" min="0"':"")+'>';
  modal(model.id?"모델 설정":"새 모델 연결",'<form id="model-form"><div class="form-grid">'+
    formField("표시 이름",field("name"))+formField("연결 방식",'<select name="provider">'+["systemone","typesafe","cloudflare","openai","ollama","demo"].map(p=>'<option '+(p===(model.provider||"systemone")?"selected":"")+'>'+p+'</option>').join("")+'</select>')+
    formField("모델 ID",field("model"),"제공자가 지정한 정확한 모델 이름")+
    formField("버전 / revision",field("revision"),"기록용 필드입니다. 실제 서버의 체크포인트도 고정하세요.")+
    formField("전체 API 주소",field("endpoint","url","http://127.0.0.1:9010/v1/systemone"),"Cloudflare는 비워두면 .env의 계정 ID를 사용합니다.",true)+
    formField("키 환경변수 이름",field("key_env","text","TYPESAFE_API_KEY"),"키 자체를 입력하지 마세요.")+
    formField("시간 제한 (초)",'<input name="timeout" type="number" min="1" max="600" value="'+(model.timeout||120)+'">')+
    formField("입력 $ / 100만 토큰",field("input_price","number"),"미입력은 비용 미측정")+
    formField("출력 $ / 100만 토큰",field("output_price","number"),"무료 출력이면 명시적으로 0 입력")+
    formField("캐시 입력 $ / 100만 토큰",field("cached_input_price","number"))+
    formField("가격 확인일",field("price_date","date"))+
    '<div class="full"><div class="small muted">이 연결에서 실제로 지원하는 입력</div><div class="row wrap">'+Object.entries(modeNames).filter(([k])=>k!=="mixed").map(([k,v])=>'<label class="row small"><input name="modality" type="checkbox" value="'+k+'" '+((model.modalities||["text"]).includes(k)?"checked":"")+'>'+v+'</label>').join("")+'</div></div>'+
    formField("설명",'<textarea name="description">'+esc(model.description||"")+'</textarea>',"",true)+
    '</div><div class="dialog-actions"><button class="btn primary" type="submit">설정 저장</button></div></form>');
  $("#model-form").onsubmit=e=>{e.preventDefault();action($('button[type="submit"]',e.target),async()=>{
    const data=Object.fromEntries(new FormData(e.target)); data.modalities=new FormData(e.target).getAll("modality"); data.id=model.id||"";
    await api("/api/models",data); $("#dialog").close();await route();toast("모델 설정을 저장했습니다.");
  });};
}

function renderDatasets() {
  $("#main").innerHTML=heading("데이터셋","정답이 있는 평가 묶음과 직접 확인할 자료를 등록합니다.",'<a class="btn" href="/api/template">양식 다운로드 ↓</a><button class="btn primary" id="compose">＋ 직접 만들기</button>')+
    '<div class="grid two"><section class="panel"><div class="panel-head"><div><h2>평가 묶음 가져오기</h2><p>JSON · JSONL · CSV / 최대 10,000개 샘플</p></div></div><div class="drop-zone"><label>데이터셋 파일 선택<input id="dataset-file" type="file" accept=".json,.jsonl,.ndjson,.csv"></label><p>미디어가 있으면 오른쪽에서 먼저 업로드하세요.</p></div></section>'+
    '<section class="panel"><div class="panel-head"><div><h2>미디어 업로드</h2><p>파일당 최대 20MiB · 원본 보존</p></div></div><div class="drop-zone"><label>이미지 · 오디오 · 영상 선택<input id="media-files" type="file" multiple accept=".png,.jpg,.jpeg,.webp,.wav,.mp3,.mp4,.webm"></label><p>PNG/JPEG/WebP · WAV/MP3 · MP4/WebM</p></div></section></div>'+
    '<section class="panel section-gap"><div class="panel-head"><h2>등록된 데이터셋</h2>'+pill(state.data.datasets.length+" DATASETS","blue")+'</div>'+
    state.data.datasets.map(d=>'<div class="dataset-card"><div><h3>'+esc(d.name)+'</h3><p>'+esc(d.description)+'</p><div class="chips">'+pill(d.size+"개 샘플")+pill(d.split)+(Object.entries(d.counts||{}).map(([k,v])=>pill((modeNames[k]||k)+" "+v)).join(""))+'</div></div><button class="btn" data-dataset="'+esc(d.id)+'">내용 보기</button></div>').join("")+'</section>'+
    '<section class="panel section-gap"><div class="panel-head"><h2>업로드한 미디어</h2><span class="small muted">동일 이름 중복 업로드 시 asset ID로 연결하세요</span></div>'+
    (state.data.assets.length?'<div class="asset-list">'+state.data.assets.map(a=>'<button class="asset-item" data-asset="'+esc(a.id)+'">'+esc(a.name)+' <span class="muted">'+number(a.size/1024,0)+' KB</span></button>').join("")+'</div>':empty("아직 미디어가 없습니다","텍스트 평가에는 미디어 업로드가 필요하지 않습니다."))+'</section>';
  $("#compose").onclick=composeDataset;
  $("#dataset-file").onchange=e=>action(e.target,async()=>{
    const file=e.target.files[0];if(!file)return;
    await api("/api/datasets",{text:await file.text(),filename:file.name});await route();toast("데이터셋을 등록했습니다.");
  });
  $("#media-files").onchange=e=>action(e.target,async()=>{
    const files=Array.from(e.target.files);
    for(const file of files) {
      if(file.size>20*1024*1024)throw new Error(file.name+": 20MiB를 초과했습니다.");
      const uri=await readDataUrl(file);
      await api("/api/assets",{name:file.name,data:uri.split(",")[1]});toast(file.name+" 업로드 완료");
    }
    await route();
  });
  $$("[data-dataset]").forEach(b=>b.onclick=()=>action(b,async()=>{
    const d=await api("/api/datasets/"+b.dataset.dataset);
    modal("데이터셋 살펴보기",'<h3>'+esc(d.name)+'</h3><p class="small muted">'+esc(d.source)+' · '+esc(d.split)+' · '+d.samples.length+'개</p><pre>'+esc(JSON.stringify(d.samples.slice(0,5),null,2))+'</pre><div class="small muted">앞 5개 샘플 표시 · SHA256 '+esc(d.sha256)+'</div>');
  }));
  $$("[data-asset]").forEach(b=>b.onclick=()=>{const a=state.data.assets.find(a=>a.id===b.dataset.asset);modal("미디어",'<h3>'+esc(a.name)+'</h3>'+mediaPreview([a.id])+'<pre>'+esc(a.id)+'</pre>');});
}
function readDataUrl(file) {
  return new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result);r.onerror=()=>reject(new Error("파일 읽기 실패"));r.readAsDataURL(file);});
}
function composeDataset() {
  modal("직접 평가 샘플 만들기",'<form id="compose-form"><div class="form-grid">'+
    formField("데이터셋 이름",'<input name="name" required placeholder="예: 경고 화면 확인">',"",true)+
    formField("상황 / 텍스트",'<textarea name="text" placeholder="필요한 상황이나 발화 내용을 입력하세요."></textarea>',"",true)+
    formField("질문",'<input name="question" required placeholder="화면에 오류 메시지가 있나요?">',"",true)+
    formField("선택지 · 한 줄에 ID:설명",'<textarea name="choices">yes:예\nno:아니요</textarea>')+
    formField("정답 ID (선택)",'<input name="label" placeholder="yes">',"정답이 없으면 시간·토큰만 측정합니다.")+
    formField("미디어 연결",'<select name="assets" multiple size="4">'+state.data.assets.map(a=>'<option value="'+esc(a.id)+'">'+esc(a.name)+'</option>').join("")+'</select>',"여러 파일: Ctrl(Windows) / Command(Mac) 클릭","full")+
    '</div><div class="dialog-actions"><button class="btn primary" type="submit">샘플 저장</button></div></form>');
  $("#compose-form").onsubmit=e=>{e.preventDefault();action($('button[type="submit"]',e.target),async()=>{
    const f=new FormData(e.target), choices={};
    for(const line of f.get("choices").split("\n").filter(x=>x.trim())) {
      const split=line.indexOf(":");if(split<1)throw new Error("선택지는 ID:설명 형식으로 작성하세요.");
      const id=line.slice(0,split).trim();if(Object.hasOwn(choices,id))throw new Error("선택지 ID가 중복됩니다.");choices[id]=line.slice(split+1).trim();
    }
    await api("/api/datasets",{name:f.get("name"),samples:[{id:"sample-1",text:f.get("text"),question:f.get("question"),choices,label:f.get("label")||null,assets:f.getAll("assets")}]});
    $("#dialog").close();await route();toast("데이터셋을 저장했습니다.");
  });};
}
function mediaPreview(ids) {
  return '<div class="media-preview">'+ids.map(id=>{
    const a=state.data.assets.find(a=>a.id===id);if(!a)return '<span class="muted">원본 미디어 없음</span>';
    const src="/media/"+encodeURIComponent(id);
    return a.kind==="image"?'<img src="'+src+'" alt="'+esc(a.name)+'">':a.kind==="audio"?'<audio controls preload="metadata" src="'+src+'"></audio>':'<video controls preload="metadata" src="'+src+'"></video>';
  }).join("")+'</div>';
}

function renderExperiments() {
  $("#main").innerHTML=heading("벤치마크 실행","데이터셋과 모델을 고르고 재현할 수 있는 조건을 저장합니다.")+
    '<form id="run-form"><div class="grid two"><section class="panel"><div class="panel-head"><h2>01 · 데이터와 모델</h2>'+pill("INPUT","blue")+'</div><div class="form-grid">'+
    formField("실험 이름",'<input name="name" placeholder="예: 한국어 의도 분류 · 1차">',"",true)+
    formField("데이터셋",'<select name="dataset_id">'+state.data.datasets.map(d=>'<option value="'+esc(d.id)+'">'+esc(d.name)+' ('+d.size+')</option>').join("")+'</select>',"",true)+
    formField("입력 형식",'<select name="modality"><option value="all">전체</option>'+Object.entries(modeNames).map(([k,v])=>'<option value="'+k+'">'+v+'</option>').join("")+'</select>')+
    formField("샘플 제한",'<input name="limit" type="number" min="0" max="10000" value="0">',"0 = 전체. seed로 순서를 고정합니다.")+
    '<div class="full"><p class="small muted" style="margin-bottom:8px">비교 모델</p><div class="model-options">'+state.data.models.map(m=>'<label class="check-card"><input type="checkbox" name="model_ids" value="'+esc(m.id)+'" '+(m.id==="demo"?"checked":"")+'><span class="check-content"><strong>'+esc(m.name)+'</strong><small>'+esc(m.provider)+' · '+(m.readiness==="configured"?"설정 완료":m.readiness==="demo"?"모의 응답":"설정 필요")+'</small></span></label>').join("")+'</div></div></div></section>'+
    '<section class="panel"><div class="panel-head"><h2>02 · 측정 조건</h2>'+pill("PROTOCOL","blue")+'</div><div class="form-grid">'+
    formField("동시 요청 수",'<select name="concurrency"><option>1</option><option>4</option><option>8</option><option>16</option></select>',"한 모델 안에서의 동시 요청 수")+
    formField("반복 횟수",'<input name="repeats" type="number" min="1" max="10" value="1">',"정확도는 첫 반복, 시간은 모든 반복")+
    formField("워밍업 요청 수",'<input name="warmup" type="number" min="0" max="5" value="0">',"측정 통계에서 제외. API 비용은 발생할 수 있음")+
    formField("샘플 순서 seed",'<input name="seed" type="number" value="42">')+
    formField("실행 환경 메모",'<textarea name="hardware_note" placeholder="예: GPU H200 / BF16 / worker revision / API 리전"></textarea>',"로컬 웹서버와 원격 모델 서버의 환경을 구분해서 기록하세요.",true)+
    '</div><div class="notice">모델은 순차 실행합니다. 요청당 질문은 1개이며 자동 재시도와 예측 캐시는 사용하지 않습니다. 제공자 내부 캐시는 통제하지 않습니다.</div><div class="row wrap"><button class="btn primary" type="submit">벤치마크 시작 →</button><button class="btn" type="button" id="sweep">동시 요청 1 · 4 · 16 비교</button></div><p class="result-note">외부 모델에는 평가 자료가 전송되며 사용량에 따라 비용이 발생할 수 있습니다.</p></section></div></form>';
  const submit=async(sweep=false)=>{
    const f=new FormData($("#run-form")),data=Object.fromEntries(f);data.model_ids=f.getAll("model_ids");
    if(!data.model_ids.length)throw new Error("모델을 선택하세요.");
    const levels=sweep?[1,4,16]:[Number(data.concurrency)];
    let last;
    for(const c of levels)last=await api("/api/runs",{...data,concurrency:c,name:(data.name||"벤치마크")+(sweep?" · 동시 "+c:"")});
    toast(levels.length+"개 실험을 등록했습니다.");location.hash="results/"+last.id;
  };
  $("#run-form").onsubmit=e=>{e.preventDefault();action($('button[type="submit"]',e.target),()=>submit());};
  $("#sweep").onclick=e=>action(e.currentTarget,()=>submit(true));
}

function renderRunList() {
  $("#main").innerHTML=heading("실험 결과","데이터셋·모델·실행 조건별 결과를 열고 비교합니다.",'<a class="btn primary" href="#experiments">＋ 새 벤치마크</a>')+
    '<section class="panel">'+runTable(state.data.runs)+'</section>';
}
function bars(rows,key,formatter) {
  const max=Math.max(...rows.map(r=>r[key]||0),1);
  return rows.map(r=>'<div class="chart-row"><span class="label" title="'+esc(r.name)+'">'+esc(r.name)+'</span><div class="bar-track"><span style="width:'+(r[key]==null?0:Math.max(1,r[key]/max*100))+'%"></span></div><span class="value">'+formatter(r[key])+'</span></div>').join("");
}
function calibration(summary) {
  const points=summary.calibration_bins.filter(b=>b.count);
  if(!points.length)return empty("확률이 제공되지 않았습니다","문장으로 생성한 확신도는 확률 보정 지표로 사용하지 않습니다.");
  return '<div class="calibration"><svg viewBox="0 0 340 230" role="img" aria-label="확률과 실제 정답률 보정 그래프"><path d="M40 15V190H315" stroke="#dce4f0" fill="none"/><path d="M40 190L315 15" stroke="#c9d5e9" stroke-dasharray="5 5"/>'+
    [0,.5,1].map(v=>'<text x="'+(40+275*v)+'" y="211" text-anchor="middle" fill="#8a99b0" font-size="10">'+v+'</text><text x="28" y="'+(194-175*v)+'" text-anchor="end" fill="#8a99b0" font-size="10">'+v+'</text>').join("")+
    points.map(b=>'<circle cx="'+(40+b.confidence*275)+'" cy="'+(190-b.accuracy*175)+'" r="5" fill="#244fdd"><title>'+b.count+'개 · 확률 '+pct(b.confidence)+' · 정답률 '+pct(b.accuracy)+'</title></circle>').join("")+
    '<text x="178" y="228" text-anchor="middle" fill="#8a99b0" font-size="10">선택지 최대 확률</text></svg></div>';
}
function renderResult() {
  const r=state.run,rows=r.summary;
  const active=["running","queued","stopping"].includes(r.status);
  const finished=["paused","interrupted","error"].includes(r.status);
  const buttons='<a class="btn" href="/api/runs/'+r.id+'/export?format=csv">CSV ↓</a><a class="btn" href="/api/runs/'+r.id+'/export">JSON ↓</a>'+
    (active?'<button class="btn danger" id="stop-run">중단</button>':finished?'<button class="btn primary" id="resume-run">남은 샘플 재개</button>':"");
  $("#main").innerHTML=heading(esc(r.name),esc(r.dataset.name)+" · "+r.sample_ids.length+"개 샘플 · 동시 "+r.config.concurrency+" · 반복 "+r.config.repeats,buttons)+
    '<section class="panel"><div class="row between"><div class="row">'+statusPill(r.status)+'<strong>'+r.completed+' / '+r.total+'</strong><span class="small muted">관측치 저장됨</span></div><a class="small" href="#results">전체 실험 →</a></div><div class="progress"><span style="width:'+(r.completed/r.total*100)+'%"></span></div></section>'+
    (rows.some(s=>s.demo)?'<div class="notice demo"><strong>DEMO 포함</strong> · 모의 응답과 추정 토큰입니다. 이 결과는 실제 AI 모델의 성능이 아닙니다.</div>':"")+
    (r.error?'<div class="notice error">'+esc(r.error)+'</div>':"")+
    '<div class="tabs"><button data-tab="analysis" class="'+(state.tab==="analysis"?"active":"")+'">종합 분석</button><button data-tab="samples" class="'+(state.tab==="samples"?"active":"")+'">개별 사례</button><button data-tab="protocol" class="'+(state.tab==="protocol"?"active":"")+'">실험 조건</button></div>'+
    (state.tab==="analysis"?analysisView(r):state.tab==="samples"?samplesView(r):protocolView(r));
  $$("[data-tab]").forEach(b=>b.onclick=()=>{state.tab=b.dataset.tab;renderResult();});
  if($("#stop-run"))$("#stop-run").onclick=e=>action(e.currentTarget,async()=>{await api("/api/runs/"+r.id+"/stop",{});toast("진행 중인 요청이 끝난 뒤 중단합니다.");});
  if($("#resume-run"))$("#resume-run").onclick=e=>action(e.currentTarget,async()=>{await api("/api/runs/"+r.id+"/resume",{});await route();});
  if($("#sample-select"))$("#sample-select").onchange=e=>{state.sample=e.target.value;renderResult();};
  if($("#sample-filter"))$("#sample-filter").onchange=e=>{state.filter=e.target.value;state.sample=null;renderResult();};
}
function analysisView(r) {
  if(!r.results.length)return empty("측정을 기다리고 있습니다","대기 중이거나 첫 요청을 처리하고 있습니다. 결과는 자동으로 갱신됩니다.");
  const rows=r.summary;
  return '<section class="panel"><div class="panel-head"><div><h2>모델별 비교</h2><p>품질은 첫 반복의 유효 정답, 지연 시간은 모든 성공 요청 기준입니다.</p></div></div><div class="table-wrap"><table><thead><tr><th>모델</th><th>정확도 / 표본</th><th>공통 정답률 / 표본</th><th>p50</th><th>p95</th><th>판단/초</th><th>평균 입력 / 출력 토큰</th><th>실패 / 미지원</th></tr></thead><tbody>'+
    rows.map(s=>'<tr><td><strong>'+esc(s.name)+'</strong></td><td>'+pct(s.accuracy)+' <span class="muted">/ '+s.valid_labeled+'</span></td><td>'+pct(s.common_accuracy)+' <span class="muted">/ '+s.common_n+'</span></td><td>'+ms(s.p50_ms)+'</td><td>'+ms(s.p95_ms)+'</td><td>'+number(s.decisions_per_second,2)+'</td><td>'+number(s.mean_input_tokens,1)+' / '+number(s.mean_output_tokens,1)+'</td><td>'+s.errors+' / '+s.skipped+'</td></tr>').join("")+'</tbody></table></div><p class="result-note">공통 정답률은 모든 선택 모델이 성공한 동일 샘플의 첫 반복 기준입니다. — 는 미측정입니다. 처리량은 모델 구간 종료 후 계산됩니다.</p></section>'+
    '<div class="grid two section-gap"><section class="panel"><h2>응답 시간</h2><p class="small muted">p95 · 전처리와 API 왕복 포함</p>'+bars(rows,"p95_ms",ms)+'<div class="axis-note">서로 다른 API와 로컬 GPU의 연산 시간은 동일하지 않습니다.</div></section><section class="panel"><h2>입력 토큰</h2><p class="small muted">성공 요청당 평균 · 모델별 집계 기준</p>'+bars(rows,"mean_input_tokens",v=>number(v,0))+'<div class="axis-note">토큰 수는 연산량의 공통 단위가 아닙니다. '+rows.map(s=>esc(s.name)+": "+esc(s.token_sources.join(", ")||"미제공")).join(" · ")+'</div></section></div>'+
    '<div class="grid two section-gap"><section class="panel"><h2>확률 보정 · '+esc(rows[0].name)+'</h2><p class="small muted">대각선에 가까울수록 해당 구간의 확률과 정답률이 유사합니다.</p>'+calibration(rows[0])+'</section>'+
    '<section class="panel"><h2>품질과 비용 상세</h2><div class="table-wrap section-gap"><table><thead><tr><th>모델</th><th>Macro-F1</th><th>Brier</th><th>ECE</th><th>1,000 성공 판단 비용</th></tr></thead><tbody>'+rows.map(s=>'<tr><td>'+esc(s.name)+'</td><td>'+number(s.macro_f1,3)+'</td><td>'+number(s.brier,3)+'</td><td>'+number(s.ece,3)+'</td><td>'+price(s.cost_per_1000_success)+'</td></tr>').join("")+'</tbody></table></div><p class="result-note">비용은 입력한 토큰 단가 기반 추정치입니다. 실패 요청의 미보고 비용과 GPU 운영비는 포함하지 않습니다. 워밍업은 별도 기록됩니다.</p><p class="result-note">Macro-F1은 선택지 ID의 의미가 모든 샘플에서 같은 분류 과제에 사용하세요. 객관식 A/B/C 위치의 Macro-F1은 능력 지표로 해석하지 마세요.</p></section></div>'+
    '<section class="panel section-gap"><h2>데이터 조건별 정확도</h2><div class="table-wrap section-gap"><table><thead><tr><th>모델</th><th>형식 / 태그</th><th>정확도</th><th>유효 정답 표본</th></tr></thead><tbody>'+rows.flatMap(s=>Object.entries(s.slices).map(([tag,q])=>'<tr><td>'+esc(s.name)+'</td><td>'+esc(modeNames[tag]||tag)+'</td><td>'+pct(q.accuracy)+'</td><td>'+q.valid_labeled+'</td></tr>')).join("")+'</tbody></table></div></section>';
}
function samplesView(r) {
  let samples=r.dataset.samples.filter(s=>r.sample_ids.includes(s.id));
  if(state.filter==="errors")samples=samples.filter(s=>r.results.some(x=>x.sample_id===s.id&&x.status!=="success"));
  if(state.filter==="disagree")samples=samples.filter(s=>new Set(r.results.filter(x=>x.sample_id===s.id&&x.repetition===0&&x.status==="success").map(x=>x.choice)).size>1);
  if(!samples.some(s=>s.id===state.sample))state.sample=samples[0]?.id;
  const sample=samples.find(s=>s.id===state.sample);
  let html='<div class="row wrap-mobile section-gap"><select id="sample-filter" aria-label="사례 필터">'+[["all","전체 사례"],["errors","오류 · 미지원"],["disagree","모델 판단 불일치"]].map(([k,v])=>'<option value="'+k+'" '+(state.filter===k?"selected":"")+'>'+v+'</option>').join("")+'</select><select id="sample-select" aria-label="샘플 선택">'+samples.map(s=>'<option value="'+esc(s.id)+'" '+(s.id===state.sample?"selected":"")+'>'+esc(s.id)+' · '+esc(s.question)+'</option>').join("")+'</select></div>';
  if(!sample)return html+empty("조건에 맞는 사례가 없습니다","필터를 바꾸거나 실험 완료 후 확인하세요.");
  html+='<div class="grid two section-gap"><section class="panel"><h2>원본 입력</h2><div class="sample-text">'+esc(sample.text||"미디어 입력")+'</div>'+mediaPreview(sample.assets)+'<h3>'+esc(sample.question)+'</h3><div class="chips section-gap">'+sample.choices.map(c=>pill(c.id+": "+c.label,c.id===sample.label?"green":"")).join("")+'</div><p class="result-note">초록색은 등록된 정답입니다. 모델 요청에는 정답을 보내지 않습니다.</p></section><section class="panel"><h2>모델 응답 · 첫 반복</h2><div class="grid section-gap">'+
    r.models.map(m=>{
      const result=r.results.find(x=>x.model_id===m.id&&x.sample_id===sample.id&&x.repetition===0);
      if(!result)return '<div class="result-card"><h3>'+esc(m.name)+'</h3><p class="muted">아직 실행되지 않았습니다.</p></div>';
      const answer=sample.choices.find(c=>c.id===result.choice);
      return '<div class="result-card"><div class="row between"><h3>'+esc(m.name)+'</h3>'+pill(result.status,result.status==="success"?"green":"red")+'</div><div class="answer">'+esc(answer?answer.label:result.error)+'</div>'+
        (result.probabilities?Object.entries(result.probabilities).map(([id,p])=>'<div class="prob-row"><span>'+esc(sample.choices.find(c=>c.id===id)?.label||id)+'</span><div class="bar-track"><span style="width:'+(p*100)+'%"></span></div><span>'+pct(p)+'</span></div>').join(""):'<p class="small muted">선택지 확률 미제공</p>')+
        '<div class="result-meta"><span>전체 '+ms(result.total_ms)+'</span><span>입력 '+number(result.input_tokens,0)+' · 출력 '+number(result.output_tokens,0)+' tokens</span></div><details><summary>측정 근거 · 응답 원문</summary><pre>'+esc(JSON.stringify(result,null,2))+'</pre></details></div>';
    }).join("")+'</div></section></div>';
  return html;
}
function protocolView(r) {
  const manifest={dataset:{id:r.dataset.id,name:r.dataset.name,version:r.dataset.version,split:r.dataset.split,sha256:r.dataset.sha256},
    models:r.models,config:r.config,environment:r.environment,sample_ids:r.sample_ids,segments:r.segments,warmup_log:r.warmup_log};
  return '<section class="panel"><h2>실행 시점 스냅샷</h2><p class="small muted">현재 모델 설정을 수정해도 이 실험의 기록은 바뀌지 않습니다. 중단 후 재개할 때도 이 설정을 사용합니다.</p><pre>'+esc(JSON.stringify(manifest,null,2))+'</pre><div class="notice">이 버전은 요청당 질문 1개, 원본 미디어 전달 방식입니다. 모델 내부의 해상도·프레임 추출은 모델 서버의 설정을 따릅니다. 로컬 worker는 실제 입력 manifest를 응답합니다.</div></section>';
}
function pollRun(id) {
  if(!["running","queued","stopping"].includes(state.run.status))return;
  state.timer=setTimeout(async()=>{
    if(location.hash!=="#results/"+id)return;
    try{
      state.run=await api("/api/runs/"+id);
      // 입력이나 선택 중에는 화면 전체 갱신을 보류해 키보드 포커스를 유지합니다.
      const editing=["SELECT","INPUT","TEXTAREA"].includes(document.activeElement.tagName);
      if(!editing)renderResult();
      // 실행이 끝난 순간 선택창에 포커스가 있어도 마지막 결과는 포커스 해제 후 반영합니다.
      if(editing && !["running","queued","stopping"].includes(state.run.status)) {
        document.activeElement.addEventListener("blur",()=>{
          if(location.hash==="#results/"+id)renderResult();
        },{once:true});
      } else pollRun(id);
    }catch(error){toast(error.message);}
  },1200);
}
window.addEventListener("hashchange",route);
$("#dialog").addEventListener("click",e=>{if(e.target===$("#dialog"))$("#dialog").close();});
route();
