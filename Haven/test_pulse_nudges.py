import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from pulse_nudges import build_nudges

TODAY = date(2026, 10, 13)


def prop(**changes):
    values = dict(tags='', bedrooms=2, calendar_rates=[((TODAY + timedelta(days=i)).isoformat(), 100.25) for i in range(1, 122)],
                  min_price=80, max_price=500, adj_occ_60d=0.2, adj_occ_90d=0.2, adj_occ_120d=0.2)
    values.update(changes)
    return SimpleNamespace(**values)


class PulseTests(unittest.TestCase):
    def test_horizon_and_small_bedroom_bands(self):
        for beds in (0, 1, 2):
            rows = build_nudges(prop(bedrooms=beds), [], TODAY)['daily_nudges']
            self.assertEqual(len(rows), 120)
            self.assertEqual(rows[-1]['date'], (TODAY + timedelta(days=120)).isoformat())
            self.assertTrue(all(1 <= r['nudge'] <= 5 and r['proposed_rate'] > r['current_rate'] for r in rows))

    def test_large_bedrooms(self):
        rows = build_nudges(prop(bedrooms=5), [], TODAY)['daily_nudges']
        self.assertTrue(all(5 <= r['nudge'] <= 10 for r in rows))

    def test_fixed_min_tags_override_bedrooms(self):
        for tag in ('fix min', 'FIXED MIN', 'fixed-minimum', 'Owner; Fix Min', 'Min Rate', 'MIN_RATE', 'minimum-rate'):
            result = build_nudges(prop(tags=tag, bedrooms=6), [], TODAY)
            self.assertTrue(result['fixed_minimum'])
            self.assertTrue(all(1 <= r['nudge'] <= 3 for r in result['daily_nudges']))

    def test_weak_signals_never_decrease_including_sunday(self):
        rows = build_nudges(prop(adj_occ_60d=0), [], TODAY)['daily_nudges']
        self.assertTrue(all(r['nudge'] >= 1 for r in rows))
        self.assertTrue(any(r['day'] == 'Sunday' for r in rows))

    def test_demand_proxy_and_weekend_change_magnitude(self):
        rows = build_nudges(prop(adj_occ_60d=0.9), [], TODAY)['daily_nudges']
        self.assertEqual(rows[0]['nudge'], 3)  # Wednesday
        self.assertEqual(rows[2]['nudge'], 5)  # Friday

    def test_booked_nights_exclude_checkout(self):
        res = [dict(arrivalDate='2026-10-14', departureDate='2026-10-16', status='booked')]
        dates = {r['date'] for r in build_nudges(prop(), res, TODAY)['daily_nudges']}
        self.assertNotIn('2026-10-14', dates)
        self.assertNotIn('2026-10-15', dates)
        self.assertIn('2026-10-16', dates)

    def test_cancelled_stay_not_booked(self):
        res = [dict(arrivalDate='2026-10-14', departureDate='2026-10-16', status='cancelled')]
        self.assertEqual(len(build_nudges(prop(), res, TODAY)['daily_nudges']), 120)

    def test_minimum_and_maximum(self):
        self.assertEqual(build_nudges(prop(max_price=100.5), [], TODAY)['daily_nudges'], [])
        rows = build_nudges(prop(min_price=100.25), [], TODAY)['daily_nudges']
        self.assertTrue(all(r['at_minimum'] and r['proposed_rate'] > 100.25 for r in rows))
        self.assertEqual(build_nudges(prop(min_price=200), [], TODAY)['daily_nudges'], [])

    def test_missing_reservations_never_claim_confirmed_gaps(self):
        rows = build_nudges(prop(), None, TODAY)['daily_nudges']
        self.assertTrue(all('confirm this night is open' in r['reason'] for r in rows))

    def test_short_run_missing_availability_no_suggestion(self):
        rates = [('2026-10-14', 100), ('2026-10-15', 101)]
        self.assertEqual(build_nudges(prop(calendar_rates=rates), None, TODAY)['daily_nudges'], [])

    def test_bad_data_and_missing_rates(self):
        rates = [('bad', 100), ('2026-10-14', float('nan')), ('2026-10-15', -1)]
        self.assertFalse(build_nudges(prop(calendar_rates=rates), [], TODAY)['has_rates'])

    def test_same_snapshot_does_not_compound(self):
        self.assertEqual(build_nudges(prop(), [], TODAY), build_nudges(prop(), [], TODAY))


