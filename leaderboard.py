"""Public, hourly activity comparison. Similarity is not ownership evidence."""
import json
import math
import sqlite3
from contextlib import closing
from provider_pages import timestamp, counter

RETENTION = 14 * 86400
MIN_INTERVALS = 6


def entries(payload, window, metric):
    if payload.get('window') != window or payload.get('metric') != metric:
        raise ValueError('Unexpected leaderboard window or metric')
    rows = payload.get('entries')
    if not isinstance(rows, list) or len(rows) > 200:
        raise ValueError('Invalid leaderboard rows')
    seen = set()
    for row in rows:
        name = row.get('pseudonym')
        if not isinstance(name, str) or not 1 <= len(name) <= 80 or name in seen:
            raise ValueError('Invalid or duplicate pseudonym')
        seen.add(name)
        for field in ('jobs', 'tokens', 'earnings_micro_usd', 'work_earnings_micro_usd', 'reward_earnings_micro_usd'):
            if counter(row.get(field)) is None:
                raise ValueError('Invalid leaderboard counter')
    return rows


def similarity(account, machine):
    """Conservative single-machine resemblance, not a probability of ownership."""
    if len(account) < MIN_INTERVALS or sum(account) < 100:
        return None
    mean = sum(account) / len(account)
    variance = sum((x - mean) ** 2 for x in account)
    other_mean = sum(machine) / len(machine)
    other_variance = sum((x - other_mean) ** 2 for x in machine)
    # Flat and idle time series cannot distinguish machines.
    if variance <= 0 or other_variance <= 0 or math.sqrt(variance / len(account)) / mean < .1:
        return None
    correlation = sum((a - mean) * (b - other_mean) for a, b in zip(account, machine)) / math.sqrt(variance * other_variance)
    error = sum(abs(a - b) for a, b in zip(account, machine)) / sum(account)
    if correlation < .9 or error > .15:
        return None
    return dict(agreement=round(100 * (1 - error), 1), correlation=round(correlation, 3),
                intervals=len(account), paid_jobs=sum(account), observed_requests=sum(machine))


def compare(snapshots):
    """Use only adjacent aligned checkpoints; absence never means zero."""
    snapshots = snapshots[-25:]  # Bounded CPU/memory: the most recent day of intervals.
    intervals = []
    for previous, current in zip(snapshots, snapshots[1:]):
        elapsed = current['source_at'] - previous['source_at']
        if not 2400 <= elapsed <= 4800:
            continue
        accounts = {r['pseudonym']: r for r in current['accounts']}
        old_accounts = {r['pseudonym']: r for r in previous['accounts']}
        paid_deltas = {}
        machine_deltas = {}
        for name in accounts.keys() & old_accounts.keys():
            paid = accounts[name]['jobs'] - old_accounts[name]['jobs']
            if paid >= 0:
                paid_deltas[name] = paid
        for key in current['machines'].keys() & previous['machines'].keys():
            new, old = current['machines'][key], previous['machines'][key]
            delta = new['jobs'] - old['jobs']
            coverage = new['minutes'] - old['minutes']
            if delta >= 0 and new['issues'] == old['issues'] and .9 * elapsed / 60 <= coverage <= elapsed / 60 + 4:
                machine_deltas[key] = delta
        intervals.append((paid_deltas, machine_deltas))
    grouped = {}
    latest = snapshots[-1]['machines'] if snapshots else {}
    names = set().union(*(set(a) for a, _ in intervals)) if intervals else set()
    if len(intervals) < MIN_INTERVALS:
        return []
    for name in names:
        for key in latest:
            pairs = [(a[name], m[key]) for a, m in intervals if name in a and key in m]
            if len(pairs) < MIN_INTERVALS:
                continue
            account, machine = zip(*pairs)
            if sum(account) < 100 or abs(sum(account)-sum(machine)) > .15*sum(account):
                continue
            fit = similarity(account, machine)
            if fit:
                grouped.setdefault(name, []).append(dict(pseudonym=name, public_page='/providers/se-' + key,
                    chip=latest[key]['chip'], ram=latest[key]['ram'], latest_model=latest[key]['model'], **fit))
    results = []
    for candidates in grouped.values():
        candidates.sort(key=lambda r: (-r['agreement'], r['public_page']))
        best = candidates[0]
        best['ambiguous'] = len(candidates) > 1 and best['agreement'] - candidates[1]['agreement'] < 5
        results.append(best)
    return sorted(results, key=lambda r: (-r['agreement'], r['pseudonym']))


