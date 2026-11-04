const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function controller() {
  const elements = new Map();
  class Element {
    constructor() { this.children = []; this.dataset = {}; this.attributes = {}; this.style = {}; this.listeners = {}; this.textContent = ''; this.value = ''; this.hidden = false; this.disabled = false; this.classList = { add() {}, remove() {}, toggle() {} }; this.firstChild = {textContent:''}; }
    set id(id) { this._id = id; elements.set(id, this); } get id() { return this._id; }
    append(...items) { this.children.push(...items); } prepend(...items) { this.children.unshift(...items); }
    replaceChildren(...items) { this.children = items; this.value = ''; }
    replaceWith() {} after() {} on() {} removeAllListeners() {} matches() { return false; }
    setAttribute(key, value) { this.attributes[key] = String(value); } getAttribute(key) { return this.attributes[key]; }
    addEventListener(name, action) { (this.listeners[name] ||= []).push(action); }
    get options() { return this.children; } get selectedOptions() { return this.children.filter(item => item.selected); }
    querySelectorAll(selector) { const values = []; for (const child of this.children) { if (selector === 'input[type="checkbox"]' && child.type === 'checkbox') values.push(child); values.push(...child.querySelectorAll?.(selector) || []); } return values; }
    querySelector(selector) { return element(`child:${this.id}:${selector}`); }
    showModal() { this.open = true; } close() { this.open = false; } focus() { this.focused = true; }
    click() { for (const action of this.listeners.click || []) action({target:this}); }
  }
  function element(id) { if (!elements.has(id)) { const item = new Element(); item.id = id; } return elements.get(id); }
  const tabs = ['shared','current','task'].map(origin => { const tab = element(`origin-${origin}`); tab.dataset.inputOrigin = origin; return tab; });
  const context = vm.createContext({
    document: { getElementById:element, createElement:tag => Object.assign(new Element(), {tagName:tag.toUpperCase()}), querySelectorAll:selector => selector === '[data-input-origin]' ? tabs : [], querySelector:selector => element(selector), body:new Element() },
    window: { addEventListener() {} },
    Option: class extends Element { constructor(text, value) { super(); this.text = text; this.value = value; } },
    localStorage: {getItem:() => null, setItem() {}}, fetch:() => new Promise(() => {}),
    setTimeout:action => { action(); return 1; }, clearTimeout() {}, requestAnimationFrame() {}, console, deferred,
  });
  vm.runInContext(readFileSync(path.resolve(__dirname, '../../workbench/static/workbench.js'), 'utf8'), context);
  vm.runInContext(`
    state.run = {id:'a',payload:'15B',analysis_contract:'cali_proc@0.5',config:{},jobs:[],dataset:{raw_dir:'/shared/raw/15B'}};
    state.layer = 'EC_L1'; state.busy = false; state.loading = false; state.chartLoads = 0;
    setBusy = busy => {state.busy=busy;};
    renderRun = () => {state.renders=(state.renders || 0)+1;renderLayerInputs();};
    loadSurfaceSafely = async () => {state.chartLoads++;};
    notify = (message,type) => {state.notice={message,type};};
  `, context);
  return { run:source => vm.runInContext(source, context), element, tabs };
}

test('compact input summaries render two EC dependencies without fetching products', () => {
  const ui = controller();
  ui.run(`state.calls=0;api=async()=>{state.calls++;};
    state.run.layer_inputs={EC_L1:{slots:[
      {key:'data',label:'EC L0',selected:{origin:'shared',label:'15B L0',version:'r1'}},
      {key:'tb',label:'TB L2',selected:{origin:'task',run_name:'温偏复核',version:'r2'}}
    ],selected_result:{label:'校正结果',version:'r3'}}};renderLayerInputs();`);
  assert.equal(ui.element('layer-inputs').children.length, 2);
  assert.equal(ui.element('layer-inputs').children[1].children[0].textContent, 'TB L2');
  assert.match(ui.element('layer-inputs').children[1].children[1].children[0].textContent, /温偏复核/);
  assert.equal(ui.element('layer-result-label').textContent, '结果 · r3');
  assert.equal(ui.run('state.calls'), 0);
});

