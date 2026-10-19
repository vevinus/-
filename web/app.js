const $ = (id) => document.getElementById(id);
let selectedFile, job, edit, pollTimer, rendered = false;
const video = $('video');
const names = {pop: '팝 · 놀람/질문', ding: '딩 · 성공/완성', whoosh: '슉 · 전환'};

function el(tag, attrs = {}, text = '') {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else node[key] = value;
  }
  if (text) node.textContent = text;
  return node;
}
function notice(message, busy = false) {
  $('notice').hidden = !message;
  $('notice').textContent = message;
  $('notice').classList.toggle('busy', busy);
}
function time(t) {
  const minutes = Math.floor(t / 60);
  return `${minutes}:${(t % 60).toFixed(1).padStart(4, '0')}`;
}
async function api(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let body;
    try { body = await response.json(); } catch { throw Error(`요청 실패 (${response.status})`); }
    throw Error(typeof body.detail === 'string' ? body.detail : '입력 시간과 설정을 확인하세요.');
  }
  return response.json();
}
function choose(file) {
  if (!file) return;
  selectedFile = file;
  $('file-label').textContent = `${file.name} · ${(file.size / 1024**2).toFixed(1)}MB`;
  $('analyze').disabled = false;
}
$('file').addEventListener('change', (event) => choose(event.target.files[0]));
$('dropzone').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); $('file').click(); }
});
for (const name of ['dragenter', 'dragover']) $('dropzone').addEventListener(name, (event) => {
  event.preventDefault(); $('dropzone').classList.add('drag');
});
for (const name of ['dragleave', 'drop']) $('dropzone').addEventListener(name, (event) => {
  event.preventDefault(); $('dropzone').classList.remove('drag');
});
$('dropzone').addEventListener('drop', (event) => choose(event.dataTransfer.files[0]));
for (const id of ['silence', 'freeze']) $(id).addEventListener('input', () => $(id + '-value').textContent = `${Number($(id).value).toFixed(1)}초`);
function subtitleMode() {
  $('model').disabled = $('language').disabled = !$('transcribe').checked;
  $('subtitle-source').disabled = !$('transcribe').checked;
  $('script-section').hidden = $('upload-section').hidden || !$('transcribe').checked || $('subtitle-source').value !== 'script';
}
$('transcribe').addEventListener('change', subtitleMode);
$('subtitle-source').addEventListener('change', subtitleMode);
$('script').addEventListener('input', () => $('script-count').textContent = `${$('script').value.length.toLocaleString()} / 8,000자`);
$('analyze').addEventListener('click', async () => {
  if (!selectedFile) return;
  const script = $('transcribe').checked && $('subtitle-source').value === 'script' ? $('script').value.trim() : '';
  if ($('transcribe').checked && $('subtitle-source').value === 'script' && !script) {
    notice('영상에서 말하는 대본을 먼저 입력하세요.'); $('script').focus(); return;
  }
  $('analyze').disabled = true;
  notice('영상을 불러오는 중입니다…', true);
  const body = new FormData();
  body.append('file', selectedFile);
  body.append('settings', JSON.stringify({
    silence_duration: Number($('silence').value), freeze_duration: Number($('freeze').value),
    transcribe: $('transcribe').checked, model: $('model').value, language: $('language').value,
    script,
  }));
  try {
    job = await api('/api/jobs', {method: 'POST', body});
    localStorage.setItem('cutmate-job', job.id);
    $('upload-section').hidden = true;
    subtitleMode();
    $('features').hidden = true;
    poll(job.id);
  } catch (error) { notice(error.message); $('analyze').disabled = false; }
});
function showWarnings(warnings = []) {
  $('warnings').replaceChildren(...warnings.map(text => el('div', {class: 'warning'}, text)));
  $('warnings').hidden = !warnings.length;
}
async function poll(id) {
  clearTimeout(pollTimer);
  try {
    const result = await api(`/api/jobs/${id}`);
    if (job && job.id !== id) return;
    job = result;
    const busy = ['queued', 'analyzing', 'exporting'].includes(job.status);
    notice(job.message, busy);
    if (busy) { pollTimer = setTimeout(() => poll(id), 1200); return; }
    showWarnings(job.warnings);
    if (job.edit) {
      edit = structuredClone(job.edit);
      $('editor').hidden = false;
      $('upload-section').hidden = true;
      subtitleMode();
      $('features').hidden = true;
      $('step1').classList.remove('current');
      $('step2').classList.add('current');
      $('video-title').textContent = job.filename;
      rendered = job.status === 'exported';
      const kind = rendered ? 'preview' : 'original';
      video.src = `/api/jobs/${id}/media/${kind}?v=${job.export || 'source'}`;
      $('preview-label').textContent = rendered ? '편집 결과 · 효과음 포함' : '원본 · 선택한 컷 건너뛰기';
      $('downloads').hidden = !rendered;
      if (rendered) {
        $('download-project').href = `/api/jobs/${id}/media/project`;
        $('download-srt').href = `/api/jobs/${id}/media/subtitles`;
        $('step3').classList.add('current');
      }
      $('export').disabled = false;
      $('editor').classList.remove('readonly');
      drawAll();
    }
    if (job.status === 'error') {
      $('upload-section').hidden = false;
      subtitleMode();
      $('analyze').disabled = false;
    }
  } catch (error) { notice(error.message + ' · 페이지를 새로고침하면 작업 상태를 다시 확인합니다.'); }
}
function dirty() {
  if (rendered) {
    rendered = false;
    video.src = `/api/jobs/${job.id}/media/original`;
    $('preview-label').textContent = '원본 · 선택한 컷 건너뛰기';
  }
  $('downloads').hidden = true;
  $('step3').classList.remove('current');
  notice('수정한 내용은 프로젝트를 다시 만들면 반영됩니다.');
  drawSummary();
}
function mergedCuts() {
  const result = [];
  for (const cut of edit.cuts.filter(c => c.selected).toSorted((a, b) => a.start - b.start)) {
    if (result.length && cut.start <= result.at(-1).end) result.at(-1).end = Math.max(result.at(-1).end, cut.end);
    else result.push({start: cut.start, end: cut.end});
  }
  return result;
}
function drawSummary() {
  const removed = mergedCuts().reduce((n, c) => n + c.end - c.start, 0);
  $('summary').textContent = `원본 ${time(job.meta.duration)} → 편집 ${time(Math.max(0, job.meta.duration - removed))} · ${removed.toFixed(1)}초 정리`;
  $('cut-count').textContent = edit.cuts.filter(c => c.selected).length;
  $('caption-count').textContent = edit.captions.length;
  $('effect-count').textContent = edit.effects.filter(e => e.selected).length;
  $('duration').textContent = time(rendered ? job.timeline.duration : job.meta.duration);
  const nodes = [];
  if (!rendered) for (const cut of edit.cuts) {
    if (cut.selected || cut.kind === 'freeze') {
      const node = el('span', {class: 'cut' + (!cut.selected ? ' freeze' : '')});
      node.style.left = `${100 * cut.start / job.meta.duration}%`;
      node.style.width = `${100 * (cut.end-cut.start) / job.meta.duration}%`;
      node.title = `${cut.kind === 'silence' ? '무음' : '화면 정지'} ${time(cut.start)}–${time(cut.end)}`;
      nodes.push(node);
    }
  }
  nodes.push(el('span', {class: 'playhead', id: 'playhead'}));
  $('timeline').replaceChildren(...nodes);
}
function seek(t) {
  if (rendered) { rendered = false; video.src = `/api/jobs/${job.id}/media/original`; $('preview-label').textContent = '원본 · 선택한 컷 건너뛰기'; drawSummary(); }
  video.currentTime = t;
  // Keep paused so a selected cut can be inspected before it is skipped on playback.
  video.pause();
}
function drawCuts() {
  $('cuts-list').replaceChildren(...edit.cuts.map(cut => {
    const row = el('div', {class: 'cut-row'});
    const check = el('input', {type: 'checkbox', checked: cut.selected, ariaLabel: `${time(cut.start)} 구간 삭제`, onchange: () => { cut.selected = check.checked; dirty(); }});
    const text = el('div');
    const line = el('strong');
    line.append(el('span', {class: 'tag' + (cut.kind === 'freeze' ? ' freeze' : '')}, cut.kind === 'silence' ? '무음' : '화면 정지'), document.createTextNode(`${(cut.end-cut.start).toFixed(1)}초`));
    text.append(line, el('small', {}, `${time(cut.start)} → ${time(cut.end)}`));
    row.append(check, text, el('button', {class: 'seek', onclick: () => seek(cut.start)}, '보기 ▷'));
    return row;
  }));
  if (!edit.cuts.length) $('cuts-list').append(el('div', {class: 'empty'}, '설정한 기준에 맞는 무음·정지 구간이 없어요.'));
}
function numberField(value, onChange) {
  return el('input', {type: 'number', min: 0, max: job.meta.duration, step: 0.01, value: Number(value.toFixed(2)),
    ariaLabel: '원본 기준 시간(초)', onchange: event => { onChange(Number(event.target.value)); dirty(); }});
}
function drawCaptions() {
  drawAlignment();
  $('captions-list').replaceChildren(...edit.captions.map((caption, i) => {
    const row = el('div', {class: 'caption-row'});
    if (caption.needs_review) {
      row.classList.add('needs-review');
      const badge = el('div', {class: 'caption-review'});
      badge.append(el('span', {}, `시간 확인 필요 · 문구 일치 ${Math.round((caption.alignment_score || 0)*100)}%`),
        el('button', {class: 'text-button', onclick: () => { caption.needs_review = false; dirty(); drawCaptions(); }}, '확인 완료'));
      row.append(badge);
    }
    const times = el('div', {class: 'time-fields'});
    times.append(el('button', {class: 'seek', onclick: () => seek(caption.start)}, '▷'),
      numberField(caption.start, v => caption.start = v), document.createTextNode('→'), numberField(caption.end, v => caption.end = v),
      el('button', {class: 'remove', ariaLabel: '자막 삭제', onclick: () => { edit.captions.splice(i, 1); dirty(); drawCaptions(); }}, '×'));
    const input = el('textarea', {value: caption.text, ariaLabel: '자막 내용', oninput: event => { caption.text = event.target.value; dirty(); }});
    row.append(times, input);
    return row;
  }));
  if (!edit.captions.length) $('captions-list').append(el('div', {class: 'empty'}, '생성된 자막이 없어요.\n자막을 직접 추가하거나 자동 자막을 켜고 다시 분석해 주세요.'));
}
function drawAlignment() {
  const report = job.script_alignment;
  const node = $('alignment-report');
  node.hidden = !report;
  node.replaceChildren();
  if (!report) return;
  const missing = report.lines.filter(line => line.status === 'unmatched');
  node.append(el('p', {}, `대본 음성 매칭 ${Math.round(report.coverage*100)}% · 찾지 못한 문장 ${missing.length}개`));
  node.append(el('small', {}, '문구 일치도를 뜻하며 시간 정확도는 재생해서 확인해 주세요.'));
  for (const line of missing) {
    const row = el('div', {class: 'unmatched-line'});
    row.append(el('span', {}, `${line.line}. ${line.text}`), el('button', {class: 'text-button', onclick: () => {
      const start = Math.max(0, Math.min(rendered ? 0 : video.currentTime, job.meta.duration-.1));
      edit.captions.push({start, end: Math.min(start+2, job.meta.duration), text: line.text,
        needs_review: true, alignment_score: line.score});
      dirty(); drawCaptions();
      notice('현재 재생 위치에 수동 자막을 추가했습니다. 시작·끝 시간을 맞춘 뒤 확인 완료를 누르세요.');
    }}, '현재 위치에 추가'));
    node.append(row);
  }
}
function drawEffects() {
  $('effects-list').replaceChildren(...edit.effects.map((effect, i) => {
    const row = el('div', {class: 'effect-row'});
    const fields = el('div', {class: 'time-fields'});
    const select = el('select', {ariaLabel: '효과음 종류', onchange: event => { effect.kind = event.target.value; dirty(); }});
    for (const [value, label] of Object.entries(names)) select.append(el('option', {value, selected: effect.kind === value}, label));
    const check = el('input', {type: 'checkbox', checked: effect.selected, ariaLabel: '효과음 사용', onchange: event => { effect.selected = event.target.checked; dirty(); }});
    fields.append(check, numberField(effect.time, v => effect.time = v), select,
      el('button', {class: 'remove', ariaLabel: '효과음 삭제', onclick: () => { edit.effects.splice(i, 1); dirty(); drawEffects(); }}, '×'));
    const volume = el('label', {class: 'volume'}, '음량');
    const output = el('span', {}, `${Math.round(effect.volume * 100)}%`);
    volume.append(el('input', {type: 'range', min: 0, max: 1, step: 0.01, value: effect.volume,
      oninput: event => { effect.volume = Number(event.target.value); output.textContent = `${Math.round(effect.volume*100)}%`; dirty(); }}), output);
    row.append(fields, el('small', {}, effect.reason), volume);
    return row;
  }));
  if (!edit.effects.length) $('effects-list').append(el('div', {class: 'empty'}, '자동 추천할 표현이 없어요.\n원하는 위치에 효과음을 직접 추가할 수 있어요.'));
}
function drawAll() { drawSummary(); drawCuts(); drawCaptions(); drawEffects(); }
$('select-freezes').addEventListener('click', () => {
  const freezes = edit.cuts.filter(c => c.kind === 'freeze');
  const select = freezes.some(c => !c.selected);
  for (const cut of freezes) cut.selected = select;
  dirty(); drawCuts();
});
$('add-caption').addEventListener('click', () => {
  const start = Math.max(0, Math.min(rendered ? 0 : video.currentTime, job.meta.duration - .1));
  edit.captions.push({start, end: Math.min(start + 2, job.meta.duration), text: '자막을 입력하세요'});
  dirty(); drawCaptions();
});
$('add-effect').addEventListener('click', () => {
  edit.effects.push({time: Math.min(rendered ? 0 : video.currentTime, job.meta.duration - .05), kind: 'pop', reason: '직접 추가', selected: true, volume: .22});
  dirty(); drawEffects();
});
document.querySelectorAll('.tab').forEach(button => button.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(tab => { tab.classList.toggle('active', tab === button); tab.setAttribute('aria-selected', tab === button ? 'true' : 'false'); });
  for (const name of ['cuts', 'captions', 'effects']) $(name + '-panel').hidden = name !== button.dataset.tab;
}));
$('timeline').addEventListener('click', event => {
  const box = $('timeline').getBoundingClientRect();
  video.currentTime = Math.max(0, Math.min(1, (event.clientX-box.left)/box.width)) * (rendered ? job.timeline.duration : job.meta.duration);
});
video.addEventListener('timeupdate', () => {
  if (!edit || !job.meta) return;
  if (!rendered && !video.paused) {
    const cut = mergedCuts().find(c => video.currentTime >= c.start && video.currentTime < c.end - .015);
    if (cut) video.currentTime = cut.end;
  }
  const captions = rendered ? job.timeline.captions : edit.captions;
  $('subtitle-overlay').textContent = captions.find(c => c.start <= video.currentTime && c.end > video.currentTime)?.text || '';
  const head = $('playhead');
  if (head) head.style.left = `${video.currentTime / (rendered ? job.timeline.duration : job.meta.duration) * 100}%`;
});
$('export').addEventListener('click', async () => {
  if (edit.captions.some(c => c.end <= c.start || c.start < 0 || c.end > job.meta.duration)) { notice('자막 시작·끝 시간이 올바른지 확인하세요.'); return; }
  $('export').disabled = true;
  video.pause();
  $('editor').classList.add('readonly');
  try {
    await api(`/api/jobs/${job.id}/export`, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(edit)});
    poll(job.id);
  } catch (error) { notice(error.message); $('export').disabled = false; $('editor').classList.remove('readonly'); }
});
$('new-file').addEventListener('click', () => {
  clearTimeout(pollTimer); job = edit = undefined; rendered = false;
  localStorage.removeItem('cutmate-job');
  video.pause(); video.removeAttribute('src'); video.load();
  $('editor').hidden = true; $('downloads').hidden = true; $('warnings').hidden = true;
  $('upload-section').hidden = false; $('features').hidden = false;
  $('step1').classList.add('current'); $('step2').classList.remove('current'); $('step3').classList.remove('current');
  $('analyze').disabled = !selectedFile; notice('');
  subtitleMode();
});
api('/api/health').then(health => {
  if (!health.ok) notice('FFmpeg 또는 FFprobe가 없습니다. README의 Windows 설치 안내를 확인하세요.');
}).catch(() => notice('로컬 서버 연결을 확인하세요.'));
const saved = localStorage.getItem('cutmate-job');
if (saved && /^[a-f0-9]{32}$/.test(saved)) poll(saved);
