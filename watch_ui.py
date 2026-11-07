"""Browser-only watch diagnostics, separate from Ruffle's sampled canvas."""

WATCH_UI = r"""() => {
    const style = document.createElement('style');
    style.textContent = `
      body { overflow:hidden; background:#10141d; color:#e7edf6; font:14px system-ui,sans-serif; }
      #watch-shell { display:grid; grid-template-columns:minmax(0,1fr) 280px; height:100vh; }
      #game-area { position:relative; min-width:0; min-height:0; overflow:hidden; }
      #game-stage { position:absolute; width:550px; height:400px; transform-origin:top left; }
      #watch-frozen { position:absolute; inset:0; width:550px; height:400px; pointer-events:none; z-index:1; }
      #watch-frozen[hidden] { display:none; }
      #watch-status { position:absolute; top:12px; left:12px; right:12px; max-width:520px; padding:12px 16px; border-radius:8px; background:rgba(16,20,29,.9); border:1px solid #354258; pointer-events:none; z-index:5; }
      #watch-level, #watch-progress { font-size:11px; color:#b8c5d9; }
      #watch-phase { font-size:18px; font-weight:650; color:#91d8e8; margin:4px 0; }
      #watch-detail { font-size:12px; line-height:1.4; margin:4px 0; overflow-wrap:anywhere; }
      #watch-actions { position:absolute; right:16px; bottom:16px; z-index:5; }
      #watch-next { padding:12px 18px; border:1px solid #60738f; border-radius:8px; background:#26344b; color:white; cursor:pointer; font:600 14px system-ui; }
      #watch-next:disabled { opacity:.5; }
      #watch-levels { display:grid; grid-template-columns:repeat(6,1fr); gap:7px; margin:12px 0; }
      .level-cell { display:grid; place-items:center; height:32px; background:#303949; color:#c0cad8; border:1px solid #465166; border-radius:6px; font-size:12px; font-weight:650; }
      .level-cell[data-tone=green] { background:#173f2e; color:#86efac; border-color:#318757; }
      .level-cell[data-tone=yellow] { background:#4b3e12; color:#fde047; border-color:#a88b23; }
      .level-cell[data-tone=red] { background:#4b2227; color:#fca5a5; border-color:#a54e55; }
      .level-cell[aria-current=true] { outline:2px solid #fff; outline-offset:2px; }
      #watch-overlay { position:absolute; inset:0; width:550px; height:400px; pointer-events:none; z-index:2; }
      #watch-preparing { position:absolute; inset:0; background:#10141d; color:#a2aec1; display:grid; align-content:center; justify-items:center; padding:28px; text-align:center; pointer-events:none; z-index:3; }
      #watch-preparing[hidden] { display:none; }
      #watch-preparing strong { font-size:22px; color:#e7edf6; }
      #watch-preparing p { max-width:360px; line-height:1.6; }
      #watch-result { position:absolute; top:50%; left:24px; right:24px; transform:translateY(-50%); max-width:520px; margin:0 auto; padding:24px; border:1px solid var(--result-color); border-top:5px solid var(--result-color); border-radius:12px; background:rgba(16,20,29,.94); box-shadow:0 12px 48px #0008; text-align:center; pointer-events:none; z-index:4; }
      #watch-result[hidden] { display:none; }
      #watch-result-level { color:#c3ccda; font-size:12px; text-transform:uppercase; letter-spacing:.12em; }
      #watch-result-title { margin:10px 0; font-size:clamp(26px,4vw,44px); color:var(--result-color); line-height:1.1; }
      #watch-result-detail { margin:0; font-size:14px; color:#e7edf6; line-height:1.6; overflow-wrap:anywhere; }
      #watch-panel { padding:22px; border-left:1px solid #2c3444; overflow:auto; background:#181e29; }
      #watch-panel h1 { font-size:18px; margin:0 0 5px; }
      #watch-panel .muted { color:#a2aec1; font-size:12px; line-height:1.5; }
      #watch-panel h2 { font-size:11px; text-transform:uppercase; letter-spacing:.1em; color:#a2aec1; margin:24px 0 8px; }
      #watch-panel label { display:block; margin:10px 0; cursor:pointer; }
      #watch-panel input { accent-color:#91d8e8; vertical-align:middle; }
      @media(max-width:700px) { #watch-shell { grid-template-columns:1fr; grid-template-rows:minmax(220px,55vh) 1fr; } #watch-panel { border-left:0; border-top:1px solid #2c3444; padding:14px; } }
    `;
    document.head.append(style);
    const shell = document.createElement('div');
    shell.id = 'watch-shell';
    shell.innerHTML = `
      <main id="game-area"><div id="game-stage"><canvas id="watch-frozen" width="550" height="400" hidden></canvas><svg id="watch-overlay" viewBox="0 0 550 400" aria-hidden="true"></svg></div><div id="watch-preparing" hidden><strong>Preparing attempt</strong><p id="watch-preparing-detail"></p><p>Gameplay appears when the planner starts its attempt.</p></div><section id="watch-result" role="status" aria-live="polite" hidden><div id="watch-result-level"></div><h2 id="watch-result-title"></h2><p id="watch-result-detail"></p></section><section id="watch-status" aria-label="Current planner phase"><div id="watch-level">Waiting for game</div><div id="watch-phase" role="status">Starting</div><p id="watch-detail">Loading the original game.</p><div id="watch-progress"></div></section><div id="watch-actions"><button id="watch-next" hidden>Next level</button></div></main>
      <aside id="watch-panel" aria-label="Planner diagnostics">
        <h1>Planner watch</h1><p class="muted">Watch the planner’s decisions. The white outline marks the current level.</p>
        <h2>Levels</h2><div id="watch-levels" role="list" aria-label="Level results"></div>
        <p class="muted"><span style="color:#86efac">Green: cleared</span> · <span style="color:#fde047">Yellow: partial / attempted</span><br><span style="color:#fca5a5">Red: no route / unmodeled</span> · Grey: unattempted</p>
        <h2>Display</h2>
        <label><input type="checkbox" id="show-preparation" checked> Show preparation gameplay</label>
        <h2>Show on game</h2>
        <label><input type="checkbox" id="show-route" checked> <span style="color:#35c9cc">Route & targets</span></label>
        <label><input type="checkbox" id="show-player"> <span style="color:#f1bc62">Player trail</span></label>
        <label><input type="checkbox" id="show-enemies"> <span style="color:#d9a4ff">Enemy samples</span></label>
        <p class="muted">Overlays are visual guides, separate from the pixels read by the planner. Enemy samples show observed positions, not guaranteed future paths.</p>
      </aside>`;
    document.body.append(shell);
    const stage = document.getElementById('game-stage');
    const area = document.getElementById('game-area');
    const svg = document.getElementById('watch-overlay');
    const ns = 'http://www.w3.org/2000/svg';
    let state = {}, trail = [], enemies = [], lastTick = -1, suppressResult = false;
    const levelCells = new Map();
    for (let level=1; level<=30; level++) {
        const cell=document.createElement('span');
        cell.className='level-cell'; cell.dataset.level=level; cell.dataset.tone='grey';
        cell.setAttribute('role','listitem'); cell.textContent=level;
        cell.title=`Level ${level}: unattempted`; cell.setAttribute('aria-label',cell.title);
        document.getElementById('watch-levels').append(cell); levelCells.set(level,cell);
    }
    const frozen = document.getElementById('watch-frozen');
    const savedFrame = document.createElement('canvas'); savedFrame.width=550; savedFrame.height=400;
    const savedContext = savedFrame.getContext('2d');
    const probe = document.createElement('canvas'); probe.width=550; probe.height=400;
    const probeContext = probe.getContext('2d', {willReadFrequently:true});
    let frameArmed=false, referenceHUD=null, hasSavedFrame=false;
    function renderStatus() {
        const confirming = frameArmed && !frozen.hidden;
        if (state.level) document.getElementById('watch-level').textContent = `Level ${state.level} / 30${state.run ? ' · '+state.run : ''}`;
        document.getElementById('watch-phase').textContent = confirming ? 'Confirming outcome' : (state.phase || 'Starting');
        document.getElementById('watch-detail').textContent = confirming ? 'Holding the last gameplay frame while the game confirms the result.' : (state.detail || '');
        document.getElementById('watch-progress').textContent = `${state.tick || 0} ticks · ${((state.tick || 0)/30).toFixed(1)}s simulated${state.progress ? ' · '+state.progress : ''}`;
    }
    function holdFrame() {
        if (!hasSavedFrame) return;
        if (frozen.hidden) frozen.getContext('2d').drawImage(savedFrame,0,0);
        frozen.hidden=false;
        renderStatus();
    }
    function sampleFrame() {
        if (!frameArmed) return;
        const source=document.querySelector('ruffle-player')?.shadowRoot?.querySelector('canvas');
        if (!source) return;
        probeContext.drawImage(source,0,0,550,400);
        const pixels=probeContext.getImageData(0,0,550,400).data;
        // The level marker must still match the attempted level. Ignore tip
        // screens and frames where the player is absent during a transition.
        if (referenceHUD) {
            for (let y=0;y<27;y++) for (let x=225;x<325;x++) {
                const i=(y*550+x)*4;
                if (pixels[i]!==referenceHUD[i] || pixels[i+1]!==referenceHUD[i+1] || pixels[i+2]!==referenceHUD[i+2]) {
                    // Cover the transition in this same animation callback,
                    // before the next level or tip screen can be painted.
                    holdFrame();
                    return;
                }
            }
        }
        let left=550,right=-1,top=400,bottom=-1,count=0;
        for (let y=28;y<373;y+=2) for (let x=0;x<550;x+=2) {
            const i=(y*550+x)*4;
            if (pixels[i]>=245 && pixels[i+1]<=25 && pixels[i+2]<=25) {
                left=Math.min(left,x); right=Math.max(right,x); top=Math.min(top,y); bottom=Math.max(bottom,y); count++;
            }
        }
        const width=right-left+2, height=bottom-top+2;
        if (Math.min(width,height)<5 || Math.max(width,height)>1.25*Math.min(width,height) || count*4<.8*width*height) {
            holdFrame();
            return;
        }
        if (!frozen.hidden) { frozen.hidden=true; renderStatus(); }
        if (!referenceHUD) referenceHUD=pixels;
        savedContext.drawImage(probe,0,0); hasSavedFrame=true;
    }
    function beginAttempt() {
        frozen.hidden=true; referenceHUD=null; hasSavedFrame=false; frameArmed=true;
        sampleFrame();
    }
    const outcomes = {
        'Cleared': ['green', '#4ade80'],
        'Died': ['yellow', '#facc15'],
        'Partial route': ['yellow', '#facc15'],
        'Time limit': ['yellow', '#facc15'],
        'Exit not confirmed': ['yellow', '#facc15'],
        'No route': ['red', '#f87171'],
        'Unmodeled': ['red', '#f87171'],
        'Error': ['red', '#f87171']
    };
    function fit() {
        const scale = Math.min(area.clientWidth/550, area.clientHeight/400);
        stage.style.transform = `scale(${scale})`;
        stage.style.left = `${Math.max(0,(area.clientWidth-550*scale)/2)}px`;
        stage.style.top = `${Math.max(0,(area.clientHeight-400*scale)/2)}px`;
    }
    new ResizeObserver(fit).observe(area);
    fit();
    function shape(tag, attributes) {
        const node = document.createElementNS(ns, tag);
        for (const [key,value] of Object.entries(attributes)) node.setAttribute(key,value);
        svg.append(node);
    }
    function line(points, color, opacity, dashed=false) {
        if (!points || points.length<2) return;
        shape('polyline', {points:points.map(p=>p.join(',')).join(' '), fill:'none', stroke:color,
            'stroke-width':1.4, opacity, 'stroke-dasharray':dashed?'4 3':'none'});
    }
    function draw() {
        renderStatus();
        const outcome = !suppressResult && outcomes[state.phase];
        const result = document.getElementById('watch-result');
        result.hidden = !outcome;
        document.getElementById('watch-status').hidden = Boolean(outcome);
        document.getElementById('watch-phase').style.color = outcome ? outcome[1] : '#91d8e8';
        if (outcome) {
            result.dataset.tone = outcome[0];
            result.style.setProperty('--result-color', outcome[1]);
            document.getElementById('watch-result-level').textContent = `Level ${state.level || '?'} result${!frozen.hidden ? ' · last gameplay frame' : ''}`;
            document.getElementById('watch-result-title').textContent = state.phase;
            document.getElementById('watch-result-detail').textContent = state.detail || '';
        }
        const preparing = ['Starting level', 'Reading level', 'Observing enemies', 'Planning route'].includes(state.phase);
        document.getElementById('watch-preparing').hidden = !preparing || document.getElementById('show-preparation').checked;
        document.getElementById('watch-preparing-detail').textContent = state.detail || '';
        svg.replaceChildren();
        if (document.getElementById('show-route').checked) {
            line(state.route, '#35c9cc', .75, true);
            for (const point of state.targets || []) shape('circle', {cx:point[0],cy:point[1],r:5,fill:'none',stroke:'#35c9cc','stroke-width':1.3,opacity:.85});
        }
        if (document.getElementById('show-player').checked) line(trail,'#f1bc62',.7);
        if (document.getElementById('show-enemies').checked) {
            for (const point of enemies) shape('circle',{cx:point[0],cy:point[1],r:1.8,fill:'#d9a4ff',opacity:.25});
        }
    }
    for (const control of document.querySelectorAll('#watch-panel input')) control.onchange = () => {
        draw();
        document.querySelector('ruffle-player')?.focus();
    };
    const next = document.getElementById('watch-next');
    next.onclick = () => { window.watchUI.next = true; next.disabled = true; };
    window.watchUI = {
      next:false,
      sampleFrame,
      reset() { trail=[]; enemies=[]; lastTick=-1; state.route=[]; state.targets=[]; suppressResult=true; frameArmed=false; frozen.hidden=true; hasSavedFrame=false; referenceHUD=null; draw(); },
      update(patch) {
        if (patch.phase) suppressResult = false;
        Object.assign(state,patch);
        if (['Running controller', 'Following route', 'Following partial route', 'Playing replay'].includes(patch.phase)) beginAttempt();
        const outcome = outcomes[patch.phase];
        if (outcome && state.level) {
            const cell=levelCells.get(state.level);
            if (cell) {
                cell.dataset.tone=outcome[0]; cell.title=`Level ${state.level}: ${patch.phase}`;
                cell.setAttribute('aria-label',cell.title);
            }
            frameArmed=false;
            if (patch.phase==='Cleared' && hasSavedFrame) holdFrame();
            else frozen.hidden=true;
        }
        for (const [level,cell] of levelCells) cell.setAttribute('aria-current',String(level===state.level));
        if (patch.clearTraces) { trail=[]; enemies=[]; lastTick=-1; }
        if (patch.tick!==undefined && patch.tick!==lastTick) {
            if (patch.player) trail.push(patch.player);
            if (patch.enemies) enemies.push(...patch.enemies.filter(Boolean));
            trail=trail.slice(-160); enemies=enemies.slice(-240); lastTick=patch.tick;
        }
        if (patch.enemySamples) enemies=patch.enemySamples.slice(-240);
        if (patch.waiting!==undefined) { next.hidden=!patch.waiting; next.disabled=false; window.watchUI.next=false; }
        draw();
      }
    };
}"""