test('the selected input is one accessible picker that opens its exact slot', async () => {
  const ui = controller();
  ui.run(`state.run.layer_inputs={EC_L1:{slots:[{key:'tb',label:'TB L2',follow_current:true}]}};
    state.opened=[];openLayerInputChooser=async slot=>state.opened.push(slot);renderLayerInputs();`);
  const row = ui.element('layer-inputs').children[0], picker = row.children[1];
  assert.equal(row.children.length, 2);
  assert.equal(picker.tagName, 'BUTTON');
  assert.equal(picker.getAttribute('aria-haspopup'), 'dialog');
  assert.equal(picker.getAttribute('aria-controls'), 'layer-input-dialog');
  assert.match(picker.getAttribute('aria-label'), /TB L2.*跟随本任务 TB/);
  await picker.listeners.click[0]();
  assert.equal(ui.run('state.opened.join()'), 'tb');
  ui.run('state.busy=true;renderLayerInputs();');
  assert.equal(ui.element('layer-inputs').children[0].children[1].disabled, true);
});

test('navigation lights distinguish missing, reused and computed products without hiding pending work', () => {
  const ui = controller();
  for (const [stage, tone, label] of [
    [{status:'not_run',history_count:0,current_count:0}, 'empty', '未运行，无结果'],
    [{status:'not_run',history_count:5,current_count:0}, 'available', '未运行，有结果'],
    [{status:'not_run',history_count:0,current_count:2}, 'available', '未运行，有结果'],
    [{status:'succeeded',current_count:1}, 'computed', '已计算'],
    [{status:'completed',current_count:1}, 'computed', '已计算'],
    [{status:'succeeded',stale:true,current_count:1}, 'stale', '已计算，待更新'],
    [{status:'succeeded',acquisition_coverage:{completed:2,required:5}}, 'partial', '部分计算，已处理 2 / 5'],
    [{status:'running'}, 'running', '运行中'],
    [{status:'queued'}, 'queued', '排队中'],
    [{status:'failed',history_count:5}, 'failed', '计算失败'],
  ]) {
    const actual = ui.run(`navigationIndicator(${JSON.stringify(stage)})`);
    assert.equal(actual.tone, tone);
    assert.equal(actual.label, label);
  }
  assert.equal(ui.run('navigationIndicator({status:"succeeded"},false).tone'), 'idle');
});

test('navigation keeps status text accessible while showing one light beside aligned layer names', () => {
  const ui = controller();
  ui.run(`state.run.stages={TB_L0:{status:'not_run'},TB_L1:{status:'not_run',history_count:3},TB_L2:{status:'succeeded'}};renderNavigation();`);
  const group = ui.element('stage-nav').children[0];
  assert.equal(group.children[0].children[0].textContent, 'TB');
  assert.equal(group.children[0].children[1].textContent, '温度–偏压');
  for (const [index, tone, label] of [[1,'empty','未运行，无结果'],[2,'available','未运行，有结果'],[3,'computed','已计算']]) {
    const button = group.children[index];
    assert.equal(button.children.length, 3);
    assert.equal(button.children[2].className, `nav-status ${tone}`);
    assert.equal(button.children[2].getAttribute('aria-hidden'), 'true');
    assert.ok(button.getAttribute('aria-label').includes(label));
    assert.ok(button.title.includes(label));
  }
});