class PushTests(unittest.TestCase):
    def setup_push(self, tags='', bedrooms=2, delta=1):
        from pulse_nudges import push_nudges
        from unittest.mock import Mock
        self.push = push_nudges
        self.day = (TODAY + timedelta(days=1)).isoformat()
        self.action = dict(generated_on=TODAY.isoformat(), listing_id='123', pms_name='test',
                           daily_nudges=[dict(date=self.day, current_rate=100.25, proposed_rate=100.25+delta)])
        self.listing = dict(id='123', pms='test', currency='USD', tags=tags, no_of_bedrooms=bedrooms, min=80, max=200)
        self.rate = dict(date=self.day, price=100.25, unbookable=0, booking_status='')
        self.overrides = []
        self.client = Mock()
        def request(method, path, payload=None, params=None):
            if path == '/listings/123': return {'listings':[self.listing]}
            if path == '/listing_prices': return [dict(id='123', pms='test', data=[self.rate])]
            if path.endswith('/overrides'):
                if method == 'POST': self.overrides = payload['overrides']
                return {'overrides':self.overrides}
            if path == '/refresh_listing': return {'pricing_array':self.overrides}
            raise AssertionError(path)
        self.client.request.side_effect = request
    def writes(self):
        return [c for c in self.client.request.call_args_list if c.args[:2] == ('POST','/listings/123/overrides')]
    def test_exact_decimal_target_and_retry(self):
        self.setup_push()
        result = self.push(self.action,self.client,TODAY)
        self.assertEqual(self.overrides[0]['price'],101.25)
        self.assertTrue(result['post_apply']['verified_sync']['verified'])
        self.rate['price'] = 101.25
        self.assertTrue(self.push(self.action,self.client,TODAY)['idempotent'])
        self.assertEqual(len(self.writes()),1)
    def test_min_rate_live_tag_blocks_large_nudge(self):
        self.setup_push(tags='min rate',bedrooms=6,delta=5)
        with self.assertRaises(ValueError): self.push(self.action,self.client,TODAY)
        self.assertFalse(self.writes())
    def test_min_rate_live_tag_allows_three(self):
        self.setup_push(tags='min rate',bedrooms=6,delta=3)
        self.push(self.action,self.client,TODAY)
        self.assertEqual(self.overrides[0]['price'],103.25)
    def test_stale_rate_booking_block_and_unknown_availability(self):
        for change in ({'price':120}, {'price':99}, {'booking_status':'booked'}, {'unbookable':1}, {'unbookable':None}):
            self.setup_push(); self.rate.update(change)
            with self.assertRaises(ValueError): self.push(self.action,self.client,TODAY)
            self.assertFalse(self.writes())
    def test_dates_limits_and_duplicates(self):
        for mode in ('expired','today','far','duplicate','ceiling','floor','decrease'):
            self.setup_push()
            if mode=='expired': self.action['generated_on']='2020-01-01'
            if mode=='today': self.action['daily_nudges'][0]['date']=TODAY.isoformat()
            if mode=='far': self.action['daily_nudges'][0]['date']=(TODAY+timedelta(days=121)).isoformat()
            if mode=='duplicate': self.action['daily_nudges']*=2
            if mode=='ceiling': self.listing['max']=101
            if mode=='floor': self.listing['min']=110
            if mode=='decrease': self.action['daily_nudges'][0]['proposed_rate']=99
            with self.assertRaises(ValueError): self.push(self.action,self.client,TODAY)
            self.assertFalse(self.writes())
    def test_newer_override_cannot_be_lowered(self):
        self.setup_push(); self.overrides=[dict(date=self.day,price=150,price_type="fixed")]
        with self.assertRaises(ValueError): self.push(self.action,self.client,TODAY)
        self.assertFalse(self.writes())
    def test_preserve_other_override_settings(self):
        self.setup_push(); self.overrides=[dict(date=self.day,min_stay=4,check_in='0000010')]
        self.push(self.action,self.client,TODAY)
        self.assertEqual(self.overrides[0]['min_stay'],4)
        self.assertEqual(self.overrides[0]['check_in'],'0000010')
    def test_missing_confirmation_not_success(self):
        self.setup_push()
        original=self.client.request.side_effect
        def request(method,path,payload=None,params=None):
            if method=='POST' and path.endswith('/overrides'): return {}
            return original(method,path,payload,params)
        self.client.request.side_effect=request
        with self.assertRaises(ValueError): self.push(self.action,self.client,TODAY)
    def test_refresh_failure_reports_saved_without_reposting(self):
        self.setup_push()
        original=self.client.request.side_effect
        def request(method,path,payload=None,params=None):
            if path=='/refresh_listing': raise RuntimeError('rate limit')
            return original(method,path,payload,params)
        self.client.request.side_effect=request
        result=self.push(self.action,self.client,TODAY)
        self.assertFalse(result['post_apply']['verified_sync']['verified'])
        self.assertIn('Overrides saved',result['post_apply']['refresh']['message'])
        self.assertEqual(len(self.writes()),1)




