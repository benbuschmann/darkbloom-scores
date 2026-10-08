import tempfile
import unittest
from pathlib import Path
from provider_pages import ProviderPages, load_pages
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
        row=self.store.read('fleet',3660)['rows'][0]
        self.assertEqual(row['output'],100)
        self.assertEqual(row['estimated_input'],1000)
        self.assertAlmostEqual(row['estimated_usd'],.00027)
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

    def test_network_minutes_upsert_not_double_counted(self):
        self.capture(3600,1000)
        self.capture(3660,1100)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT SUM(input) FROM public_network_minutes').fetchone()[0],1000)