def validate_fleet(snapshots, keys, name):
    """Known owner-supplied grouping, never inferred from a best-fitting subset.

    Only aggregate validation results leave this function: no private fleet URL
    or list of machine keys is returned. Does not alter any matching thresholds.
    """
    rows = []
    recent = snapshots[-25:]
    for old, new in zip(recent, recent[1:]):
        elapsed = new['source_at']-old['source_at']
        a = next((r for r in old['accounts'] if r['pseudonym']==name), None)
        b = next((r for r in new['accounts'] if r['pseudonym']==name), None)
        if not a or not b or not 2400 <= elapsed <= 4800 or b['jobs'] < a['jobs']:
            continue
        observed = 0
        for key in keys:
            before, after = old['machines'].get(key), new['machines'].get(key)
            if not before or not after or after['jobs'] < before['jobs'] or after['issues'] != before['issues'] or not .9*elapsed/60 <= after['minutes']-before['minutes'] <= elapsed/60+4:
                break
            observed += after['jobs']-before['jobs']
        else:
            rows.append(dict(at=new['source_at'],paid_jobs=b['jobs']-a['jobs'],observed_requests=observed))
    total = sum(r['paid_jobs'] for r in rows)
    return dict(intervals=len(rows),paid_jobs=total,observed_requests=sum(r['observed_requests'] for r in rows),
        relative_error=sum(abs(r['paid_jobs']-r['observed_requests']) for r in rows)/total if total else None,
        status='collecting' if len(rows)<MIN_INTERVALS else 'measured', rows=rows)


