"""Opt-in provider estimates from public stats only. No account credentials."""
import json
import sqlite3
import os
import re
from contextlib import contextmanager, closing
from pathlib import Path


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
            if not exists or (track_all and not all_schema):
                suffix='.before-all-public-providers.backup' if exists else '.before-provider-pages.backup'
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
            ''')

    def ids_for(self, slug):
        if slug in self.pages:
            return self.pages[slug]
        if self.track_all and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', slug):
            with self.connect() as db:
                if db.execute('SELECT 1 FROM public_provider_samples WHERE provider=?',(slug,)).fetchone():
                    return [slug]
        raise KeyError('Unknown provider page')

    def directory(self, search='', offset=0):
        # Never enumerate private aggregate-page slugs or account memberships.
        query='%'+search[:64].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%'
        with self.connect() as db:
            sql="""FROM public_provider_samples s LEFT JOIN public_provider_chips c ON c.provider=s.provider
                   LEFT JOIN public_provider_hardware h ON h.provider=s.provider
                   WHERE s.provider LIKE ? ESCAPE '\\' OR c.chip LIKE ? ESCAPE '\\' OR s.model LIKE ? ESCAPE '\\'"""
            args=(query,query,query)
            count=db.execute('SELECT COUNT(*) '+sql,args).fetchone()[0]
            rows=db.execute('SELECT s.provider,c.chip,h.ram,h.status,s.model,s.at '+sql+' ORDER BY s.provider LIMIT 100 OFFSET ?',args+(offset,)).fetchall()
        return dict(total=count,offset=offset,limit=100,rows=[dict(provider_id=p,chip=c or 'Unknown chip',ram=ram,status=st or 'unknown',model=m,last_seen=at) for p,c,ram,st,m,at in rows])

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def capture(self, payload, prices, now):
        wanted = {p for ids in self.pages.values() for p in ids}
        with self.connect() as db:
            for row in payload.get('time_series', []):
                from datetime import datetime
                at = int(datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')).timestamp())
                # Current minute is provisional; upsert as it fills in.
                db.execute('INSERT OR REPLACE INTO public_network_minutes VALUES(?,?,?)',
                           (at, max(0, int(row['prompt_tokens'])), max(0, int(row['completion_tokens']))))
            for model, price in prices.items():
                if price.input_usd is not None and price.output_usd is not None:
                    db.execute('INSERT OR REPLACE INTO public_hour_prices VALUES(?,?,?,?)',
                               (now // 3600 * 3600, model, price.input_usd, price.output_usd))
            for row in payload.get('providers', []):
                pid = row.get('id')
                if not isinstance(pid,str) or (not self.track_all and pid not in wanted):
                    continue
                if self.track_all and not re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',pid):
                    continue
                value=row.get('tokens_generated')
                # Missing counters are unknown, not a reset to zero.
                if isinstance(value,bool) or not isinstance(value,int) or value<0:
                    value=None
                chip = str(row.get('chip') or '').strip()[:80]
                if chip:
                    db.execute('INSERT OR REPLACE INTO public_provider_chips VALUES(?,?)', (pid,chip))
                ram=row.get('memory_gb')
                ram=ram if isinstance(ram,int) and not isinstance(ram,bool) and ram>=0 else None
                db.execute('INSERT OR REPLACE INTO public_provider_hardware VALUES(?,?,?)',(pid,ram,str(row.get('status') or 'unknown')[:32]))
                model = row.get('current_model') or ''
                tokens = value
                previous = db.execute('SELECT at,model,tokens FROM public_provider_samples WHERE provider=?', (pid,)).fetchone()
                if previous and tokens is not None and previous[2] is not None and 0 < now - previous[0] <= 150 and tokens >= previous[2]:
                    delta = tokens - previous[2]
                    attributed = model if model and model == previous[1] else 'Unattributed model switch'
                    # Split a boundary interval proportionally, never count lifetime totals.
                    start = previous[0]
                    while start < now:
                        hour = start // 3600 * 3600
                        end = min(now, hour + 3600)
                        part = delta if end == now else round(delta * (end-start)/(now-start))
                        db.execute('INSERT INTO public_provider_usage VALUES(?,?,?,?) ON CONFLICT(provider,hour,model) DO UPDATE SET output=output+excluded.output',
                                   (pid, hour, attributed, part))
                        delta -= part
                        start = end
                db.execute('INSERT OR REPLACE INTO public_provider_samples VALUES(?,?,?,?)', (pid,now,model,tokens))
            cutoff = now - 38*86400
            for table, field in [('public_provider_usage','hour'), ('public_network_minutes','at'), ('public_hour_prices','hour')]:
                db.execute(f'DELETE FROM {table} WHERE {field}<?', (cutoff,))

    def read(self, slug, now):
        ids = self.ids_for(slug)
        start = (now-23*3600)//3600*3600
        rows = []
        with self.connect() as db:
            chips = {pid:(db.execute('SELECT chip FROM public_provider_chips WHERE provider=?',(pid,)).fetchone() or [''])[0] for pid in ids}
            names = [chips.get(pid) or f'Computer {chr(65+n)}' for n,pid in enumerate(ids)]
            labels = [f'{name} · {1+names[:n].count(name)}' if names.count(name)>1 else name for n,name in enumerate(names)]
            ratios = {h: (i/o if o else None) for h,i,o in db.execute('SELECT (at/3600)*3600,SUM(input),SUM(output) FROM public_network_minutes WHERE at>=? GROUP BY 1', (start,))}
            prices = {(h,m):(i,o) for h,m,i,o in db.execute('SELECT hour,model,input,output FROM public_hour_prices WHERE hour>=?', (start,))}
            latest = max((db.execute('SELECT at FROM public_provider_samples WHERE provider=?',(pid,)).fetchone() or [0])[0] for pid in ids) or None
            for n,pid in enumerate(ids):
                for h,m,out in db.execute('SELECT hour,model,output FROM public_provider_usage WHERE provider=? AND hour>=?', (pid,start)):
                    ratio = ratios.get(h)
                    price = prices.get((h,m))
                    inp = out*ratio if ratio is not None else None
                    value = (inp*price[0]+out*price[1])/1e6 if inp is not None and price else None
                    rows.append(dict(computer=labels[n],hour=h,model=m,output=out,estimated_input=inp,estimated_usd=value))
        return dict(rows=rows, computers=labels, last_sample_at=latest, start=start, now=now,
                    page_title='Provider · public estimates' if slug not in self.pages else 'My fleet · public estimates')


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
