"""Per-stage progress, explicit quantities and exact selected-task privacy."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch
import task_progress
import test_task_progress as legacy
import test_all_task_progress as native


class StageProgressTests(unittest.TestCase):
    def project(self, points, **extra):
        raw = legacy.snapshot()
        raw.update(checkpoints=points, **extra)
        return task_progress.project(raw, legacy.THREAD, now=legacy.NOW)

    def point(self, **extra):
        return dict(id="one", label="Проверить результат", done=False, **extra)

    def test_legacy_checkpoint_shape_and_percent_are_unchanged(self):
        result = self.project(legacy.snapshot()["checkpoints"])
        self.assertEqual(result["percent"], 33)
        self.assertEqual(result["checkpoints"][0], {"id":"step-0","title":"Этап 0","done":True})

    def test_quantity_derives_stage_percentage_and_preserves_real_unit(self):
        value = self.project([self.point(status="in_progress",count_done=12,count_total=40,unit="документов",percent=99)])
        point = value["checkpoints"][0]
        self.assertEqual((point["percent"],point["count_done"],point["count_total"],point["unit"]),(30,12,40,"документов"))
        self.assertEqual(value["percent"],30)
        self.assertFalse(point["done"])

    def test_without_quantity_only_explicit_stage_percent_is_exposed(self):
        point = self.project([self.point(status="waiting",percent=37.5)])["checkpoints"][0]
        self.assertEqual((point["status"],point["percent"]),("waiting",37.5))
        self.assertFalse(any(k in point for k in ("count_done","count_total","unit")))

    def test_overall_is_equal_stage_weight_not_business_count_weight(self):
        value = self.project([self.point(percent=50),dict(id="two",label="Второй",done=False,count_done=1,count_total=1000,unit="строк")])
        self.assertEqual(value["percent"],25)
        self.assertEqual(value["checkpoints"][1]["percent"],0.1)

    def test_done_cannot_be_true_before_a_hundred_or_with_noncompleted_status(self):
        for changes in [dict(percent=99),dict(count_done=1,count_total=2,unit="строк"),dict(percent=100,status="pending")]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.project([dict(id="one",label="Этап",done=True,**changes)])

    def test_completed_status_requires_explicit_done_and_a_hundred(self):
        with self.assertRaises(ValueError):
            self.project([self.point(status="completed",percent=100)])
        result=self.project([dict(id="one",label="Этап",done=True,status="completed",percent=100)])
        self.assertEqual(result["percent"],100)

    def test_bad_partial_boolean_nonfinite_and_oversized_quantities_fail_closed(self):
        cases=[dict(count_done=1),dict(count_done=1,count_total=2),dict(unit="строк"),
               dict(count_done=True,count_total=2,unit="строк"),dict(count_done=1,count_total=0,unit="строк"),
               dict(count_done=-1,count_total=2,unit="строк"),dict(count_done=3,count_total=2,unit="строк"),
               dict(count_done=1,count_total=float("inf"),unit="строк"),dict(count_done=1,count_total=2**53,unit="строк"),
               dict(count_done=1,count_total=2**2048,unit="строк"),
               dict(count_done=1,count_total=2,unit="token=private")]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.project([self.point(**changes)])

    def test_bad_percent_status_and_private_unknown_fields_do_not_escape(self):
        for changes in [dict(percent=True),dict(percent=float("nan")),dict(percent=-1),dict(percent=101),dict(status="invented"),dict(status={"injected":True})]:
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.project([self.point(**changes)])
        point=self.point(status="blocked",percent=0,path="C:/private",command="private")
        self.assertNotIn("private",json.dumps(self.project([point])))

    def test_a_hundred_percent_does_not_automatically_mark_unconfirmed_stage_done(self):
        point=self.project([self.point(percent=100,status="waiting")])["checkpoints"][0]
        self.assertEqual(point["percent"],100)
        self.assertFalse(point["done"])

    def test_final_new_protocol_requires_all_stages_done_and_a_hundred(self):
        finished=legacy.snapshot()["updated_at"]
        for points in [[self.point(percent=25,status="in_progress")],
                       [dict(id="one",label="Первый",done=True,percent=100),dict(id="two",label="Второй",done=False)]]:
            with self.subTest(points=points),self.assertRaises(ValueError):
                self.project(points,finished_at=finished)
        result=self.project([dict(id="one",label="Готовый",done=True,status="completed",percent=100)],finished_at=finished)
        self.assertEqual((result["final"],result["percent"]),(True,100))

    def test_legacy_final_incomplete_is_retained_without_fabricating_a_hundred(self):
        result=self.project([self.point()],finished_at=legacy.snapshot()["updated_at"])
        self.assertEqual((result["final"],result["percent"],result["checkpoints"][0]["done"]),(True,0,False))


class NativeStageTests(unittest.TestCase):
    def setUp(self):
        self.fixture=native.AllTaskProgressTests("runTest")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_native_table_preserves_running_status_without_invented_quantities(self):
        value=self.fixture.snapshot()
        self.assertEqual([p["status"] for p in value["checkpoints"]],["completed","in_progress","pending"])
        self.assertEqual([p["percent"] for p in value["checkpoints"]],[100,0,0])
        self.assertTrue(all("count_total" not in p for p in value["checkpoints"]))

    def test_structured_plan_preserves_explicit_status(self):
        self.fixture.append(self.fixture.path,native.record("response_item",{"type":"function_call","name":"functions.update_plan","arguments":json.dumps({"plan":[{"step":"Проверить","status":"in_progress"}]})}))
        value=self.fixture.snapshot()
        self.assertEqual((value["checkpoints"][0]["status"],value["checkpoints"][0]["percent"]),("in_progress",0))

    def test_selected_snapshot_does_not_enumerate_or_open_another_task(self):
        other=self.fixture.add_source(native.OTHER,[native.started()])
        seen=[]
        original=Path.open
        def tracked(path,*args,**kwargs):
            seen.append(path)
            if path==other: raise AssertionError("other task content must not be opened")
            return original(path,*args,**kwargs)
        service=self.fixture.service()
        with patch.object(Path,"open",tracked):
            self.assertEqual(self.fixture.snapshot(service)["thread_id"],native.ID)
        self.assertNotIn(other,seen)

    def test_projected_legacy_callable_preserves_explicit_stage_fields(self):
        raw=self.fixture.protocol(checkpoints=[dict(id="work",label="Проверить",done=False,status="in_progress",percent=20,count_done=2,count_total=10,unit="строк")])
        projected=task_progress.project(raw,native.ID,now=native.NOW)
        service=self.fixture.service(legacy_provider=lambda _:copy.deepcopy(projected))
        point=self.fixture.snapshot(service)["checkpoints"][0]
        self.assertEqual((point["percent"],point["status"],point["count_done"],point["unit"]),(20,"in_progress",2,"строк"))


if __name__=="__main__": unittest.main(verbosity=2)