class Leaderboard:
    def __init__(self, path):
        self.path = str(path)
        self.error = None
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS leaderboard_meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leaderboard_hourly (hour INTEGER PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leaderboard_machine_totals (fingerprint TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS leaderboard_bucket_baselines (fingerprint TEXT,hour INTEGER,payload TEXT NOT NULL,PRIMARY KEY(fingerprint,hour));
            ''')

    def observe(self, at):
        """Fold existing validated hourly aggregates into monotonic totals.

        Only two hours of reconciliation baselines and one latest total per SE
        key are retained. No raw polls, account IDs or private fleet links.
        """
        cutoff = at // 3600 * 3600 - 3600
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            previous_at = db.execute("SELECT value FROM leaderboard_meta WHERE key='observed_at'").fetchone()
            if previous_at and at <= int(previous_at[0]):
                return
            buckets = {}
            for key, hour, jobs, output, partial in db.execute('''SELECT k.fingerprint,u.hour,SUM(COALESCE(u.requests,0)),SUM(u.output),MAX(u.requests_partial)
                FROM public_provider_usage u JOIN public_provider_keys k ON k.provider=u.provider
                WHERE u.hour>=? GROUP BY k.fingerprint,u.hour''', (cutoff,)):
                buckets[(key, hour)] = dict(jobs=jobs, output=output, minutes=0, issues=partial)
            masks = {}
            for key, hour, minutes, resets, gaps in db.execute('''SELECT k.fingerprint,q.hour,q.minutes,q.resets,q.gaps
                FROM public_provider_quality q JOIN public_provider_keys k ON k.provider=q.provider WHERE q.hour>=?''', (cutoff,)):
                bucket = buckets.setdefault((key, hour), dict(jobs=0, output=0, minutes=0, issues=0))
                masks[(key, hour)] = masks.get((key, hour), 0) | minutes
                bucket['issues'] += resets + gaps
            for pair, mask in masks.items():
                buckets[pair]['minutes'] = mask.bit_count()
            totals = {k: json.loads(v) for k, v in db.execute('SELECT fingerprint,payload FROM leaderboard_machine_totals')}
            baselines = {(k, h): json.loads(v) for k, h, v in db.execute('SELECT fingerprint,hour,payload FROM leaderboard_bucket_baselines')}
            for (key, hour), bucket in buckets.items():
                old = baselines.get((key, hour), dict(jobs=0, output=0, minutes=0, issues=0))
                total = totals.setdefault(key, dict(jobs=0, output=0, minutes=0, issues=0))
                for field in ('jobs', 'output', 'minutes', 'issues'):
                    change = bucket[field] - old[field]
                    if change < 0:
                        total['issues'] += 1
                    else:
                        total[field] += change
                total['seen_at'] = at
                db.execute('INSERT OR REPLACE INTO leaderboard_bucket_baselines VALUES(?,?,?)', (key, hour, json.dumps(bucket)))
            db.executemany('INSERT OR REPLACE INTO leaderboard_machine_totals VALUES(?,?)', ((k, json.dumps(v)) for k, v in totals.items()))
            db.execute('DELETE FROM leaderboard_bucket_baselines WHERE hour<?', (cutoff,))
            db.execute('DELETE FROM leaderboard_hourly WHERE hour<?',(at-RETENTION,))
            db.execute("INSERT OR REPLACE INTO leaderboard_meta VALUES('observed_at',?)", (str(at),))

    def capture(self, daily, cumulative, sample_at, now):
        daily_rows = entries(daily, '24h', 'earnings')
        all_rows = entries(cumulative, 'all', 'jobs')
        day_at, all_at = timestamp(daily['updated_at']), timestamp(cumulative['updated_at'])
        if not 0 <= now - day_at <= 900 or not 0 <= now - all_at <= 900:
            raise ValueError('Leaderboard source is stale or in the future')
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute("INSERT OR REPLACE INTO leaderboard_meta VALUES('daily',?)", (json.dumps(dict(updated_at=day_at, entries=daily_rows)),))
            # Never label cached leaderboards and fresh machine samples synchronized.
            if abs(sample_at - all_at) <= 120 and 0 <= now - sample_at <= 150:
                hour = all_at // 3600 * 3600
                if not db.execute('SELECT 1 FROM leaderboard_hourly WHERE hour=?', (hour,)).fetchone():
                    machines = {k: json.loads(v) for k, v in db.execute('SELECT fingerprint,payload FROM leaderboard_machine_totals')}
                    metadata = db.execute('''SELECT k.fingerprint,c.chip,h.ram,s.model,s.at FROM public_provider_keys k
                        JOIN public_provider_samples s ON s.provider=k.provider
                        LEFT JOIN public_provider_chips c ON c.provider=k.provider
                        LEFT JOIN public_provider_hardware h ON h.provider=k.provider ORDER BY s.at''')
                    for key, chip, ram, model, seen in metadata:
                        if key in machines:
                            machines[key].update(chip=chip or 'Unknown chip', ram=ram, model=model or '', source_seen_at=seen)
                    machines = {k:v for k,v in machines.items() if 'chip' in v and sample_at-v['source_seen_at'] <= 150}
                    snapshot = dict(source_at=all_at, machine_at=sample_at, accounts=all_rows, machines=machines)
                    db.execute('INSERT INTO leaderboard_hourly VALUES(?,?)', (hour, json.dumps(snapshot)))
            db.execute('DELETE FROM leaderboard_hourly WHERE hour<?', (now-RETENTION,))
        self.error = None

    def capture_daily(self, daily, now):
        rows=entries(daily,'24h','earnings')
        at=timestamp(daily['updated_at'])
        if not 0 <= now-at <= 900:
            raise ValueError('Daily leaderboard source is stale or in the future')
        with closing(sqlite3.connect(self.path,timeout=10)) as db, db:
            db.execute("INSERT OR REPLACE INTO leaderboard_meta VALUES('daily',?)",(json.dumps(dict(updated_at=at,entries=rows)),))

    def read(self, now, validation_keys=None, validation_name=None):
        with closing(sqlite3.connect(self.path)) as db:
            daily = db.execute("SELECT value FROM leaderboard_meta WHERE key='daily'").fetchone()
            snapshots = [json.loads(v) for v, in db.execute('SELECT payload FROM leaderboard_hourly WHERE hour>=? ORDER BY hour',(now-RETENTION,))]
        data = json.loads(daily[0]) if daily else dict(updated_at=None, entries=[])
        matching_stale=not snapshots or now-snapshots[-1]['source_at']>7200
        data.update(warning=self.error, checkpoints=len(snapshots), minimum_intervals=MIN_INTERVALS,
            comparison_checkpoints=min(25,len(snapshots)),
            last_checkpoint=snapshots[-1]['source_at'] if snapshots else None,
            candidates=[] if matching_stale else compare(snapshots),matching_stale=matching_stale,
            method='single_machine_all_time_job_deltas', retention_days=14,
            stale=not data['updated_at'] or now-data['updated_at']>900)
        if validation_keys and validation_name:
            data['validation']=validate_fleet(snapshots,validation_keys,validation_name)
        return data
