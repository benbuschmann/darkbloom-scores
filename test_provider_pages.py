import tempfile
import unittest
import base64
import sqlite3
from pathlib import Path
from provider_pages import ProviderPages, load_pages, base_ceiling
from warm_model_manager import ModelPrice


class ProviderEstimateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = ProviderPages(Path(self.tmp.name)/'test.db', {'fleet':['p']})
        self.prices = {'m':ModelPrice(.05,2.2)}

    def capture(self, at, tokens, model='m'):
        self.store.capture({'providers':[{'id':'p','current_model':model,'tokens_generated':tokens}],
                            'time_series':[{'timestamp':'1970-01-01T01:00:00Z','prompt_tokens':1000,'completion_tokens':100}]},self.prices,at)

    def test_delta_ratio_price_and_anonymity(self):
        self.capture(3600,10000)
        self.assertEqual(self.store.read('fleet',3660)['rows'],[])
        self.capture(3660,10100)
        self.network()
        row=self.store.read('fleet',7200)['rows'][0]
        self.assertEqual(row['output'],100)
        self.assertEqual(row['estimated_input'],1000)
        self.assertAlmostEqual(row['ratio_usd'],.00027)
        self.assertEqual(row['computer'],'Computer A')
        self.assertNotIn('provider',row)

    def test_switch_reset_gap(self):
        self.capture(3600,1000)
        self.capture(3660,1100,'other')
        self.assertIsNone(self.store.read('fleet',3660)['rows'][0]['estimated_usd'])
        self.capture(3720,10)
        self.capture(4000,1000)
        self.assertEqual(sum(x['output'] for x in self.store.read('fleet',4000)['rows']),100)

    def test_hour_boundary_conserves_tokens(self):
        self.capture(3590,1000)
        self.capture(3610,1101)
        rows=self.store.read('fleet',3610)['rows']
        self.assertEqual(sum(x['output'] for x in rows),101)
        self.assertEqual(len(rows),2)

    def test_config_rejects_invalid_slug(self):
        with self.assertRaises(ValueError):load_pages('{"../bad":[]}')

    def test_public_chip_label_and_duplicate_names(self):
        self.store.pages={'fleet':['p','q']}
        self.store.capture({'providers':[{'id':p,'chip':'Apple M1 Max','tokens_generated':0} for p in ['p','q']]}, {}, 3600)
        self.assertEqual(self.store.read('fleet',3600)['computers'],['Apple M1 Max · 1','Apple M1 Max · 2'])

    def network(self, both=True):
        series={'window':'24h','bucket_seconds':1800,'start_at':'1969-12-31T02:00:00Z','end_at':'1970-01-01T02:00:00Z',
                'time_series':[{'timestamp':'1970-01-01T01:00:00Z','prompt_tokens':1000,'completion_tokens':100}]}
        if both:series['time_series'].append({'timestamp':'1970-01-01T01:30:00Z','prompt_tokens':1000,'completion_tokens':100})
        self.store.capture_network(series,{'window':'24h','updated_at':'1970-01-01T02:00:00Z','work_earnings_micro_usd':1000000,'tokens':999999},
                                   {'snapshot_at':'1970-01-01T02:00:00Z','last_24h_completion_tokens':1000},7200)

    def test_network_minutes_are_not_ingested_and_buckets_upsert(self):
        self.capture(3600,1000)
        self.capture(3660,1100)
        self.network();self.network()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM public_network_minutes').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT SUM(input) FROM public_network_buckets').fetchone()[0],2000)
            # Calibration uses completion tokens, not totals.tokens.
            self.assertEqual(db.execute('SELECT usd_per_output FROM public_network_calibration').fetchone()[0],.001)

    def test_ratio_requires_both_closed_buckets(self):
        self.capture(3600,1000);self.capture(3660,1100);self.network(False)
        row=self.store.read('fleet',7200)['rows'][0]
        self.assertIsNone(row['ratio_usd']);self.assertEqual(row['ratio_buckets'],1)

    def test_snapshot_time_duplicates_and_request_deltas(self):
        def sample(at,tokens,jobs):
            self.store.capture({'snapshot_at':at,'providers':[{'id':'p','tokens_generated':tokens,'requests_served':jobs,'current_model':'m'}]},self.prices,9999)
        sample('1970-01-01T01:00:00Z',100,10)
        sample('1970-01-01T01:01:00Z',150,12)
        sample('1970-01-01T01:01:00Z',100000,10000)
        sample('1970-01-01T01:00:00Z',100000,10000)
        row=self.store.read('fleet',4000)['rows'][0]
        self.assertEqual(row['hour'],3600);self.assertEqual(row['output'],50);self.assertEqual(row['requests'],2)
        self.assertEqual(self.store.read('fleet',4000)['coverage'][0]['samples'],2)

    def test_p95_jump_reset_and_gap_quality(self):
        for n in range(7):self.capture(3600+n*60,1000+n*60)
        self.capture(4020,500000)
        self.capture(4080,500060)
        self.capture(4400,500200)
        result=self.store.read('fleet',4500)
        self.assertEqual(sum(r['output'] for r in result['rows']),420)
        q=result['coverage'][0]
        self.assertEqual(q['resets'],1);self.assertEqual(q['gaps'],1);self.assertEqual(q['longest_gap'],320)

    def test_shared_catalog_range_and_real_switch(self):
        prices={'m':ModelPrice(.05,2.2),'n':ModelPrice(.1,4)}
        for at,tokens,model in [(3600,100,'m'),(3660,200,'m'),(3720,300,'n')]:
            self.store.capture({'providers':[{'id':'p','tokens_generated':tokens,'requests_served':tokens//100,'current_model':model,'models':['m','n']}]},prices,at)
        self.network()
        rows=self.store.read('fleet',7200)['rows'];shared=next(r for r in rows if r['shared'])
        self.assertEqual(shared['models'],['m','n']);self.assertAlmostEqual(shared['output_floor_usd'],.00022)
        self.assertAlmostEqual(shared['output_ceiling_usd'],.0004)
        self.assertAlmostEqual(shared['ratio_usd'],.00027);self.assertAlmostEqual(shared['ratio_high_usd'],.0005)
        self.assertTrue(next(r for r in rows if r['unattributed'])['ratio_usd'] is None)

    def test_base_ceiling_calendar_and_availability(self):
        self.assertEqual(base_ceiling(64,54,0),0)
        self.assertAlmostEqual(base_ceiling(64,57,0),18/(31*24)*.5)
        self.assertAlmostEqual(base_ceiling(96,60,0),22/(31*24))
        self.assertEqual(base_ceiling(16,60,0),0)

    def test_base_public_gates_and_reconnect_overlap(self):
        key=base64.b64encode(bytes(64)).decode();self.store.pages={'fleet':['p','q']}
        created=self.store.create_fleet([key]);slug=created['url'].split('/')[-1]
        self.store.pages[slug]=['p','q']
        self.store.capture_keys({'providers':[{'provider_id':p,'se_public_key':key} for p in ['p','q']]})
        for n in range(60):
            self.store.capture({'providers':[{'id':p,'tokens_generated':n,'chip':'Apple M5','memory_gb':64,'os_version':'27.0','status':'online','current_model':'m'} for p in ['p','q']]},self.prices,3600+n*60,
                {'providers':[{'provider_id':p,'app_attest_authorized':True,'status':'online'} for p in ['p','q']]})
        coverage=self.store.read(slug,7200)['coverage']
        self.assertEqual(len(coverage),1);self.assertEqual(coverage[0]['samples'],60);self.assertEqual(coverage[0]['eligible_minutes'],60)
        self.assertAlmostEqual(coverage[0]['base_estimate_usd'],18/(31*24))

    def test_new_session_lifetime_restore_is_excluded(self):
        key=base64.b64encode(bytes(64)).decode();self.store.pages={'fleet':['p','q']}
        self.store.capture_keys({'providers':[{'provider_id':p,'se_public_key':key} for p in ['p','q']]})
        self.capture(3600,10000)
        for at,tokens in [(3660,0),(3720,10000),(3780,10010)]:
            self.store.capture({'providers':[{'id':'q','tokens_generated':tokens,'current_model':'m'}]},self.prices,at)
        page=self.store.read('fleet',4000)
        self.assertEqual(sum(r['output'] for r in page['rows']),10)
        self.assertEqual(sum(q['resets'] for q in page['coverage']),1)

    def test_legacy_migration_preserves_history_and_has_verified_backup(self):
        path=Path(self.tmp.name)/'legacy.db'
        with sqlite3.connect(path) as db:
            db.executescript('CREATE TABLE public_provider_samples(provider TEXT PRIMARY KEY,at INTEGER,model TEXT,tokens INTEGER); CREATE TABLE public_provider_hardware(provider TEXT PRIMARY KEY,ram INTEGER,status TEXT); CREATE TABLE public_provider_usage(provider TEXT,hour INTEGER,model TEXT,output INTEGER,PRIMARY KEY(provider,hour,model)); INSERT INTO public_provider_samples VALUES("p",3660,"m",100); INSERT INTO public_provider_usage VALUES("p",3600,"m",75);')
        store=ProviderPages(path,{'fleet':['p']})
        row=store.read('fleet',4000)['rows'][0]
        self.assertEqual(row['output'],75);self.assertIsNone(row['requests']);self.assertIsNone(row['ratio_usd'])
        self.assertTrue(row['requests_partial'])
        self.assertEqual(store.read('fleet',4000)['coverage'],[])
        backup=path.with_name(path.name+'.before-provider-quality.backup')
        with sqlite3.connect('file:'+str(backup)+'?mode=ro',uri=True) as db:
            self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT output FROM public_provider_usage').fetchone()[0],75)
        ProviderPages(path,{'fleet':['p']})  # migration is idempotent
        for at,tokens,jobs in [(3720,150,10),(3780,200,13)]:
            store.capture({'providers':[{'id':'p','tokens_generated':tokens,'requests_served':jobs,'current_model':'m'}]},self.prices,at)
        row=store.read('fleet',4000)['rows'][0]
        self.assertEqual(row['requests'],3);self.assertTrue(row['requests_partial'])
        self.assertEqual(row['output'],175)

    def test_invalid_or_partial_series_does_not_change_saved_buckets(self):
        self.network()
        invalid={'window':'24h','bucket_seconds':60,'start_at':'1969-12-31T02:00:00Z','end_at':'1970-01-01T02:00:00Z','time_series':[]}
        with self.assertRaises(ValueError):self.store.capture_network(invalid,{}, {},7200)
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM public_network_buckets').fetchone()[0],2)

    def test_gap_quality_spans_hours_and_missing_jobs_stay_unknown(self):
        self.capture(3500,1000);self.capture(7300,3000)
        coverage=self.store.read('fleet',7400)['coverage']
        self.assertEqual([q['hour'] for q in coverage],[0,3600,7200])
        self.assertTrue(all(q['gaps']==1 for q in coverage))
        self.assertEqual(self.store.read('fleet',7400)['rows'],[])

    def test_base_gates_fail_closed(self):
        for n in range(60):
            self.store.capture({'providers':[{'id':'p','tokens_generated':n,'memory_gb':64,'os_version':'26.6','status':'online','current_model':'m'}]},self.prices,3600+n*60,
                               {'providers':[{'provider_id':'p','app_attest_authorized':True,'status':'online'}]})
        q=self.store.read('fleet',7200)['coverage'][0]
        self.assertEqual(q['eligible_minutes'],0);self.assertEqual(q['base_estimate_usd'],0)

    def test_counter_boundary_conserves_requests(self):
        for at,tokens,jobs in [(3590,1000,100),(3610,1101,105)]:
            self.store.capture({'providers':[{'id':'p','tokens_generated':tokens,'requests_served':jobs,'current_model':'m'}]},self.prices,at)
        rows=self.store.read('fleet',3700)['rows']
        self.assertEqual(sum(r['requests'] for r in rows),5)
        self.assertEqual(sum(r['output'] for r in rows),101)

    def test_late_key_mapping_joins_already_saved_usage(self):
        store=ProviderPages(Path(self.tmp.name)/'late.db',{},track_all=True)
        pid='00000000-0000-0000-0000-000000000001';key=base64.b64encode(bytes(64)).decode()
        slug=store.create_fleet([key])['url'].split('/')[-1]
        for at,tokens in [(3600,100),(3660,150)]:
            store.capture({'providers':[{'id':pid,'tokens_generated':tokens,'current_model':'m'}]},self.prices,at)
        self.assertEqual(store.read(slug,4000)['rows'],[])
        store.capture_keys({'providers':[{'provider_id':pid,'se_public_key':key}]})
        self.assertEqual(sum(r['output'] for r in store.read(slug,4000)['rows']),50)

    def test_totals_failure_keeps_valid_ratio_buckets(self):
        series={'window':'24h','bucket_seconds':1800,'start_at':'1969-12-31T02:00:00Z','end_at':'1970-01-01T02:00:00Z',
                'time_series':[{'timestamp':'1970-01-01T01:00:00Z','prompt_tokens':1000,'completion_tokens':100}]}
        warning=self.store.capture_network(series,{}, {},7200)
        self.assertIn('missing public earnings/output totals',warning)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM public_network_buckets').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM public_network_calibration').fetchone()[0],0)

    def test_all_provider_discovery_history_and_private_slug_is_not_listed(self):
        pid='00000000-0000-0000-0000-000000000001'
        store=ProviderPages(Path(self.tmp.name)/'all.db', {'secret-fleet':[pid]},track_all=True)
        row={'id':pid,'chip':'Apple M5 Max','tokens_generated':1000,'current_model':'m','memory_gb':64,'status':'online'}
        store.capture_keys({'providers':[{'provider_id':pid,'se_public_key':base64.b64encode(bytes(64)).decode()}]})
        store.capture({'providers':[row]},self.prices,3600)
        row['tokens_generated']=1100
        store.capture({'providers':[row]},self.prices,3660)
        self.assertEqual(store.ids_for(pid),[pid])
        self.assertEqual(store.read(pid,3660)['rows'][0]['output'],100)
        directory=store.directory('M5')
        self.assertEqual(directory['total'],1)
        self.assertNotIn('secret-fleet',str(directory))
        self.assertEqual(directory['rows'][0]['ram'],64)
        store.capture({'providers':[]},self.prices,3720)
        self.assertEqual(store.ids_for(pid),[pid])
        with self.assertRaises(KeyError):store.ids_for('00000000-0000-0000-0000-000000000002')

    def test_directory_paging_and_literal_search(self):
        store=ProviderPages(Path(self.tmp.name)/'many.db',{},track_all=True)
        providers=[{'id':f'00000000-0000-0000-0000-{n:012d}','chip':'chip','tokens_generated':0} for n in range(105)]
        store.capture_keys({'providers':[{'provider_id':p['id'],'se_public_key':base64.b64encode(bytes([n])*64).decode()} for n,p in enumerate(providers)]})
        store.capture({'providers':providers},{},3600)
        self.assertEqual(store.directory()['total'],105)
        self.assertEqual(len(store.directory()['rows']),100)
        self.assertEqual(len(store.directory(offset=100)['rows']),5)
        self.assertEqual(store.directory('%')['total'],0)

    def test_public_key_combines_reconnected_sessions(self):
        store=ProviderPages(Path(self.tmp.name)/'keys.db',{},track_all=True)
        key=base64.b64encode(bytes(range(64))).decode()
        ids=['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002']
        for n,pid in enumerate(ids):
            store.capture_keys({'providers':[{'provider_id':pid,'se_public_key':key}]})
            for t,tokens in [(3600+n*120,100),(3660+n*120,150)]:
                store.capture({'providers':[{'id':pid,'chip':'Apple M5','tokens_generated':tokens,'current_model':'m'}]},self.prices,t)
        directory=store.directory(key)
        self.assertEqual(directory['total'],1)
        self.assertNotIn('provider_id',directory['rows'][0])
        page=store.read(directory['rows'][0]['page_slug'],3900)
        self.assertEqual(page['computers'],['Apple M5'])
        self.assertEqual(sum(r['output'] for r in page['rows']),100)
        self.assertEqual(store.directory(ids[0])['total'],0)

    def test_missing_counter_creates_no_invented_tokens(self):
        self.capture(3600,1000)
        self.store.capture({'providers':[{'id':'p','chip':'chip'}]},self.prices,3660)
        self.capture(3720,2000)
        self.assertEqual(self.store.read('fleet',3720)['rows'],[])

    def test_created_key_fleet_persists_and_follows_sessions(self):
        path=Path(self.tmp.name)/'created.db'
        store=ProviderPages(path,{},track_all=True)
        key=base64.b64encode(bytes(range(64))).decode()
        created=store.create_fleet([key,key])
        self.assertEqual(created['computers'],1)
        slug=created['url'].split('/')[-1]
        self.assertEqual(store.read(slug,3600)['rows'],[])
        for n,pid in enumerate(['00000000-0000-0000-0000-000000000001','00000000-0000-0000-0000-000000000002']):
            store.capture_keys({'providers':[{'provider_id':pid,'se_public_key':key}]})
            for t,tokens in [(3600+n*120,100),(3660+n*120,150)]:
                store.capture({'providers':[{'id':pid,'chip':'Apple M5','tokens_generated':tokens,'current_model':'m'}]},self.prices,t)
        reopened=ProviderPages(path,{},track_all=True)
        result=reopened.read(slug,3900)
        self.assertEqual(result['computers'],['Apple M5'])
        self.assertEqual(sum(r['output'] for r in result['rows']),100)
        self.assertNotIn(slug,str(store.directory()))
        for bad in [[],[key]*27,['machine-id'],['! '*44],[123]]:
            with self.assertRaises(ValueError):store.create_fleet(bad)