class CalendarLoadTests(unittest.TestCase):
    def test_live_calendar_without_csv_and_blocks(self):
        from pulse_nudges import load_pulse_calendars
        from unittest.mock import Mock
        p=prop(listing_id='123',pms_name='test',calendar_rates=[])
        client=Mock()
        client.request.return_value=[dict(id='123',pms='test',currency='USD',data=[
            dict(date='2026-10-14',price=100,unbookable=0,booking_status=''),
            dict(date='2026-10-15',price=100,unbookable=1,booking_status='')])]
        loaded=load_pulse_calendars([p],client,TODAY)[0]
        rows=build_nudges(loaded,None,TODAY)['daily_nudges']
        self.assertEqual([n['date'] for n in rows],['2026-10-14'])
        self.assertEqual(p.calendar_rates,[])
        self.assertEqual(rows[0]['minimum_price'],80)
    def test_missing_calendar_never_uses_stale_csv(self):
        from pulse_nudges import load_pulse_calendars
        from unittest.mock import Mock
        for response in ([],[dict(id='123',pms='other',currency='USD',data=[])],{}):
            client=Mock();client.request.return_value=response
            loaded=load_pulse_calendars([prop(listing_id='123',pms_name='test')],client,TODAY)[0]
            self.assertTrue(loaded.pulse_data_error)
            self.assertEqual(build_nudges(loaded,None,TODAY)['daily_nudges'],[])

class PulseRouteTests(unittest.TestCase):
    def setUp(self):
        import ast, threading
        from pathlib import Path
        from types import SimpleNamespace
        class Flask:
            def __init__(self, name): pass
            def route(self, *args, **kwargs): return lambda fn: fn
        request=SimpleNamespace(get_json=lambda **kw:self.payload)
        jsonify=lambda **kw:kw
        from datetime import datetime, timezone
        from unittest.mock import Mock
        tree=ast.parse(Path(__file__).with_name('app.py').read_text(encoding='utf-8-sig'))
        node=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='pulse_decision')
        self.app=Flask(__name__)
        self.action=dict(id='test',type='pulse_daily_nudge',generated_on=date.today().isoformat(),daily_nudges=[dict(date='a',decision='approved'),dict(date='b',decision='rejected')])
        self.push=Mock(return_value={})
        scope=dict(app=self.app,request=request,jsonify=jsonify,date=date,datetime=datetime,timezone=timezone,
            _actions_lock=threading.Lock(),_load_actions=lambda:[self.action],_save_actions=lambda a:None,
            _apply_weekly_action=self.push,_finalize_pricelabs_push=lambda a:a,PricingApplyError=ValueError)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'route','exec'),scope)
        def post(path, json):
            self.payload=json
            result=scope['pulse_decision']('test')
            return SimpleNamespace(status_code=result[1] if isinstance(result,tuple) else 200)
        self.client=SimpleNamespace(post=post)
    def test_only_approved_subset_is_pushed(self):
        self.assertEqual(self.client.post('/api/pulse/test',json=dict(dates=['b'],decision='push')).status_code,409)
        self.push.assert_not_called()
        self.assertEqual(self.client.post('/api/pulse/test',json=dict(dates=['a'],decision='push')).status_code,200)
        self.assertEqual(len(self.push.call_args.args[0]['daily_nudges']),1)
        self.assertEqual(self.action['daily_nudges'][1]['decision'],'rejected')
        self.assertEqual(self.client.post('/api/pulse/test',json=dict(dates=['a'],decision='push')).status_code,409)
    def test_failed_push_keeps_approval(self):
        self.push.side_effect=ValueError('stale price')
        self.assertEqual(self.client.post('/api/pulse/test',json=dict(dates=['a'],decision='push')).status_code,400)
        self.assertEqual(self.action['daily_nudges'][0]['decision'],'approved')

if __name__ == "__main__":
    unittest.main()
