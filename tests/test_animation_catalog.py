"""Catalog invariants that protect random selection and saved question evidence."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from rt_extractor import eval_catalog
from rt_extractor.eval_catalog import animation_question_snapshot, catalog, question_snapshot


class AnimationCatalogTests(unittest.TestCase):
    def test_original_baseline_is_preserved_alongside_one_hundred_animation_tasks(self):
        questions = catalog()
        self.assertEqual(len(questions), 101)
        self.assertEqual(questions[0]["id"], "pelican-bicycle")
        self.assertEqual(questions[0]["prompt"], "Generate an SVG of a pelican riding a bicycle")
        self.assertEqual(questions[0]["rubricVersion"], "1.0")
        self.assertEqual(questions[0]["mode"], "original-baseline")
        pool = animation_question_snapshot()
        self.assertEqual(len(pool), 100)
        self.assertEqual(len({q["id"] for q in pool}), 100)
        self.assertEqual(len({q["title"] for q in pool}), 100)
        self.assertEqual(len({q["prompt"] for q in pool}), 100)
        self.assertEqual(len({q["category"] for q in pool}), 10)
        self.assertNotIn("pelican-bicycle", {q["id"] for q in pool})

    def test_animation_contract_has_observable_constraints_and_matching_rubrics(self):
        for question in animation_question_snapshot():
            with self.subTest(question=question["id"]):
                self.assertTrue(question["enabled"])
                self.assertIs(question["animation"], True)
                self.assertEqual(question["outputFormat"], "SVG")
                self.assertEqual(question["sourceKind"], "project-original")
                self.assertFalse(question["official"])
                self.assertIsInstance(question["durationSeconds"], int)
                self.assertGreater(question["durationSeconds"], 0)
                prompt = question["prompt"]
                for requirement in ("场景：", "主运动：", "协同约束：", "无限循环", "SMIL", "不得使用 JavaScript", "自包含的 SVG"):
                    self.assertIn(requirement, prompt)
                rubric = question["scoringRubric"]
                self.assertEqual(question["rubricVersion"], rubric["version"])
                self.assertEqual(rubric["maximum"], 20)
                self.assertEqual(len(rubric["scale"]), 6)
                self.assertEqual([d["id"] for d in rubric["dimensions"]], list("ABCD"))
                self.assertIn("单张截图", rubric["notice"])
                # The motion/rules scored must be the same concrete constraints
                # given to the model, rather than generic pelican criteria.
                motion = prompt.split("2. 主运动：", 1)[1].split("。\n", 1)[0]
                coordination = prompt.split("3. 协同约束：", 1)[1].split("。\n", 1)[0]
                self.assertIn(motion, rubric["dimensions"][1]["criteria"])
                self.assertIn(coordination, rubric["dimensions"][2]["criteria"])
                self.assertNotIn("鹈鹕辨识", str(rubric))

    def test_batch_snapshots_do_not_share_mutable_rubrics_or_task_lists(self):
        pool = animation_question_snapshot()
        first_id = pool[0]["id"]
        original = deepcopy(pool[0])
        pool[0]["prompt"] = "changed after a batch was created"
        pool[0]["scoringRubric"]["dimensions"][0]["criteria"] = "changed"
        pool[0]["abilityTags"].append("changed")
        pool.pop()
        self.assertEqual(animation_question_snapshot()[0], original)
        self.assertEqual(question_snapshot([first_id])[0], original)
        self.assertEqual(len(animation_question_snapshot()), 100)

    def test_disabled_tasks_are_excluded_from_random_and_fixed_selection(self):
        questions = catalog()
        disabled = questions[1]["id"]
        questions[1]["enabled"] = False
        with patch.object(eval_catalog, "QUESTIONS", questions):
            self.assertEqual(len(animation_question_snapshot()), 99)
            self.assertNotIn(disabled, {q["id"] for q in animation_question_snapshot()})
            with self.assertRaises(ValueError):
                question_snapshot([disabled])

    def test_fixed_selection_supports_animation_and_retains_thirty_question_limit(self):
        ids = [q["id"] for q in animation_question_snapshot()]
        self.assertEqual(len(question_snapshot(ids[:30])), 30)
        self.assertEqual([q["id"] for q in question_snapshot([ids[4], "pelican-bicycle", ids[4]])],
                         [ids[4], "pelican-bicycle"])
        for invalid in (ids[:31], [], ["does-not-exist"], [{}], [None], "pelican-bicycle"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                question_snapshot(invalid)


if __name__ == "__main__":
    unittest.main()
