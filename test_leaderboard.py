import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from leaderboard import Leaderboard, compare, entries, similarity, validate_fleet
from provider_pages import ProviderPages


def fixtures():
    result=[]
    jobs=0
    for i, delta in enumerate([0,100,260,80,400,170,300,120]):
        jobs+=delta
        result.append(dict(source_at=1800000000+i*3600,machine_at=1800000000+i*3600,
            accounts=[dict(pseudonym='known-reference',jobs=10000+jobs)],
            machines={'a':dict(jobs=jobs,minutes=i*60,issues=0,chip='Example chip',ram=64,model='model'),
                      'b':dict(jobs=jobs*2,minutes=i*60,issues=0,chip='Other chip',ram=32,model='other')}))
    return result


class ComparisonTests(unittest.TestCase):
    def test_repeated_varying_aligned_activity_matches_not_one_total(self):
        snaps=fixtures()
        self.assertEqual(compare(snaps[:6]),[])
        match=compare(snaps)[0]
        self.assertEqual(match['public_page'],'/providers/se-a')
        self.assertEqual(match['agreement'],100)
        self.assertFalse(match['ambiguous'])

    def test_flat_or_small_activity_is_not_evidence(self):
        self.assertIsNone(similarity([100]*8,[100]*8))
        self.assertIsNone(similarity([1,2,3,4,5,6],[1,2,3,4,5,6]))

    def test_missing_rank_is_not_zero_and_negative_counts_are_excluded(self):
        snaps=fixtures()[:7]
        snaps[3]['accounts']=[]
        self.assertEqual(compare(snaps),[])
        snaps=fixtures()[:7]
        snaps[3]['accounts'][0]['jobs']=0
        self.assertEqual(compare(snaps),[])

    def test_coverage_reset_and_long_interval_guards(self):
        for kind in ['coverage','reset','gap']:
            snaps=fixtures()[:7]
            if kind=='coverage':snaps[3]['machines']['a']['minutes']=0
            if kind=='reset':snaps[3]['machines']['a']['issues']=1
            if kind=='gap':snaps[3]['source_at']+=5000
            self.assertEqual(compare(snaps),[])

    def test_similar_machines_mark_ambiguous_not_confirmed(self):
        snaps=fixtures()
        for s in snaps:s['machines']['b']=copy.deepcopy(s['machines']['a'])
        self.assertTrue(compare(snaps)[0]['ambiguous'])

    def test_known_multi_machine_fleet_validates_sum_without_training_others(self):
        snaps=fixtures()
        for s in snaps:
            s['accounts'][0]['jobs']=10000+s['machines']['a']['jobs']*3
        self.assertEqual(compare(snaps),[])
        reference=validate_fleet(snaps,['a','b'],'known-reference')
        self.assertEqual(reference['intervals'],7)
        self.assertEqual(reference['relative_error'],0)
        self.assertNotIn('keys',reference)
        self.assertEqual(validate_fleet(snaps[:3],['a','b'],'known-reference')['status'],'collecting')


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/'data.sqlite3'
        self.providers=ProviderPages(self.path,{},True)
        self.board=Leaderboard(self.path)
        self.at=1800000000

    def tearDown(self):
        self.temp.cleanup()

    def feed(self,window,metric,at=None):
        from datetime import datetime,timezone
        return dict(window=window,metric=metric,updated_at=datetime.fromtimestamp(at or self.at,timezone.utc).isoformat(),entries=[dict(rank=1,pseudonym='public-name',jobs=300,tokens=400,earnings_micro_usd=10,work_earnings_micro_usd=10,reward_earnings_micro_usd=0)])

    def capture(self,offset=0):
        self.board.capture(self.feed('24h','earnings'),self.feed('all','jobs'),self.at+offset,self.at+offset)

    def test_checkpoint_dedupe_alignment_staleness_and_daily_preservation(self):
        self.capture(130)
        self.assertEqual(self.board.read(self.at)['checkpoints'],0)
        self.capture()
        self.capture(30)
        self.assertEqual(self.board.read(self.at)['checkpoints'],1)
        self.assertTrue(self.board.read(self.at+901)['stale'])
        with self.assertRaises(ValueError):self.capture(1000)
        self.assertEqual(len(self.board.read(self.at)['entries']),1)

    def test_snapshot_schema_validation(self):
        payload=self.feed('24h','earnings')
        payload['entries'][0]['jobs']=-1
        with self.assertRaises(ValueError):entries(payload,'24h','earnings')
        with self.assertRaises(ValueError):entries(self.feed('24h','earnings'),'all','jobs')

    def test_daily_feed_survives_cumulative_failure_and_history_is_retained(self):
        self.board.capture_daily(self.feed('24h','earnings'),self.at)
        self.board.error='Cumulative unavailable'
        data=self.board.read(self.at)
        self.assertEqual(len(data['entries']),1)
        self.assertEqual(data['checkpoints'],0)
        self.assertTrue(data['matching_stale'])
        self.assertIsNotNone(data['warning'])
        with sqlite3.connect(self.path) as db:
            for s in fixtures():db.execute('INSERT INTO leaderboard_hourly VALUES(?,?)',(s['source_at']//3600*3600,json.dumps(s)))
        self.assertEqual(self.board.read(self.at+15*86400)['checkpoints'],0)
        self.assertEqual(self.board.read(self.at+15*86400)['candidates'],[])

    def test_observe_repeated_hours_do_not_double_count_and_retention_does_not_reset_totals(self):
        hour=self.at//3600*3600
        with sqlite3.connect(self.path) as db:
            db.execute('INSERT INTO public_provider_keys VALUES(?,?,?)',('session','public','a'))
            db.execute('INSERT INTO public_provider_usage VALUES(?,?,?,?,?,?)',('session',hour,'model',100,10,0))
            db.execute('INSERT INTO public_provider_quality(provider,hour,minutes) VALUES(?,?,?)',('session',hour,7))
        self.board.observe(self.at)
        self.board.observe(self.at+60)
        def total():
            with sqlite3.connect(self.path) as db:return json.loads(db.execute('SELECT payload FROM leaderboard_machine_totals').fetchone()[0])
        self.assertEqual(total()['jobs'],10)
        with sqlite3.connect(self.path) as db:db.execute('UPDATE public_provider_usage SET requests=15')
        self.board.observe(self.at+120)
        self.assertEqual(total()['jobs'],15)
        with sqlite3.connect(self.path) as db:db.execute('DELETE FROM public_provider_usage')
        self.board.observe(self.at+7200)
        self.assertEqual(total()['jobs'],15)


if __name__=='__main__':unittest.main()
