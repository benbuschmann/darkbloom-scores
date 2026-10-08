/* Shared hourly stacking and tooltips. No account data or credentials. */
(function(root) {
  const colors = ['#27c5b3','#579fff','#e4b93f','#bd8cf0','#ff9473','#72cb85'];
  const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const money = v => v == null ? 'Unavailable' : '$'+v.toFixed(4);
  function buckets(rows, groups, key, start) {
    return Array.from({length:24}, (_,i) => {
      const hour = start+i*3600, observed = rows.filter(r=>r.hour===hour);
      const values = groups.map(g => observed.filter(r=>r[key]===g && r.estimated_usd!=null).reduce((sum,r)=>sum+r.estimated_usd,0));
      return {hour, values, total:values.reduce((sum,v)=>sum+v,0), observed:observed.length>0,
              range_high:observed.filter(r=>r.estimated_usd!=null).reduce((sum,r)=>sum+(r.metric_high??r.estimated_usd),0),
              estimate_available:observed.some(r=>r.estimated_usd!=null),
              complete:observed.length>0 && observed.every(r=>r.estimated_usd!=null),
              unpriced:observed.filter(r=>r.estimated_usd==null).reduce((sum,r)=>sum+r.output,0),
              output:observed.reduce((sum,r)=>sum+r.output,0),
              requests:observed.length && observed.every(r=>r.requests!=null)?observed.reduce((sum,r)=>sum+r.requests,0):null,
              requests_partial:observed.some(r=>r.requests_partial || r.requests==null),
              estimates:Object.fromEntries(['output_floor_usd','output_ceiling_usd','calibrated_usd','ratio_usd','ratio_high_usd'].map(field=>[field,observed.length && observed.every(r=>r[field]!=null)?observed.reduce((sum,r)=>sum+r[field],0):null]))};
    });
  }
  function render(container, rows, groups, key, start, now) {
    const data = buckets(rows,groups,key,start);
    const max = Math.max(.001,...data.map(b=>Math.max(b.total,b.range_high)));
    const left=15, right=920, bottom=185, top=15, step=(right-left)/24, width=step*.72;
    let svg = `<svg viewBox="0 0 1000 225" role="group" aria-label="Hourly estimated value by ${key}">`;
    for(let i=0;i<=4;i++) {
      const y=bottom-(bottom-top)*i/4;
      svg+=`<path d="M${left} ${y}H${right}" stroke="#303943"/><text x="935" y="${y+4}" fill="#a5b1be" font-size="12">${money(max*i/4)}</text>`;
    }
    data.forEach((b,i)=>{
      let y=bottom;
      svg+=`<g ${b.hour===Math.floor(now/3600)*3600?'opacity=".6"':''}>`;
      b.values.forEach((v,j)=>{const h=v/max*(bottom-top);y-=h;
        svg+=`<rect x="${left+i*step+step*.14}" y="${y}" width="${width}" height="${h}" rx="2" fill="${colors[j%colors.length]}"/>`;
      });
      if(b.range_high>b.total){const highY=bottom-b.range_high/max*(bottom-top);svg+=`<rect x="${left+i*step+step*.14}" y="${highY}" width="${width}" height="${y-highY}" fill="#a5b1be22" stroke="#a5b1be" stroke-dasharray="3 3"/>`;}
      const label=new Date(b.hour*1000).toLocaleString([], {weekday:'short',hour:'numeric'});
      svg+=`<rect class="hour-hit" data-index="${i}" tabindex="0" role="button" aria-label="${esc(label)}: ${b.estimate_available?money(b.total)+(b.complete?'':' partial estimate'):b.observed?'Selected estimate unavailable':'No observations'}" x="${left+i*step}" y="${top}" width="${step}" height="${bottom-top}" fill="transparent"/>`;
      svg+='</g>';
      if(i%4===0)svg+=`<text x="${left+i*step}" y="213" fill="#a5b1be" font-size="12">${esc(new Date(b.hour*1000).toLocaleTimeString([],{hour:'numeric'}))}</text>`;
    });
    container.innerHTML=svg+'</svg><div class="hour-tooltip" role="status" hidden></div>';
    const tip=container.querySelector('.hour-tooltip');
    function show(target) {
      const i=Number(target.dataset.index), b=data[i];
      const label=new Date(b.hour*1000).toLocaleString([], {weekday:'short',hour:'numeric'});
      tip.innerHTML=`<div class="muted">${esc(label)}${b.hour===Math.floor(now/3600)*3600?' · partial hour':''}</div><strong>${b.estimate_available?money(b.total)+(b.complete?' estimated':' partial estimate'):b.observed?'Selected estimate unavailable':'No observations'}</strong>`+
        (b.observed?groups.map((g,j)=>`<div class="tooltip-row"><span><i class="dot" style="background:${colors[j%colors.length]}"></i>${esc(g)}</span><b>${money(b.values[j])}</b></div>`).join('')+`<div class="small">${b.output.toLocaleString()} observed output tokens · ${b.requests==null?'unknown':b.requests.toLocaleString()} observed requests${b.requests_partial?' (partial)':''}</div><div class="small">Output-only API proxy: ${money(b.estimates.output_floor_usd)}–${money(b.estimates.output_ceiling_usd)}</div><div class="small">Network payout proxy: ${money(b.estimates.calibrated_usd)}</div><div class="small">Input-ratio API proxy: ${money(b.estimates.ratio_usd)}–${money(b.estimates.ratio_high_usd)}</div>`:'')+
        (b.unpriced?`<div class="small">Plus ${b.unpriced.toLocaleString()} output tokens without the selected estimate</div>`:'');
      tip.hidden=false;
      // Anchor inside the chart; clamp so edge hours never overflow the card.
      const desired=(i+.5)/24*container.clientWidth-tip.offsetWidth/2;
      tip.style.left=Math.max(0,Math.min(container.clientWidth-tip.offsetWidth,desired))+'px';
    }
    container.querySelectorAll('.hour-hit').forEach(hit=>{
      hit.addEventListener('pointerenter',()=>show(hit));
      hit.addEventListener('focus',()=>show(hit));
      hit.addEventListener('click',()=>show(hit));
      hit.addEventListener('keydown',e=>{if(e.key==='Escape')tip.hidden=true;if(e.key==='Enter'||e.key===' '){e.preventDefault();show(hit)}});
      hit.addEventListener('blur',()=>{tip.hidden=true});
    });
    container.addEventListener('pointerleave',()=>{tip.hidden=true});
  }
  function legend(groups) {
    return groups.map((g,i)=>`<span><i class="dot" style="background:${colors[i%colors.length]}"></i>${esc(g)}</span>`).join('');
  }
  const api={buckets,render,legend};
  if(typeof module!=='undefined'&&module.exports)module.exports=api;
  else root.ProviderCharts=api;
})(typeof globalThis!=='undefined'?globalThis:this);
