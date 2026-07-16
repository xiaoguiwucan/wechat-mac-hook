import tempfile
import time
import unittest
from pathlib import Path

from memory_store import MemoryStore


class PersonaSystemTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = MemoryStore(Path(self.tmp.name) / "memory.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def add_messages(self, group_id, user_id, count, start=1_700_000_000):
        for index in range(count):
            text = "我喜欢摄影，值班结束一起吃火锅" if index % 3 else "这个值班梗真的太好笑了！"
            self.store.add_message({
                "event_id": f"{group_id}:{user_id}:{start}:{index}", "group_id": group_id,
                "group_name": group_id, "user_id": user_id, "sender_name": f"成员-{group_id}",
                "message_id": str(index), "event_time": start + index * 60, "direction": "incoming",
                "text": text, "raw_message": text, "segments": [{"type": "text", "data": {"text": text}}],
            })

    def run_job(self, job_id):
        seen = []
        for _ in range(20):
            job = self.store.process_persona_job_batch(job_id, 100)
            seen.append(job["processed_messages"])
            if job["status"] == "completed":
                return job, seen
        self.fail("persona job did not complete")

    def test_full_history_batches_evidence_and_group_isolation(self):
        self.add_messages("group-a", "same-wxid", 235)
        self.add_messages("group-b", "same-wxid", 17)
        queued = self.store.queue_persona_analysis("group-a", "same-wxid", "full")
        job, seen = self.run_job(queued["jobs"][0])
        self.assertEqual([100, 200, 235], seen)
        self.assertEqual(235, job["total_messages"])
        detail = self.store.persona_detail("group-a", "same-wxid")
        self.assertEqual(235, detail["metrics"]["message_count"])
        self.assertTrue(detail["claims"])
        for claim in detail["claims"]:
            if claim["source"] == "auto":
                self.assertTrue(claim["evidence"])
                self.assertTrue(claim["evidence"][0]["event_id"].startswith("group-a:"))
        self.assertEqual({}, self.store.persona_detail("missing", "same-wxid"))
        self.assertEqual(17, self.store.persona_metrics("group-b", "same-wxid")["message_count"])

    def test_manual_overrides_survive_reanalysis_and_pause_resume(self):
        self.add_messages("group-a", "member-1", 130)
        self.store.save_persona("member-1", "group-a", "人工摘要", ["核心成员"], ["永久人工事实"])
        queued = self.store.queue_persona_analysis("group-a", "member-1", "full")
        job_id = queued["jobs"][0]
        self.store.set_persona_job_status(job_id, "paused")
        paused = next(x for x in self.store.persona_jobs("group-a") if x["id"] == job_id)
        self.assertEqual("paused", paused["status"])
        self.store.set_persona_job_status(job_id, "queued")
        self.run_job(job_id)
        detail = self.store.persona_detail("group-a", "member-1")
        self.assertEqual("人工摘要", detail["profile"]["summary"])
        self.assertIn("核心成员", detail["profile"]["tags"])
        self.assertIn("永久人工事实", detail["profile"]["facts"])
        manual = [x for x in detail["claims"] if x["source"] == "manual"]
        self.assertEqual(["永久人工事实"], [x["value"] for x in manual])

    def test_incremental_job_starts_at_previous_cursor_and_model_evidence_is_validated(self):
        self.add_messages("group-a", "member-2", 105)
        first = self.store.queue_persona_analysis("group-a", "member-2", "full")
        self.run_job(first["jobs"][0])
        self.add_messages("group-a", "member-2", 31, start=1_710_000_000)
        due = self.store.queue_due_persona_analysis("2999-01-01 00:00:00")
        self.assertEqual(1, due["queued"])
        job = next(x for x in self.store.persona_jobs("group-a") if x["id"] == due["jobs"][0])
        self.assertEqual(105, job["cursor_offset"])
        payload = self.store.persona_job_batch_payload(job["id"])
        inserted = self.store.add_persona_model_claims("group-a", "member-2", [
            {"category": "interest", "value": "有证据的摄影兴趣", "confidence": .91,
             "evidence_ids": [payload["messages"][0]["event_id"]]},
            {"category": "fact", "value": "无证据的虚构事实", "confidence": .99,
             "evidence_ids": ["missing-message"]},
        ], payload["messages"])
        self.assertEqual(1, inserted)
        self.run_job(job["id"])
        detail = self.store.persona_detail("group-a", "member-2")
        values = [x["value"] for x in detail["claims"]]
        self.assertIn("有证据的摄影兴趣", values)
        self.assertNotIn("无证据的虚构事实", values)
        self.assertEqual(136, detail["metrics"]["message_count"])

    def test_new_member_is_auto_queued_and_real_name_survives_raw_wxid(self):
        self.store.add_message({
            "event_id": "auto-1", "group_id": "group-auto", "user_id": "wxid_real_1",
            "sender_name": "真实群昵称", "direction": "incoming", "text": "今天值班",
            "raw_message": "今天值班", "event_time": 1_700_000_001,
        })
        self.store.add_message({
            "event_id": "auto-2", "group_id": "group-auto", "user_id": "wxid_real_1",
            "sender_name": "wxid_real_1", "nickname": "wxid_real_1", "direction": "incoming",
            "text": "还在", "raw_message": "还在", "event_time": 1_700_000_002,
        })
        listed = self.store.persona_list("group-auto")
        self.assertEqual(1, listed["total"])
        self.assertEqual("真实群昵称", listed["items"][0]["display_name"])
        due = self.store.queue_due_persona_analysis("2000-01-01 00:00:00")
        self.assertEqual(1, due["queued"])
        job = next(x for x in self.store.persona_jobs("group-auto") if x["id"] == due["jobs"][0])
        self.assertEqual("full", job["mode"])

    def test_xml_payload_does_not_pollute_profile_topics(self):
        xml = '<?xml version="1.0"?><msg><appmsg><title>魔法</title><type>57</type><appattach><totallen>10</totallen></appattach></appmsg></msg>'
        self.store.add_message({
            "event_id": "xml-1", "group_id": "group-xml", "user_id": "member-xml",
            "sender_name": "分享者", "direction": "incoming", "text": xml, "raw_message": xml,
            "event_time": 1_700_000_003,
        })
        queued = self.store.queue_persona_analysis("group-xml", "member-xml", "full")
        payload = self.store.persona_job_batch_payload(queued["jobs"][0])
        self.assertEqual("魔法", payload["messages"][0]["text"])
        self.run_job(queued["jobs"][0])
        detail = self.store.persona_detail("group-xml", "member-xml")
        values = {x["value"] for x in detail["claims"]}
        self.assertFalse(values.intersection({"gt", "lt", "msgsource", "type", "version"}))


if __name__ == "__main__":
    unittest.main()
