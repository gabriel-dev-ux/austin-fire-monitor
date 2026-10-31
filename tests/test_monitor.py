import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import fire_monitor as m

NOW = datetime(2026, 10, 30, 18, 0, tzinfo=timezone.utc)


def fresh_state():
    return {"version": 1, "sources": {}, "seen": {}, "pending": {}}


def event(key="one", title="Austin apartment fire", published=None):
    return {"id": key, "title": title, "published": published or m.iso(NOW),
            "link": "https://www.reddit.com/r/Austin/comments/one/", "publisher": "r/Austin",
            "qualifies": True, "label": "UNVERIFIED REDDIT TIP"}


def official(issue="BOX -Structure Fire", status="ACTIVE", address="100 Example Rd"):
    return {"traffic_report_id": "incident-one", "published_date": m.iso(NOW),
            "issue_reported": issue, "traffic_report_status": status, "agency": "FIRE", "address": address}


def news_xml(title="Austin shooting investigation", source="KXAN", domain="https://www.kxan.com", date="Fri, 30 Oct 2026 18:00:00 GMT"):
    return ('<rss><channel><item><title>'+title+' - '+source+'</title><link>https://news.google.com/rss/articles/one</link>'
            '<pubDate>'+date+'</pubDate><source url="'+domain+'">'+source+'</source></item></channel></rss>').encode()


class ParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = m.load_config()

    def test_serious_official_categories(self):
        for category in ["BOX -Structure Fire", "BOXL- Structure Fire", "BOXHI - HIRISE Structure Fire",
                         "BRUSH - Brush Fire", "BRSHL - Brush Alarm / Light", "HMTF - Hazmat Taskforce",
                         "WRESQT - Water Rescue TF Pri 1", "RESQV - Vehicle Rescue", "ALERT3 - Aircraft Down",
                         "ATTACK - Active Attack", "Traffic Injury Pri 1"]:
            with self.subTest(category=category):
                self.assertTrue(m.parse_fire(json.dumps([official(category)]), self.cfg)[0]["qualifies"])

    def test_routine_calls_are_suppressed(self):
        for category in ["ALARM - Fire Alarm", "ALARMH - Fire Alarm HIRISE", "GRASS - Small Grass Fire",
                         "ODOR - Odor Investigation", "Traffic Injury Pri 2"]:
            self.assertFalse(m.parse_fire(json.dumps([official(category)]), self.cfg)[0]["qualifies"])

    def test_archived_incident_is_ignored(self):
        self.assertEqual([], m.parse_fire(json.dumps([official(status="ARCHIVED")]), self.cfg))

    def test_address_correction_does_not_repeat_alert(self):
        a = m.parse_fire(json.dumps([official(address="100 Example Rd")]), self.cfg)
        b = m.parse_fire(json.dumps([official(address="102 Example Rd")]), self.cfg)
        self.assertEqual(a[0]["id"], b[0]["id"])

    def test_alarm_upgrade_has_a_new_key(self):
        a = m.parse_fire(json.dumps([official("ALARM - Fire Alarm")]), self.cfg)
        b = m.parse_fire(json.dumps([official()]), self.cfg)
        self.assertNotEqual(a[0]["id"], b[0]["id"])

    def test_schema_change_is_visible(self):
        with self.assertRaisesRegex(ValueError, "schema"):
            m.parse_fire('[{"incident": "unexpected"}]', self.cfg)

    def test_fire_query_filters_active_records(self):
        self.assertIn("traffic_report_status%3D%27ACTIVE%27", m.fire_url(self.cfg))

    def test_reddit_tip_label_and_date(self):
        body = ('<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_one</id>'
                '<title>Apartment fire near downtown</title><published>2026-10-30T18:00:00Z</published>'
                '<link href="https://www.reddit.com/r/Austin/comments/one/"/></entry></feed>')
        item = m.parse_reddit(body, self.cfg)[0]
        self.assertTrue(item["qualifies"])
        self.assertEqual("UNVERIFIED REDDIT TIP", item["label"])

    def test_wrong_feed_format_is_an_error(self):
        with self.assertRaises(ValueError):
            m.parse_reddit("<html><h1>Denied</h1></html>", self.cfg)

    def test_verified_news_removes_publisher_suffix(self):
        item = m.parse_news(news_xml(), self.cfg)[0]
        self.assertTrue(item["qualifies"])
        self.assertEqual("Austin shooting investigation", item["title"])

    def test_publisher_name_does_not_establish_locality(self):
        item = m.parse_news(news_xml("Dallas shooting investigation", "Austin News", "https://www.kxan.com"), self.cfg)[0]
        self.assertFalse(item["qualifies"])

    def test_untrusted_news_and_lookalike_domains(self):
        for domain in ["https://unknown.example", "https://kxan.com.evil.example"]:
            self.assertFalse(m.parse_news(news_xml(domain=domain), self.cfg)[0]["qualifies"])

    def test_rejects_fiction_and_retrospectives(self):
        for title in ["Austin shooting movie filming", "Austin house fire anniversary"]:
            self.assertFalse(m.parse_news(news_xml(title), self.cfg)[0]["qualifies"])

    def test_unknown_dates_are_not_replaced_with_now(self):
        state = fresh_state()
        state["sources"]["news"] = {"initialized": True}
        item = m.parse_news(news_xml(date="not a date"), self.cfg)[0]
        m.reconcile(state, {"news": [item]}, {}, self.cfg, NOW)
        self.assertFalse(state["pending"])

    def test_source_outages_are_independent(self):
        def get(url):
            if "reddit" in url:
                raise OSError("temporary outage")
            return b"[]" if "austintexas" in url else b"<rss><channel/></rss>"
        with mock.patch.object(m, "request", side_effect=get):
            results, errors = m.fetch_sources(self.cfg)
        self.assertEqual({"fire", "news"}, set(results))
        self.assertEqual({"reddit"}, set(errors))


class StateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = m.load_config()

    def test_first_poll_seeds_without_backfill(self):
        state = m.reconcile(fresh_state(), {"reddit": [event()]}, {}, self.cfg, NOW)
        self.assertFalse(state["pending"])
        self.assertIn("one", state["seen"])

    def test_new_event_alerts_once(self):
        state = m.reconcile(fresh_state(), {"reddit": [event()]}, {}, self.cfg, NOW)
        m.reconcile(state, {"reddit": [event("two")]}, {}, self.cfg, NOW)
        self.assertEqual({"two"}, set(state["pending"]))
        m.reconcile(state, {"reddit": [event("two")]}, {}, self.cfg, NOW)
        self.assertEqual({"two"}, set(state["pending"]))

    def test_failed_source_recovers_with_silent_seed(self):
        state = m.reconcile(fresh_state(), {}, {"reddit": "offline"}, self.cfg, NOW)
        self.assertNotIn("initialized", state["sources"]["reddit"])
        m.reconcile(state, {"reddit": [event()]}, {}, self.cfg, NOW)
        self.assertFalse(state["pending"])

    def test_stale_and_future_posts_do_not_alert(self):
        state = fresh_state()
        state["sources"]["reddit"] = {"initialized": True}
        items = [event("old", published=m.iso(NOW-timedelta(days=1))), event("future", published=m.iso(NOW+timedelta(days=1)))]
        m.reconcile(state, {"reddit": items}, {}, self.cfg, NOW)
        self.assertFalse(state["pending"])

    def test_reseed_suppresses_an_existing_backlog(self):
        state = fresh_state()
        state["sources"]["reddit"] = {"initialized": True}
        state["pending"]["old"] = {"event":event(), "delivered":[], "queued_at":m.iso(NOW)}
        m.reconcile(state, {"reddit": [event("new")]}, {}, self.cfg, NOW, seed=True)
        self.assertFalse(state["pending"])
        self.assertIn("new", state["seen"])

    def test_partial_delivery_only_retries_missing_recipient(self):
        state = fresh_state()
        state["pending"]["one"] = {"event":event(), "delivered":[], "queued_at":m.iso(NOW)}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"state.json"
            with mock.patch.object(m, "post_slack", side_effect=[None, OSError("down")]):
                self.assertFalse(m.deliver(state, "test-token", ["UONE", "UTWO"], path))
            loaded = m.load_state(path)
            self.assertEqual([m.fingerprint("recipient", "UONE")], loaded["pending"]["one"]["delivered"])
            self.assertNotIn("UONE", path.read_text())
            with mock.patch.object(m, "post_slack") as sender:
                self.assertTrue(m.deliver(loaded, "test-token", ["UONE", "UTWO"], path))
                self.assertEqual("UTWO", sender.call_args.args[2])
                self.assertEqual(1, sender.call_count)
            self.assertFalse(m.load_state(path)["pending"])

    def test_corrupt_state_is_not_silently_reset(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/"state.json"
            path.write_text("broken JSON")
            with self.assertRaises(ValueError):
                m.load_state(path)

    def test_dry_run_never_writes_state_or_sends(self):
        state = fresh_state()
        state["sources"]["reddit"] = {"initialized": True}
        with mock.patch.object(m, "load_state", return_value=state), mock.patch.object(m, "save_state") as save, mock.patch.object(m, "post_slack") as send, mock.patch.object(m, "fetch_sources", return_value=({"reddit": [event(published=m.iso())]}, {})):
            self.assertEqual(0, m.main(["--dry-run"]))
            save.assert_not_called()
            send.assert_not_called()
        self.assertFalse(state["pending"])

    def test_baseline_failure_leaves_original_checkpoint(self):
        with mock.patch.object(m, "load_state", return_value=fresh_state()), mock.patch.object(m, "save_state") as save, mock.patch.object(m, "fetch_sources", return_value=({"fire": []}, {"reddit": "offline"})):
            self.assertEqual(1, m.main(["--seed-only"]))
            save.assert_not_called()


class OperationsTests(unittest.TestCase):
    def test_alert_workflow_requires_activation(self):
        workflow = (m.ROOT/".github/workflows/monitor.yml").read_text()
        self.assertIn("vars.MONITOR_ENABLED == 'true'", workflow)
        self.assertIn("default: check", workflow)
        self.assertNotIn("${{ toJSON(secrets) }}", workflow)
        check_job = workflow.split("  check:", 1)[1].split("  baseline:", 1)[0]
        self.assertNotIn("SLACK_BOT_TOKEN", check_job)


if __name__ == "__main__":
    unittest.main()
