"""Opt-in provider estimates from public stats only. No account credentials."""
import json
import sqlite3
import os
import re
import base64
import hashlib
import secrets
import time
import math
import calendar
from datetime import datetime, timezone
from contextlib import contextmanager, closing
from pathlib import Path


def timestamp(value):
    if not isinstance(value,str): raise ValueError('Missing source timestamp')
    at=datetime.fromisoformat(value.replace('Z', '+00:00'))
    if at.tzinfo is None: raise ValueError('Source timestamp needs a timezone')
    return int(at.timestamp())


def counter(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def base_ceiling(ram, eligible_minutes, hour):
    """Public scenario only: hidden pool, health and ownership gates remain unknown."""
    monthly = next((usd for gb, usd in [(512,40),(192,30),(128,26),(96,22),(64,18),(48,16),(32,12),(24,10)] if ram is not None and ram >= gb), 0)
    at = datetime.fromtimestamp(hour, timezone.utc)
    availability = max(0, min(1, (eligible_minutes / 60 - .9) / .1))
    return monthly / (calendar.monthrange(at.year, at.month)[1] * 24) * availability


class ProviderPages:
    def __init__(self, path, pages, track_all=False):
        self.path = Path(path)
        self.pages = pages
        self.track_all = track_all
        self.enabled = bool(pages or track_all)
        if not self.enabled:
            return
        with self.connect() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='public_provider_samples'").fetchone()
            all_schema = db.execute("SELECT 1 FROM sqlite_master WHERE name='public_provider_hardware'").fetchone()
            quality = db.execute("SELECT 1 FROM sqlite_master WHERE name='public_provider_quality'").fetchone()
            if not exists or (track_all and not all_schema) or not quality:
                suffix='.before-provider-quality.backup' if exists and all_schema else ('.before-all-public-providers.backup' if exists else '.before-provider-pages.backup')
                backup = self.path.with_name(self.path.name + suffix)
                # Create once without overwriting; fail startup if backup fails.
                try:
                    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                except FileExistsError:
                    with closing(sqlite3.connect(f'file:{backup}?mode=ro', uri=True)) as saved:
                        if saved.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise RuntimeError('Provider-page migration backup is invalid')
                else:
                    os.close(fd)
                    with closing(sqlite3.connect(backup)) as saved:
                        db.backup(saved)
                        if saved.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                            raise RuntimeError('Provider-page migration backup failed')
                    print('Public-provider migration: consistent private SQLite backup verified', flush=True)
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS public_provider_samples (
                    provider TEXT PRIMARY KEY, at INTEGER, model TEXT, tokens INTEGER);
                CREATE TABLE IF NOT EXISTS public_provider_chips (
                    provider TEXT PRIMARY KEY, chip TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS public_provider_hardware (
                    provider TEXT PRIMARY KEY, ram INTEGER, status TEXT);
                CREATE TABLE IF NOT EXISTS public_provider_usage (
                    provider TEXT, hour INTEGER, model TEXT, output INTEGER,
                    PRIMARY KEY(provider,hour,model));
                CREATE TABLE IF NOT EXISTS public_network_minutes (
                    at INTEGER PRIMARY KEY, input INTEGER, output INTEGER);
                CREATE TABLE IF NOT EXISTS public_hour_prices (
                    hour INTEGER, model TEXT, input REAL, output REAL,
                    PRIMARY KEY(hour,model));
                CREATE INDEX IF NOT EXISTS public_provider_usage_hour ON public_provider_usage(hour);
                CREATE TABLE IF NOT EXISTS public_provider_keys (
                    provider TEXT PRIMARY KEY, public_key TEXT NOT NULL, fingerprint TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS public_provider_keys_fingerprint ON public_provider_keys(fingerprint);
                CREATE TABLE IF NOT EXISTS public_key_fleets (
                    slug TEXT PRIMARY KEY, keys_json TEXT NOT NULL, created_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS public_network_buckets (
                    at INTEGER PRIMARY KEY, input INTEGER NOT NULL, output INTEGER NOT NULL, fetched_at INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS public_network_calibration (
                    hour INTEGER PRIMARY KEY, at INTEGER NOT NULL, usd_per_output REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS public_provider_quality (
                    provider TEXT, hour INTEGER, minutes INTEGER NOT NULL DEFAULT 0,
                    eligible INTEGER NOT NULL DEFAULT 0, resets INTEGER NOT NULL DEFAULT 0,
                    gaps INTEGER NOT NULL DEFAULT 0, longest_gap INTEGER NOT NULL DEFAULT 0,
                    ram INTEGER, PRIMARY KEY(provider,hour));
            ''')
            # Additive migration: do not rewrite legacy usage or pretend it has job coverage.
            for table, columns in {
                'public_provider_samples': [('requests','INTEGER'),('models','TEXT'),('rates','TEXT')],
                'public_provider_usage': [('requests','INTEGER')],
                'public_hour_prices': [('at','INTEGER')],
            }.items():
                existing={r[1] for r in db.execute('PRAGMA table_info('+table+')')}
                for name, kind in columns:
                    if name not in existing: db.execute('ALTER TABLE '+table+' ADD COLUMN '+name+' '+kind)

    def capture_network(self, series, totals, stats, fetched_at):
        """Only closed, aligned 30m buckets; never consume stats.time_series."""
        if series.get('bucket_seconds') != 1800 or series.get('window') != '24h':
            raise ValueError('Unexpected public network series schema')
        end=timestamp(series['end_at']); start=timestamp(series['start_at'])
        if end % 1800 or start % 1800 or end-start != 86400 or not 0 <= fetched_at-end <= 3600:
            raise ValueError('Invalid network series boundaries')
        parsed={}
        for row in series.get('time_series',[]):
            at=timestamp(row['timestamp']); inp=counter(row.get('prompt_tokens')); out=counter(row.get('completion_tokens'))
            if at in parsed or at % 1800 or not start <= at < end or inp is None or out is None:
                raise ValueError('Invalid or duplicate network bucket')
            parsed[at]=(inp,out)
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO public_network_buckets VALUES(?,?,?,?)',[(at,i,o,fetched_at) for at,(i,o) in parsed.items()])
            # totals.tokens includes INPUT: never use it as an output denominator.
            # Match the rolling 24h stats output denominator within 120 seconds.
            work=counter(totals.get('work_earnings_micro_usd')); output=counter(stats.get('last_24h_completion_tokens'))
            if work is None or not output or not totals.get('updated_at'):
                return  # Keep valid ratio buckets even when payout calibration is unavailable.
            total_at=timestamp(totals['updated_at']); stats_at=timestamp(stats['snapshot_at'])
            if totals.get('window')=='24h' and work is not None and output and abs(total_at-stats_at)<=120 and 0 <= fetched_at-total_at <= 180 and stats_at<=fetched_at:
                db.execute('INSERT OR REPLACE INTO public_network_calibration VALUES(?,?,?)',(stats_at//3600*3600,total_at,work/1e6/output))

    def create_fleet(self, keys):
        if not isinstance(keys,list) or not 1 <= len(keys) <= 26:
            raise ValueError('Paste 1–26 SE public keys, one per line.')
        canonical=[]
        for key in keys:
            if not isinstance(key,str) or len(key.strip()) != 88:
                raise ValueError('Each entry must be a complete SE public key, not a provider or machine ID.')
            try:
                raw=base64.b64decode(key.strip(),validate=True)
            except ValueError:
                raise ValueError('Invalid public key format.') from None
            if len(raw)!=64:
                raise ValueError('Invalid public key length.')
            canonical.append(base64.b64encode(raw).decode())
        canonical=list(dict.fromkeys(canonical))
        now=int(time.time())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM public_key_fleets WHERE created_at>?',(now-60,)).fetchone()[0]>=20 or db.execute('SELECT COUNT(*) FROM public_key_fleets').fetchone()[0]>=10000:
                raise ValueError('Fleet creation limit reached. Please try again later.')
            slug='fleet-'+secrets.token_hex(16)
            db.execute('INSERT INTO public_key_fleets VALUES(?,?,?)',(slug,json.dumps(canonical),now))
        return dict(url='/providers/'+slug,computers=len(canonical))

    def fleet_keys(self, slug):
        with self.connect() as db:
            row=db.execute('SELECT keys_json FROM public_key_fleets WHERE slug=?',(slug,)).fetchone()
        return json.loads(row[0]) if row else None

    def capture_keys(self, payload):
        with self.connect() as db:
            for row in payload.get('providers', []):
                if not isinstance(row,dict):continue
                pid, key = row.get('provider_id'), row.get('se_public_key')
                if not isinstance(pid, str) or not isinstance(key, str):
                    continue
                try:
                    raw = base64.b64decode(key, validate=True)
                except ValueError:
                    continue
                if len(raw) != 64:
                    continue
                fingerprint = hashlib.sha256(raw).hexdigest()
                db.execute('INSERT OR IGNORE INTO public_provider_keys VALUES(?,?,?)', (pid,key,fingerprint))

    def ids_for(self, slug):
        if slug in self.pages:
            return self.pages[slug]
        if self.track_all and re.fullmatch(r'fleet-[a-f0-9]{32}',slug):
            keys=self.fleet_keys(slug)
            if keys is not None:
                with self.connect() as db:
                    return [p[0] for key in keys for p in db.execute('SELECT provider FROM public_provider_keys WHERE public_key=? ORDER BY provider',(key,))]
        if self.track_all and re.fullmatch(r'se-[a-f0-9]{64}', slug):
            with self.connect() as db:
                ids = [r[0] for r in db.execute('SELECT provider FROM public_provider_keys WHERE fingerprint=? ORDER BY provider', (slug[3:],))]
            if ids:
                return ids
        if self.track_all and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', slug):
            with self.connect() as db:
                if db.execute('SELECT 1 FROM public_provider_samples WHERE provider=?',(slug,)).fetchone():
                    return [slug]
        raise KeyError('Unknown provider page')

    def directory(self, search='', offset=0):
        # Never enumerate private aggregate-page slugs or account memberships.
        query='%'+search[:128].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.connect() as db:
            sql="""FROM (SELECT s.*,k.public_key,k.fingerprint,
                   ROW_NUMBER() OVER(PARTITION BY k.fingerprint ORDER BY s.at DESC,s.provider) AS rank
                   FROM public_provider_samples s JOIN public_provider_keys k ON k.provider=s.provider) s
                   LEFT JOIN public_provider_chips c ON c.provider=s.provider
                   LEFT JOIN public_provider_hardware h ON h.provider=s.provider
                   WHERE s.rank=1 AND (s.public_key LIKE ? ESCAPE '\\' OR c.chip LIKE ? ESCAPE '\\' OR s.model LIKE ? ESCAPE '\\')"""
            args=(query,query,query)
            count=db.execute('SELECT COUNT(*) '+sql,args).fetchone()[0]
            rows=db.execute('SELECT s.public_key,s.fingerprint,c.chip,h.ram,h.status,s.model,s.at '+sql+' ORDER BY s.fingerprint LIMIT 100 OFFSET ?',args+(offset,)).fetchall()
        return dict(total=count,offset=offset,limit=100,rows=[dict(public_key=k,page_slug='se-'+f,chip=c or 'Unknown chip',ram=ram,status=st or 'unknown',model=m,last_seen=at) for k,f,c,ram,st,m,at in rows])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def capture(self, payload, prices, now, attestation=None, pricing_at=None):
        if payload.get('snapshot_at'):
            now=timestamp(payload['snapshot_at'])
        attest={r.get('provider_id'):r for r in (attestation or {}).get('providers',[])}
        wanted = {p for ids in self.pages.values() for p in ids}
        with self.connect() as db:
            for model, price in prices.items():
                if all(v is not None and math.isfinite(v) and v>=0 for v in [price.input_usd,price.output_usd]):
                    db.execute('INSERT OR REPLACE INTO public_hour_prices(hour,model,input,output,at) VALUES(?,?,?,?,?)',
                               (now // 3600 * 3600, model, price.input_usd, price.output_usd,now if pricing_at is None else pricing_at))
            for row in payload.get('providers', []):
                pid = row.get('id')
                if not isinstance(pid,str) or (not self.track_all and pid not in wanted):
                    continue
                if self.track_all and not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',pid):
                    continue
                value=counter(row.get('tokens_generated'))
                requests=counter(row.get('requests_served'))
                previous = db.execute('SELECT at,model,tokens,requests,models,rates FROM public_provider_samples WHERE provider=?', (pid,)).fetchone()
                # Duplicate/out-of-order snapshots cannot change the baseline or coverage.
                if previous and now<=previous[0]: continue
                chip = str(row.get('chip') or '').strip()[:80]
                if chip:
                    db.execute('INSERT OR REPLACE INTO public_provider_chips VALUES(?,?)', (pid,chip))
                ram=row.get('memory_gb')
                ram=ram if isinstance(ram,int) and not isinstance(ram,bool) and ram>=0 else None
                db.execute('INSERT OR REPLACE INTO public_provider_hardware VALUES(?,?,?)',(pid,ram,str(row.get('status') or 'unknown')[:32]))
                model = row.get('current_model') or ''
                tokens = value
                catalog=row.get('models')
                models=sorted(set(m for m in catalog if isinstance(m,str) and m))[:64] if isinstance(catalog,list) else []
                hour=now//3600*3600; minute=(now%3600)//60; bit=1<<minute
                authorization=attest.get(pid,{})
                os_version=str(row.get('os_version') or '')
                os_match=re.match(r'^(\d+)(?:\.|$)',os_version)
                eligible=(authorization.get('app_attest_authorized') is True and
                          authorization.get('status') in ('online','serving') and
                          row.get('status') in ('online','serving') and bool(model) and
                          os_match is not None and int(os_match[1])>=27)
                db.execute('INSERT INTO public_provider_quality(provider,hour,minutes,eligible,ram) VALUES(?,?,?,?,?) ON CONFLICT(provider,hour) DO UPDATE SET minutes=minutes|excluded.minutes,eligible=eligible|excluded.eligible,ram=CASE WHEN ram IS NULL THEN excluded.ram WHEN excluded.ram IS NULL THEN ram ELSE MIN(ram,excluded.ram) END',
                           (pid,hour,bit,bit if eligible else 0,ram))
                rates=json.loads(previous[5] or '[]') if previous else []
                elapsed=now-previous[0] if previous else 0
                delta=tokens-previous[2] if previous and tokens is not None and previous[2] is not None else None
                jobs=requests-previous[3] if previous and requests is not None and previous[3] is not None else None
                reset=(delta is not None and delta<0) or (jobs is not None and jobs<0)
                if elapsed and delta is not None and delta>=0:
                    # Bounded per-session rolling positive-rate history. Bootstrap cap
                    # protects an initial lifetime restore before a p95 exists.
                    p95=sorted(rates)[math.ceil(.95*len(rates))-1] if len(rates)>=5 else None
                    cap=min(10000,20*p95) if p95 else 10000
                    reset=reset or delta/elapsed>cap or (jobs is not None and jobs/elapsed>1000)
                    if previous[2]==0 and delta>0:
                        old=db.execute('SELECT MAX(s.tokens) FROM public_provider_samples s JOIN public_provider_keys k ON k.provider=s.provider WHERE s.provider!=? AND k.public_key=(SELECT public_key FROM public_provider_keys WHERE provider=?)',(pid,pid)).fetchone()[0]
                        if old is not None and old>0 and delta>=old: reset=True
                gap=elapsed>150
                if previous:
                    db.execute('UPDATE public_provider_quality SET resets=resets+?,gaps=gaps+?,longest_gap=MAX(longest_gap,?) WHERE provider=? AND hour=?',(int(reset),int(gap),elapsed,pid,hour))
                    if gap:
                        # Record the outage in every overlapping hour, not just
                        # the hour in which collection finally resumes.
                        for gap_hour in range(max(previous[0]//3600*3600,hour-38*86400),hour,3600):
                            db.execute('INSERT INTO public_provider_quality(provider,hour,gaps,longest_gap) VALUES(?,?,1,?) ON CONFLICT(provider,hour) DO UPDATE SET gaps=gaps+1,longest_gap=MAX(longest_gap,excluded.longest_gap)',(pid,gap_hour,elapsed))
                if previous and 0<elapsed<=150 and not reset and (delta is not None or jobs is not None):
                    if delta is not None and delta>0: rates=(rates+[delta/elapsed])[-32:]
                    old_models=json.loads(previous[4] or '[]')
                    # models is an advertised catalog, NOT a loaded-model list.
                    # Conservatively use a shared-catalog range; never assert a
                    # precise current-model attribution for a multi-model catalog.
                    if model != previous[1] or not model:
                        attributed='Unattributed model switch'
                    elif len(set(models+old_models))>1:
                        attributed='Shared catalog: '+json.dumps(sorted(set(models+old_models)),separators=(',',':'))
                    else: attributed=model
                    # Split a boundary interval proportionally, never count lifetime totals.
                    start = previous[0]
                    remaining_output=delta or 0; remaining_jobs=jobs
                    while start < now:
                        hour = start // 3600 * 3600
                        end = min(now, hour + 3600)
                        part = remaining_output if end == now else round(remaining_output * (end-start)/(now-start))
                        job_part=remaining_jobs if end==now or remaining_jobs is None else round(remaining_jobs*(end-start)/(now-start))
                        db.execute('INSERT INTO public_provider_usage(provider,hour,model,output,requests) VALUES(?,?,?,?,?) ON CONFLICT(provider,hour,model) DO UPDATE SET output=output+excluded.output,requests=CASE WHEN requests IS NULL OR excluded.requests IS NULL THEN NULL ELSE requests+excluded.requests END',
                                   (pid, hour, attributed, part,job_part))
                        remaining_output-=part
                        if remaining_jobs is not None: remaining_jobs-=job_part
                        start = end
                if reset: rates=[]
                db.execute('INSERT OR REPLACE INTO public_provider_samples(provider,at,model,tokens,requests,models,rates) VALUES(?,?,?,?,?,?,?)', (pid,now,model,tokens,requests,json.dumps(models),json.dumps(rates)))
            cutoff = now - 38*86400
            for table, field in [('public_provider_usage','hour'), ('public_network_buckets','at'), ('public_network_calibration','hour'), ('public_provider_quality','hour'), ('public_network_minutes','at'), ('public_hour_prices','hour')]:
                db.execute(f'DELETE FROM {table} WHERE {field}<?', (cutoff,))

    def read(self, slug, now):
        ids = self.ids_for(slug)
        fleet_keys=self.fleet_keys(slug)
        start = (now-23*3600)//3600*3600
        rows = []
        with self.connect() as db:
            chips = {pid:(db.execute('SELECT chip FROM public_provider_chips WHERE provider=?',(pid,)).fetchone() or [''])[0] for pid in ids}
            names = [chips.get(pid) or f'Computer {chr(65+n)}' for n,pid in enumerate(ids)]
            labels = [f'{name} · {1+names[:n].count(name)}' if names.count(name)>1 else name for n,name in enumerate(names)]
            if slug.startswith('se-'):
                # Reconnected sessions of one public key remain one computer.
                latest_id = max(ids, key=lambda pid:(db.execute('SELECT at FROM public_provider_samples WHERE provider=?',(pid,)).fetchone() or [0])[0])
                labels = [chips.get(latest_id) or 'Computer'] * len(ids)
            if fleet_keys is not None:
                key_names=[]
                key_ids=[]
                for key in fleet_keys:
                    members=[r[0] for r in db.execute('SELECT k.provider FROM public_provider_keys k LEFT JOIN public_provider_samples s ON s.provider=k.provider WHERE k.public_key=? ORDER BY s.at DESC,k.provider',(key,))]
                    key_ids.append(members)
                    key_names.append(next((chips[p] for p in members if chips.get(p)), 'Computer · awaiting public observation'))
                key_labels=[f'{name} · {1+key_names[:n].count(name)}' if key_names.count(name)>1 else name for n,name in enumerate(key_names)]
                name_by_id={pid:key_labels[n] for n,members in enumerate(key_ids) for pid in members}
                labels=[name_by_id[pid] for pid in ids]
            buckets={at:(i,o) for at,i,o in db.execute('SELECT at,input,output FROM public_network_buckets WHERE at>=?',(start,))}
            ratios={}
            for h in range(start,now//3600*3600+1,3600):
                pair=[buckets[t] for t in (h,h+1800) if t in buckets]
                out=sum(o for i,o in pair)
                ratios[h]=(sum(i for i,o in pair)/out if len(pair)==2 and out>0 else None,len(pair))
            calibration={h:(at,rate) for h,at,rate in db.execute('SELECT hour,at,usd_per_output FROM public_network_calibration WHERE hour>=?',(start,))}
            prices = {(h,m):(i,o,at) for h,m,i,o,at in db.execute('SELECT hour,model,input,output,at FROM public_hour_prices WHERE hour>=?', (start,))}
            latest = max(((db.execute('SELECT at FROM public_provider_samples WHERE provider=?',(pid,)).fetchone() or [0])[0] for pid in ids),default=0) or None
            for n,pid in enumerate(ids):
                for h,m,out,jobs in db.execute('SELECT hour,model,output,requests FROM public_provider_usage WHERE provider=? AND hour>=?', (pid,start)):
                    ratio,coverage=ratios.get(h,(None,0))
                    shared=m.startswith('Shared catalog: ')
                    models=json.loads(m[len('Shared catalog: '):]) if shared else [m]
                    rates=[prices.get((h,mid)) for mid in models]
                    priced=bool(rates) and all(r is not None for r in rates)
                    inp = out*ratio if ratio is not None else None
                    floor_low=min(r[1] for r in rates)*out/1e6 if priced else None
                    floor_high=max(r[1] for r in rates)*out/1e6 if priced else None
                    ratio_values=[(inp*r[0]+out*r[1])/1e6 for r in rates] if priced and inp is not None else []
                    cal=calibration.get(h)
                    calibrated=out*cal[1] if cal else None
                    # Default chart uses the rolling network payout proxy, not API
                    # token value. Three alternatives are exposed, not averaged.
                    rows.append(dict(computer=labels[n],hour=h,model='Shared catalog' if shared else m,
                                     models=models,shared=shared,unattributed=m=='Unattributed model switch',
                                     output=out,requests=jobs,estimated_input=inp,
                                     estimated_usd=calibrated,calibrated_usd=calibrated,
                                     calibration_at=cal[0] if cal else None,
                                     output_floor_usd=floor_low,output_ceiling_usd=floor_high,
                                     ratio_usd=min(ratio_values) if ratio_values else None,
                                     ratio_high_usd=max(ratio_values) if ratio_values else None,
                                     ratio_buckets=coverage,ratio=ratio,
                                     pricing_fresh=all(r[2] is not None and h-900<=r[2]<h+3600 for r in rates) if priced else False))
            # Merge minute bitmaps across connection sessions by public-key label.
            # Never double-count reconnect overlap toward uptime or base rewards.
            quality={}
            for n,pid in enumerate(ids):
                for h,minutes,eligible,resets,gaps,longest,ram in db.execute('SELECT hour,minutes,eligible,resets,gaps,longest_gap,ram FROM public_provider_quality WHERE provider=? AND hour>=?',(pid,start)):
                    q=quality.setdefault((labels[n],h),dict(minutes=0,eligible=0,resets=0,gaps=0,longest_gap=0,ram=None))
                    q['minutes']|=minutes;q['eligible']|=eligible;q['resets']+=resets;q['gaps']+=gaps;q['longest_gap']=max(q['longest_gap'],longest)
                    if ram is not None:q['ram']=ram if q['ram'] is None else min(q['ram'],ram)
            coverage_rows=[]
            for (computer,h),q in sorted(quality.items()):
                samples=q['minutes'].bit_count(); eligible=q['eligible'].bit_count()
                usage=[r for r in rows if r['computer']==computer and r['hour']==h]
                output=sum(r['output'] for r in usage)
                ambiguous=sum(r['output'] for r in usage if r['shared'] or r['unattributed'])
                expected=60 if h+3600<=now else max(0,min(60,(now-h)//60))
                coverage_rows.append(dict(computer=computer,hour=h,samples=samples,expected_samples=expected,
                    longest_gap=q['longest_gap'],resets=q['resets'],gaps=q['gaps'],
                    ambiguous_output_share=ambiguous/output if output else None,
                    ratio_buckets=ratios.get(h,(None,0))[1],pricing_fresh=all(r['pricing_fresh'] for r in usage) if usage else None,
                    # Approximate the source's CLOSED 5m settlement periods,
                    # not one nonlinear availability ramp over the whole hour.
                    base_estimate_usd=sum(base_ceiling(q['ram'],((q['eligible']>>(block*5))&31).bit_count()*12,h)/12 for block in range(12)) if h+3600<=now else None,
                    base_label='up to · public eligibility scenario',eligible_minutes=eligible))
        return dict(rows=rows, computers=key_labels if fleet_keys is not None else list(dict.fromkeys(labels)), last_sample_at=latest, start=start, now=now,
                    coverage=coverage_rows,estimate_method='public_counter_proxies_v2',
                    page_title='My fleet · public estimates' if slug in self.pages or fleet_keys is not None else 'Provider · public estimates')


def load_pages(value):
    import re
    pages = json.loads(value or '{}')
    if not isinstance(pages, dict):
        raise ValueError('PROVIDER_PAGES_JSON must be a slug-to-provider-ID-list object')
    for slug, ids in pages.items():
        if not re.fullmatch(r'[a-z0-9-]{1,64}', slug) or not isinstance(ids,list) or not 1 <= len(ids) <= 26 or len(ids)!=len(set(ids)):
            raise ValueError('Invalid provider page configuration')
        if any(not isinstance(p,str) or not re.fullmatch(r'[a-f0-9-]{36}',p) for p in ids):
            raise ValueError('Invalid provider ID')
    return pages