test('candidate filtering respects source, task, search and real coverage', () => {
  const ui = controller();
  ui.run(`inputSelection.mode='input';inputSelection.slot='data';inputSelection.origin='task';
    inputSelection.data={slots:[{key:'data',candidates:[
      {id:'shared',origin:'shared',label:'15B L0'},
      {id:'one',origin:'task',run_id:'r1',run_name:'Co60 双峰',observations:['Co60'],channels:[0,1]},
      {id:'two',origin:'task',run_id:'r2',run_name:'Na22 复核',observations:['Na22'],channels:[0]}
    ]}]};renderInputTaskFilter();$('input-task-filter').value='r1';$('input-candidate-search').value='co60';`);
  assert.equal(ui.run('filteredInputCandidates().map(item=>item.id).join()'), 'one');
  assert.equal(ui.run('inputCandidateCoverage(filteredInputCandidates()[0])'), '1 个文件 · CH0 / CH1');
});

test('a delayed catalog cannot paint another run or open layer', async () => {
  const ui = controller();
  ui.run(`const request=deferred();api=()=>request.promise;const pending=openLayerInputChooser('data');state.run={id:'b',payload:'14B'};`);
  await ui.run(`request.resolve({slots:[{key:'data',candidates:[{id:'old',origin:'shared'}]}]});pending;`);
  assert.equal(ui.run('inputSelection.data'), null);
  assert.equal(ui.element('input-choice-error').hidden, true);
});

test('late impact previews and late errors cannot replace the latest choice', async () => {
  const ui = controller();
  ui.run(`$('layer-input-dialog').showModal();Object.assign(inputSelection,{runId:'a',layer:'EC_L1',slot:'data',candidate:{id:'one'},follow:false});
    const first=deferred();api=()=>first.promise;const pending=previewLayerInputChoice();
    inputSelection.candidate={id:'two'};api=async()=>({layers:['EC_L2']});const next=previewLayerInputChoice();`);
  await ui.run('next');
  await ui.run(`first.resolve({layers:['EC_L3']});pending;`);
  assert.equal(ui.run('inputSelection.impact.layers.join()'), 'EC_L2');
  ui.run(`const older=deferred();api=()=>older.promise;const failed=previewLayerInputChoice();
    inputSelection.candidate={id:'three'};api=async()=>({layers:['EC_L1']});const newest=previewLayerInputChoice();`);
  await ui.run('newest');
  await ui.run(`older.reject(new Error('old failure'));failed;`);
  assert.equal(ui.run('inputSelection.impact.layers.join()'), 'EC_L1');
  assert.equal(ui.element('input-choice-error').hidden, true);
});

test('partial scope defaults to the entire bundle and rejects an empty selection', () => {
  const ui = controller();
  ui.run(`inputSelection.mode='input';inputSelection.layer='TB_L2';inputSelection.candidate={id:'one',observations:['A','B'],channels:[0,1]};renderInputScope(inputSelection.candidate);`);
  assert.equal(ui.run('selectedInputScope()'), undefined);
  ui.element('input-scope-channels').children[1].children[0].checked = false;
  assert.equal(ui.run('JSON.stringify(selectedInputScope())'), JSON.stringify({observations:['A','B'],channels:[0]}));
  for (const option of ui.element('input-scope-observations').options) option.selected = false;
  assert.throws(() => ui.run('selectedInputScope()'), /至少选择/);
});

test('missing native spectral channel metadata still permits selecting a physical fit channel', () => {
  const ui = controller();
  ui.run(`inputSelection.mode='input';inputSelection.layer='EC_L2';inputSelection.candidate={id:'native',observations:['A'],channels:[]};renderInputScope(inputSelection.candidate);`);
  assert.equal(ui.element('input-scope-channels').children.length, 4);
  assert.equal(ui.run('selectedInputScope()'), undefined);
  for (const label of ui.element('input-scope-channels').children) label.children[0].checked = label.children[0].value === 1;
  assert.equal(ui.run('JSON.stringify(selectedInputScope())'), JSON.stringify({observations:['A'],channels:[1]}));
  assert.equal(ui.run('inputCandidateCoverage(inputSelection.candidate)'), '1 个文件');
});

