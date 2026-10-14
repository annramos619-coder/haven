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
        for tag in ('fix min', 'FIXED MIN', 'fixed-minimum', 'Owner; Fix Min'):
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


if __name__ == '__main__':
    unittest.main()
