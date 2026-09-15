/* Public per-model charts. No account data, visitor identifiers, or storage. */
const ModelCharts = (() => {
  const hiddenLive = new Set();
  const hiddenScore = new Set();
  const cards = new Map();
  const number = new Intl.NumberFormat(undefined, {maximumFractionDigits: 1});
  const finite = Number.isFinite;
  const ratio = (requests, loaded) => finite(requests) && finite(loaded) && loaded > 0 ? requests / loaded * 100 : null;
  const percent = value => finite(value) ? `${number.format(value)}%` : '—';
  const scoreLabel = value => finite(value) ? value.toFixed(4) : '—';
  const scale = (value, min, max, low, high) => low + (value - min) / (max - min || 1) * (high - low);

  function displayName(id) {
    const known = {
      'gemma-4-26b-qat-4bit': 'Gemma 4 26B', 'gemma-4-26b-8bit': 'Gemma 4 26B · 8-bit',
      'qwen3.6-35b-a3b-vl-mtp-mxfp8': 'Qwen 3.6 35B', 'eigenlabs/qwen3.8-27b-4bit-mtp': 'Qwen 3.8 27B',
      'qwen3-vl-30b-a3b-instruct': 'Qwen 3 VL 30B', 'qwen3.5-35b-a3b': 'Qwen 3.5 35B',
    };
    if (known[id.toLowerCase()]) return known[id.toLowerCase()];
    const oss = id.match(/gpt-oss-(\d+b)/i);
    if (oss) return `GPT OSS ${oss[1].toUpperCase()}`;
    return id.split('/').at(-1).replace(/-/g, ' ');
  }
  function groups(rows) {
    const result = new Map();
    for (const row of rows) {
      const time = Date.parse(row.observed_at);
      if (!finite(time)) continue;
      if (!result.has(row.model_id)) result.set(row.model_id, []);
      result.get(row.model_id).push({...row, time});
    }
    for (const points of result.values()) points.sort((a, b) => a.time - b.time);
    return [...result].sort(([a, ap], [b, bp]) => {
      const as = ap.at(-1).score, bs = bp.at(-1).score;
      return (finite(bs) ? bs : -Infinity) - (finite(as) ? as : -Infinity) || a.localeCompare(b);
    });
  }
  function svgNode(name, attrs = {}, text = '') {
    const node = document.createElementNS('http://www.w3.org/2000/svg', name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (text) node.textContent = text;
    return node;
  }
  function html(name, className, text = '') {
    const node = document.createElement(name);
    node.className = className;
    node.textContent = text;
    return node;
  }
  function path(points, key, x, y, maxGap) {
    let previous = null;
    return points.map(point => {
      if (!finite(point[key])) { previous = null; return ''; }
      const move = !previous || point.time - previous.time > maxGap;
      previous = point;
      return `${move ? 'M' : 'L'}${x(point.time).toFixed(1)},${y(point[key]).toFixed(1)}`;
    }).join(' ');
  }
  function spread(entries, low, high) {
    const sorted = entries.map(item => ({...item, labelY: item.y})).sort((a, b) => a.y - b.y);
    for (let i = 1; i < sorted.length; i++) sorted[i].labelY = Math.max(sorted[i].labelY, sorted[i-1].labelY + 17);
    if (sorted.length) {
      const overflow = Math.max(0, sorted.at(-1).labelY - high);
      for (const item of sorted) item.labelY -= overflow;
      const underflow = Math.max(0, low - sorted[0].labelY);
      for (const item of sorted) item.labelY += underflow;
    }
    return sorted;
  }
  function timeLabel(time, window) {
    const date = new Date(time);
    if (window <= 43200) return date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    if (window <= 604800) return date.toLocaleString([], {weekday: 'short', hour: 'numeric'});
    return date.toLocaleDateString([], {month: 'short', day: 'numeric'});
  }
  function draw(container, id, points, data) {
    const width = Math.max(280, container.clientWidth);
    const showLive = !hiddenLive.has(id), showScore = !hiddenScore.has(id);
    const height = showScore ? 380 : 290;
    const left = width < 520 ? 53 : 72, right = width - (width < 520 ? 89 : 124);
    const top = 20, bottom = 248;
    const end = Date.parse(data.generated_at), start = end - data.window_seconds * 1000;
    const x = time => scale(time, start, end, left, right);
    const values = points.flatMap(p => [p.average_loaded, p.average_requests, ...(showLive ? [p.requests] : [])]).filter(finite);
    const max = Math.max(1, ...values) * 1.1;
    const y = value => scale(value, 0, max, bottom, top);
    const maxGap = Math.max(150000, data.bucket_seconds * 2500);
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': `${displayName(id)} loaded models, requests and score history`});
    svg.append(svgNode('title', {}, `${displayName(id)} model load`));
    svg.append(svgNode('desc', {}, 'Green and blue use the same count scale. Green is the 15-minute loaded-model average; blue is the request average. Gray is the latest sampled request count. Score has its own aligned strip below.'));
    for (let i = 0; i <= 4; i++) svg.append(svgNode('line', {x1:left, x2:right, y1:y(max*i/4), y2:y(max*i/4), class:'grid'}));
    svg.append(svgNode('rect', {x:left, y:top, width:right-left, height:bottom-top, class:'frame'}));
    const series = [['average_loaded','loaded'], ['average_requests','requests']];
    if (showLive) series.unshift(['requests','live']);
    for (const [key, color] of series) svg.append(svgNode('path', {d:path(points,key,x,y,maxGap), class:`model-line ${color}`}));
    const last = points.at(-1);
    if (!values.length) svg.append(svgNode('text', {x:(left+right)/2,y:(top+bottom)/2,'text-anchor':'middle',class:'axis'}, 'Count history starts with this update'));
    const leftLabels = [], rightLabels = [];
    for (const [key, color] of [['average_loaded','loaded'], ['average_requests','requests']]) {
      if (!finite(last[key])) continue;
      const atY = y(last[key]);
      svg.append(svgNode('line', {x1:left,x2:right,y1:atY,y2:atY,class:`model-guide ${color}`}));
      svg.append(svgNode('circle', {cx:x(last.time),cy:atY,r:3.8,class:`model-point ${color}`}));
      leftLabels.push({y:atY,color,text:number.format(last[key])});
      rightLabels.push({y:atY,color,text:color === 'loaded' ? (last[key] > 0 ? '100%' : '—') : percent(ratio(last.average_requests,last.average_loaded))});
    }
    if (showLive && finite(last.requests)) {
      svg.append(svgNode('circle', {cx:x(last.time),cy:y(last.requests),r:2.5,class:'model-point live'}));
      // Same loaded baseline as the other right-hand labels so positions and
      // percentages agree, including when live and averaged load differ.
      rightLabels.push({y:y(last.requests),color:'live',text:`Live ${percent(ratio(last.requests,last.average_loaded))}`});
    }
    for (const label of spread(leftLabels,top+6,bottom-6)) {
      svg.append(svgNode('line', {x1:left-5,x2:left,y1:label.labelY,y2:label.y,class:`model-link ${label.color}`}));
      svg.append(svgNode('text', {x:left-8,y:label.labelY+4,'text-anchor':'end',class:`model-value ${label.color}`}, label.text));
    }
    for (const label of spread(rightLabels,top+6,bottom-6)) {
      svg.append(svgNode('polyline', {points:`${x(last.time)},${label.y} ${right+4},${label.y} ${right+8},${label.labelY}`,class:`model-link ${label.color}`}));
      svg.append(svgNode('text', {x:right+11,y:label.labelY+4,class:`model-value ${label.color}`}, label.text));
    }
    if (showScore) {
      const scoreTop = 279, scoreBottom = 335;
      const maxScore = Math.max(0.01, ...points.map(p => p.score).filter(finite)) * 1.1;
      const sy = value => scale(value,0,maxScore,scoreBottom,scoreTop+13);
      svg.append(svgNode('line', {x1:left,x2:right,y1:scoreTop-13,y2:scoreTop-13,class:'grid'}));
      svg.append(svgNode('line', {x1:left,x2:right,y1:scoreBottom,y2:scoreBottom,class:'grid'}));
      svg.append(svgNode('text', {x:left,y:scoreTop,class:'model-value score'}, width < 520 ? 'Score' : `Score · 0–${scoreLabel(maxScore)}`));
      svg.append(svgNode('text', {x:right,y:scoreTop,'text-anchor':'end',class:'model-value score'}, `${width < 520 ? '' : 'Latest '}${scoreLabel(last.score)}`));
      svg.append(svgNode('path', {d:path(points,'score',x,sy,maxGap),class:'model-line score'}));
      if (finite(last.score)) svg.append(svgNode('circle', {cx:x(last.time),cy:sy(last.score),r:3,class:'model-point score'}));
    }
    const ticks = width < 520 ? 3 : 5;
    for (let i=0;i<ticks;i++) {
      const time=start+(end-start)*i/(ticks-1);
      svg.append(svgNode('text', {x:x(time),y:height-25,'text-anchor':i===0?'start':i===ticks-1?'end':'middle',class:'axis'},timeLabel(time,data.window_seconds)));
    }
    svg.append(svgNode('text', {x:13,y:(top+bottom)/2,transform:`rotate(-90 13 ${(top+bottom)/2})`,'text-anchor':'middle',class:'axis-title'},'15m average count'));
    if (width >= 520) svg.append(svgNode('text', {x:width-5,y:(top+bottom)/2,transform:`rotate(90 ${width-5} ${(top+bottom)/2})`,'text-anchor':'middle',class:'axis-title'},'Relative to loaded models'));
    svg.append(svgNode('text', {x:(left+right)/2,y:height-4,'text-anchor':'middle',class:'axis-title'},'Shared time window'));
    container.replaceChildren(svg);
  }
  function makeCard(id) {
    const article=html('article','card model-card');
    article.dataset.modelId=id;
    const head=html('div','card-head'), about=html('div','model-about');
    about.append(html('h2','',displayName(id)),html('div','model-id',id));
    const availability=html('div','model-availability'); about.append(availability);
    const metric=html('div','model-metric');
    metric.append(html('div','metric-label','15m average requests / loaded models'));
    const numbers=html('div','metric-numbers'), usage=html('strong','metric-usage'), score=html('span','metric-score');
    numbers.append(usage,score); metric.append(numbers); head.append(about,metric);
    const legend=html('div','model-legend');
    legend.append(html('span','legend-title','15m averages'),html('span','legend-key loaded','Loaded models'),html('span','legend-key requests','Requests'));
    const chart=html('div','chart model-chart');
    const state={article,availability,usage,score,chart,points:[],data:null};
    for (const [label,color,hidden] of [['Live requests','live',hiddenLive],['Saved score','score',hiddenScore]]) {
      const button=html('button',`legend-key ${color}`,label);
      button.type='button'; button.setAttribute('aria-pressed',String(!hidden.has(id)));
      button.setAttribute('aria-label',`${label} for ${displayName(id)}`);
      button.addEventListener('click',()=>{
        hidden.has(id) ? hidden.delete(id) : hidden.add(id);
        button.setAttribute('aria-pressed',String(!hidden.has(id)));
        draw(chart,id,state.points,state.data);
      });
      legend.append(button);
    }
    const note=html('p','model-note'); state.note=note;
    article.append(head,legend,chart,note);
    return state;
  }
  function render(container, data) {
    const entries=groups(data.rows), active=new Set();
    for (const [id,points] of entries) {
      active.add(id);
      if (!cards.has(id)) cards.set(id,makeCard(id));
      const card=cards.get(id), last=points.at(-1);
      card.points=points; card.data=data;
      card.usage.textContent=percent(ratio(last.average_requests,last.average_loaded));
      card.score.textContent=`Score ${scoreLabel(last.score)}`;
      card.availability.textContent=finite(last.available_to_load) ? `${number.format(last.available_to_load)} available to load · latest sample` : 'Availability not recorded';
      const coverage=last.average_coverage_seconds;
      card.note.textContent=!finite(coverage) ? 'Loaded/request history was not recorded for these earlier scores.' :
        `One-minute snapshots · ${coverage < 900 ? `partial 15m average (${number.format(coverage/60)}m observed)` : 'full 15m average'} · gray is the sampled request count.`;
      if (Date.parse(data.generated_at)-last.time>180000) card.note.textContent+=' Last model sample is delayed.';
      container.append(card.article);
      draw(card.chart,id,points,data);
    }
    for (const [id,card] of cards) if (!active.has(id)) { card.article.remove(); cards.delete(id); }
  }
  return {render,ratio,groups,path,displayName};
})();
if (typeof module !== 'undefined') module.exports = ModelCharts;