test('fallback spectral scope uses only channels supported by the detector model', () => {
  const ui = controller();
  ui.run(`state.run.payload='10B';state.run.dataset={};inputSelection.mode='input';inputSelection.layer='TB_L1';inputSelection.candidate={id:'native',observations:['A'],channels:[]};renderInputScope(inputSelection.candidate);`);
  assert.equal(ui.element('input-scope-channels').children.length, 3);
  assert.equal(ui.run('JSON.stringify(inputScopeChannels(inputSelection.candidate))'), '[0,1,2]');
});

test('corr input scope selects whole event files without offering physical-channel replacement', () => {
  const ui = controller();
  ui.run(`inputSelection.mode='input';inputSelection.layer='EC_L1';inputSelection.candidate={id:'raw',observations:['A','B'],channels:[0,1,2,3]};renderInputScope(inputSelection.candidate);`);
  assert.equal(ui.element('input-channel-scope').hidden, true);
  assert.equal(ui.element('input-scope-channels').children.length, 0);
  assert.equal(ui.run('selectedInputScope()'), undefined);
  ui.element('input-scope-observations').options[1].selected = false;
  assert.equal(ui.run('JSON.stringify(selectedInputScope())'), JSON.stringify({observations:['A']}));
});

test('adopting an existing result applies mode=result without starting a processor', async () => {
  const ui = controller();
  ui.run(`$('layer-input-dialog').showModal();Object.assign(inputSelection,{runId:'a',layer:'EC_L1',mode:'result',candidate:{id:'existing'},follow:false,impact:{layers:[]}});
    state.calls=[];api=async(path,options)=>{state.calls.push({path,body:options.body});return {run:{...state.run,layer_inputs:{}}};};`);
  await ui.run('applyLayerInputChoice(false)');
  assert.equal(ui.run('state.calls.length'), 1);
  assert.equal(ui.run('state.calls[0].body.mode'), 'result');
  assert.equal(ui.run('state.calls[0].body.action'), 'apply');
  assert.equal(ui.run('state.calls[0].body.slot'), undefined);
  assert.equal(ui.run('state.calls[0].path.endsWith("/layer-inputs")'), true);
  assert.equal(ui.run('state.chartLoads'), 1);
});

test('viewing and filtering results never applies or computes them', () => {
  const ui = controller();
  ui.run(`state.calls=0;api=async()=>{state.calls++;};inputSelection.mode='result';inputSelection.origin='shared';inputSelection.data={results:[{id:'one',origin:'shared',label:'Result'}]};renderInputTaskFilter();renderInputCandidates();$('input-candidate-search').value='missing';renderInputCandidates();`);
  assert.equal(ui.run('state.calls'), 0);
  assert.equal(ui.element('input-candidate-list').children[0].textContent, '没有匹配的结果');
});

test('changing an input keeps the displayed old result until it is recomputed', async () => {
  const ui = controller();
  ui.run(`$('layer-input-dialog').showModal();Object.assign(inputSelection,{runId:'a',layer:'EC_L1',slot:'data',mode:'input',candidate:{id:'existing'},follow:false,impact:{layers:['EC_L1']}});
    state.surface={old:true};api=async()=>({run:{...state.run,layer_inputs:{}}});`);
  await ui.run('applyLayerInputChoice(false)');
  assert.equal(ui.run('state.surface.old'), true);
  assert.equal(ui.run('state.chartLoads'), 0);
});

test('a sequential update polls beyond the first completed job and reloads plots once', async () => {
  const ui = controller();
  ui.run(`state.run.update_chain={status:'running',steps:[{layer:'EC_L1'},{layer:'EC_L2'}],position:0};state.calls=0;
    api=async()=>{state.calls++;return {...state.run,update_chain:{...state.run.update_chain,status:state.calls===1?'running':'succeeded',position:1}};};`);
  await ui.run(`restorePolling({id:'first',layer:'EC_L1',status:'succeeded'})`);
  assert.equal(ui.run('state.calls'), 2);
  assert.equal(ui.run('state.chartLoads'), 1);
  assert.equal(ui.run('state.busy'), false);
  assert.equal(ui.run('state.notice.type'), 'success');
});

test('an update chain from another run cannot repaint the new task', async () => {
  const ui = controller();
  ui.run(`state.run.update_chain={status:'running',steps:[{layer:'TB_L1'}]};const request=deferred();api=()=>request.promise;const pending=restorePolling({layer:'TB_L1',status:'succeeded'});`);
  await Promise.resolve();
  ui.run(`state.run={id:'b',payload:'14B'};++state.pollToken;request.resolve({id:'a',update_chain:{status:'succeeded'}});`);
  await ui.run('pending');
  assert.equal(ui.run('state.run.id'), 'b');
  assert.equal(ui.run('state.chartLoads'), 0);
});

test('impact descriptions preserve native EC names and scoped channels', () => {
  const ui = controller();
  ui.run(`appendInputImpact($('input-impact-steps'),{layers:['EC_L2','EC_L3'],scopes:{EC_L2:{stems:['Co60'],channels:[1],point_ids:['left','right']},EC_L3:{channels:[1]}}});`);
  const rows = ui.element('input-impact-steps').children;
  assert.equal(rows[0].children[0].textContent, 'EC L1');
  assert.match(rows[0].children[1].textContent, /Co60 · CH1 · 2 个峰/);
  assert.equal(rows[1].children[0].textContent, 'EC L2');
});

test('an in-flight application cannot be replaced by another candidate', () => {
  const ui = controller();
  ui.run(`inputSelection.busy=true;inputSelection.candidate={id:'original'};selectInputCandidate({id:'other'});`);
  assert.equal(ui.run('inputSelection.candidate.id'), 'original');
});

test('an update failure after the picker closes reports in the workbench', () => {
  const ui = controller();
  ui.run(`$('layer-input-dialog').close();messageError=error=>{state.error=error.message;};inputApplyError(new Error('Connection lost'));`);
  assert.equal(ui.run('state.error'), 'Connection lost');
});

test('the scoped update action appears only for remaining affected layers', () => {
  const ui = controller();
  ui.run(`state.run.last_impact={layers:['EC_L2','EC_L3']};state.run.dirty_layers=['EC_L3','TB_L1'];renderLayerInputs();`);
  assert.equal(ui.element('update-affected-layers').hidden, false);
  assert.equal(ui.element('update-affected-layers').title, 'EC L2');
  ui.run(`state.run.dirty_layers=['TB_L1'];renderLayerInputs();`);
  assert.equal(ui.element('update-affected-layers').hidden, true);
});

test('the scoped update action uses the stored impact without submitting unrelated layers', async () => {
  const ui = controller();
  ui.run(`state.run.last_impact={layers:['EC_L3']};state.run.dirty_layers=['EC_L3','TB_L0'];state.calls=[];
    api=async(path,options)=>{state.calls.push({path,body:options.body});return {run:{...state.run,dirty_layers:['TB_L0']}};};`);
  await ui.run('updateAffectedLayers()');
  assert.equal(ui.run('state.calls.length'), 1);
  assert.equal(ui.run('state.calls[0].path'), '/api/runs/a/updates');
  assert.equal(ui.run('JSON.stringify(state.calls[0].body)'), '{}');
  assert.equal(ui.run('state.busy'), false);
});

test('a delayed scoped update response cannot replace a newly opened task', async () => {
  const ui = controller();
  ui.run(`state.run.last_impact={layers:['EC_L3']};state.run.dirty_layers=['EC_L3'];const request=deferred();api=()=>request.promise;const pending=updateAffectedLayers();`);
  ui.run(`state.run={id:'b',payload:'14B'};++state.openToken;request.resolve({run:{id:'a',payload:'15B'}});`);
  await ui.run('pending');
  assert.equal(ui.run('state.run.id'), 'b');
});
