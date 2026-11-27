import asyncio
import copy
import importlib
import io
import struct
import os
import shutil
import threading
import unittest
import uuid
import warnings
from pathlib import Path
from unittest.mock import patch

from PIL import Image


def preview_module():
    # Missing implementation is an assertion failure during the initial red run.
    try:
        return importlib.import_module("product_workflow.ui_previews")
    except ModuleNotFoundError as exc:
        if exc.name != "product_workflow.ui_previews":
            raise
        raise AssertionError("candidate preview implementation is missing") from exc


def candidates():
    return [
        {"candidate_id": candidate_id, "rank": rank,
         "nodes": [[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]],
         "connections": [[0, 1], [0, 2], [0, 3], [1, 2], [1, 3], [2, 3]],
         "normalized_diameter": diameter}
        for candidate_id, rank, diameter in (("third", 3, .12), ("first", 1, .08), ("second", 2, .1))
    ]


class CandidatePreviewTests(unittest.TestCase):
    def test_valid_geometry_produces_pngs_in_input_order_without_mutation(self):
        source = candidates()
        before = copy.deepcopy(source)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            result = preview_module().render_candidate_previews(source)
        self.assertIsInstance(result, tuple)
        self.assertEqual([item.candidate_id for item in result], ["third", "first", "second"])
        self.assertEqual([item.label for item in result], ["Rank 3", "Rank 1", "Rank 2"])
        self.assertEqual(source, before)
        for item in result:
            self.assertIsNone(item.error)
            self.assertTrue(item.png.startswith(b"\x89PNG\r\n\x1a\n"))
            with Image.open(io.BytesIO(item.png)) as image:
                self.assertEqual(image.size, (512, 512))
                rgb = image.convert("RGB").tobytes()
                pixels = zip(rgb[::3], rgb[1::3], rgb[2::3])
                self.assertGreater(sum(1 for r, g, b in pixels if b > r + 30 and b > g), 100)

    def test_geometry_and_normalized_diameter_change_the_visible_preview(self):
        module = preview_module()
        source = candidates()
        for item in source:
            item["normalized_diameter"] = .08
        same_geometry = module.render_candidate_previews(source)
        source[1]["normalized_diameter"] = .18
        source[2]["connections"] = [[0, 1], [0, 2]]
        changed = module.render_candidate_previews(source)
        self.assertNotEqual(same_geometry[1].png, changed[1].png)
        self.assertNotEqual(same_geometry[2].png, changed[2].png)

    def test_explicit_preview_label_and_missing_rank_preserve_visible_identity(self):
        source = candidates()
        source[0]["preview_label"] = "Chosen rank 3"
        source[1].pop("rank")
        result = preview_module().render_candidate_previews(source)
        self.assertEqual([item.label for item in result], ["Chosen rank 3", "Candidate 2", "Rank 2"])

    def test_invalid_source_rejects_the_batch_before_any_render(self):
        module = preview_module()
        invalid = [
            lambda source: source.pop(),
            lambda source: source[1].pop("nodes"),
            lambda source: source[1].update(nodes=[[0, float("nan"), 0], [1, 0, 0]]),
            lambda source: source[1].update(connections=[[0, 99]]),
            lambda source: source[1].update(connections=[[True, 1]]),
            lambda source: source[1].update(connections=[[0, 0]]),
            lambda source: source[1].update(normalized_diameter=0),
            lambda source: source[1].update(normalized_diameter=float("inf")),
            lambda source: source[1].pop("normalized_diameter"),
            lambda source: source[1].update(candidate_id="third"),
        ]
        for mutate in invalid:
            source = candidates()
            mutate(source)
            with self.subTest(source=source), patch.object(module, "_render_candidate_png") as render:
                with self.assertRaises(module.PreviewSourceError):
                    module.render_candidate_previews(source)
                render.assert_not_called()

    def test_renderer_failure_preserves_all_candidate_slots(self):
        module = preview_module()
        real_render = module._render_candidate_png
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("synthetic rendering failure")
            return real_render(*args, **kwargs)

        with patch.object(module, "_render_candidate_png", side_effect=fail_second):
            result = module.render_candidate_previews(candidates())
        self.assertEqual([item.candidate_id for item in result], ["third", "first", "second"])
        self.assertIsNotNone(result[0].png)
        self.assertEqual(result[1].label, "Rank 1")
        self.assertIsNotNone(result[1].png)
        self.assertTrue(result[1].error)
        self.assertIsNotNone(result[2].png)
        with Image.open(io.BytesIO(result[1].png)) as image:
            self.assertEqual(image.size, (512, 512))
            pixels = image.convert("RGB").tobytes()
            self.assertFalse(any(b > r + 60 and b > g + 20
                                 for r, g, b in zip(pixels[::3], pixels[1::3], pixels[2::3])))

    def test_placeholder_is_valid_png_with_visible_identity_and_failure_text(self):
        module = preview_module()
        self.assertTrue(hasattr(module, "render_candidate_placeholder"))
        png = module.render_candidate_placeholder("candidate-123", "Rank 2")
        self.assertTrue(png.startswith(b"\x89PNG\r\n\x1a\n"))
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.size, (512, 512))
            # Header and central message have visible ink; no geometric substitute.
            for region in ((20, 10, 490, 85), (30, 210, 480, 305)):
                self.assertLess(min(image.crop(region).convert("L").getextrema()), 100)


class SolidResultPreviewTests(unittest.TestCase):
    def setUp(self):
        root = Path(os.environ["LATTICEGPT_TEST_RUNTIME_ROOT"]) / f"stl-{uuid.uuid4().hex[:8]}"
        root.mkdir(parents=True)
        self.addCleanup(shutil.rmtree, root)
        self.path = root / "solid.stl"
        vertices = ((0., 0., 0.), (1., 0., 0.), (0., 1., 0.), (0., 0., 1.))
        faces = ((0, 2, 1), (0, 1, 3), (1, 2, 3), (2, 0, 3))
        data = b"V4 preview test".ljust(80, b" ") + struct.pack("<I", len(faces))
        for face in faces:
            data += struct.pack("<12fH", 0., 0., 0., *(value for i in face for value in vertices[i]), 0)
        self.path.write_bytes(data)

    def test_verified_stl_can_render_a_teal_solid_without_modifying_source(self):
        module = preview_module()
        self.assertTrue(hasattr(module, "render_stl_preview"))
        before = self.path.read_bytes()
        png = module.render_stl_preview(self.path)
        self.assertEqual(self.path.read_bytes(), before)
        with Image.open(io.BytesIO(png)) as picture:
            self.assertEqual(picture.size, (800, 600))
            rgb = picture.convert("RGB").tobytes()
            self.assertGreater(sum(g > r + 20 and b > r + 10 for r, g, b in zip(rgb[::3], rgb[1::3], rgb[2::3])), 10000)
            from PIL import ImageChops
            bounds = ImageChops.difference(picture.convert('RGB'), Image.new('RGB', picture.size, 'white')).getbbox()
            self.assertGreater(max((bounds[2]-bounds[0])/800, (bounds[3]-bounds[1])/600), .70)
            self.assertGreater(min(bounds[0], bounds[1], 800-bounds[2], 600-bounds[3]), 5)

    def test_stl_size_face_limit_and_invalid_geometry_are_rejected(self):
        module = preview_module()
        self.assertTrue(hasattr(module, "render_stl_preview"))
        with patch.object(module, "STL_PREVIEW_MAX_BYTES", 1):
            with self.assertRaises(module.PreviewResourceError):
                module.render_stl_preview(self.path)
        with patch.object(module, "STL_PREVIEW_MAX_TRIANGLES", 3):
            with self.assertRaises(module.PreviewResourceError):
                module.render_stl_preview(self.path)
        self.path.write_bytes(b"solid invalid\nendsolid invalid\n")
        with self.assertRaises(module.PreviewSourceError):
            module.render_stl_preview(self.path)


class PreviewCacheTests(unittest.TestCase):
    def test_lru_recency_and_item_limit(self):
        cache = preview_module().PreviewCache(max_items=2, max_bytes=20)
        cache.put("a", b"a")
        cache.put("b", b"b")
        self.assertEqual(cache.get("a"), b"a")
        cache.put("c", b"c")
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("a"), b"a")
        self.assertEqual(cache.get("c"), b"c")

    def test_total_bytes_oversized_replacement_and_clear(self):
        cache = preview_module().PreviewCache(max_items=5, max_bytes=5)
        cache.put("a", b"123")
        cache.put("b", b"456")
        self.assertIsNone(cache.get("a"))
        cache.put("b", b"123456")
        self.assertIsNone(cache.get("b"))
        cache.put("c", b"12345")
        self.assertEqual(cache.get("c"), b"12345")
        cache.clear()
        self.assertIsNone(cache.get("c"))

    def test_only_bytes_are_cached(self):
        cache = preview_module().PreviewCache()
        with self.assertRaises(TypeError):
            cache.put("a", {"validated": True})
        self.assertIsNone(cache.get("a"))


class PreviewExecutorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = preview_module()
        self.executor = self.module.PreviewExecutor()
        self.release = threading.Event()
        self.started = threading.Event()
        self.calls = []

    def tearDown(self):
        self.release.set()
        self.executor.shutdown()

    def blocking_work(self):
        self.calls.append("first")
        self.started.set()
        if not self.release.wait(3):
            raise AssertionError("test failed to release preview worker")
        return "first-result"

    async def wait_started(self):
        for _ in range(1000):
            if self.started.is_set():
                return
            await asyncio.sleep(.001)
        self.fail("preview worker did not start")

    async def test_same_key_merges_without_blocking_event_loop(self):
        first = asyncio.create_task(self.executor.run("same", self.blocking_work))
        await self.wait_started()
        second = asyncio.create_task(self.executor.run("same", lambda: "wrong"))
        await asyncio.sleep(0)
        self.release.set()
        self.assertEqual(await asyncio.gather(first, second), ["first-result", "first-result"])
        self.assertEqual(self.calls, ["first"])

    async def test_one_running_one_waiting_rejects_third_batch(self):
        first = asyncio.create_task(self.executor.run("one", self.blocking_work))
        await self.wait_started()
        queued_ran = threading.Event()
        second = asyncio.create_task(self.executor.run("two", lambda: queued_ran.set()))
        await asyncio.sleep(0)
        with self.assertRaises(self.module.PreviewBusy):
            await self.executor.run("three", lambda: None)
        self.assertFalse(queued_ran.is_set())
        self.release.set()
        await asyncio.gather(first, second)
        self.assertTrue(queued_ran.is_set())

    async def test_timeout_retains_running_slot_and_same_key_work(self):
        with self.assertRaises(self.module.PreviewTimeout):
            await self.executor.run("one", self.blocking_work, timeout=.02)
        self.assertTrue(self.started.is_set())
        joined = asyncio.create_task(self.executor.run("one", lambda: "wrong"))
        queued = asyncio.create_task(self.executor.run("two", lambda: "two"))
        await asyncio.sleep(0)
        with self.assertRaises(self.module.PreviewBusy):
            await self.executor.run("three", lambda: None)
        self.release.set()
        self.assertEqual(await asyncio.gather(joined, queued), ["first-result", "two"])
        self.assertEqual(self.calls, ["first"])

    async def test_cancelled_running_waiter_does_not_release_slot(self):
        first = asyncio.create_task(self.executor.run("one", self.blocking_work))
        await self.wait_started()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        queued = asyncio.create_task(self.executor.run("two", lambda: "two"))
        await asyncio.sleep(0)
        with self.assertRaises(self.module.PreviewBusy):
            await self.executor.run("three", lambda: None)
        self.release.set()
        self.assertEqual(await queued, "two")

    async def test_cancelled_queued_batch_releases_slot(self):
        first = asyncio.create_task(self.executor.run("one", self.blocking_work))
        await self.wait_started()
        queued_ran = threading.Event()
        queued = asyncio.create_task(self.executor.run("two", queued_ran.set))
        await asyncio.sleep(0)
        queued.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await queued
        replacement = asyncio.create_task(self.executor.run("three", lambda: "three"))
        await asyncio.sleep(0)
        self.release.set()
        self.assertEqual(await replacement, "three")
        await first
        self.assertFalse(queued_ran.is_set())

    async def test_one_cancelled_joiner_does_not_cancel_other_queued_waiter(self):
        first = asyncio.create_task(self.executor.run("one", self.blocking_work))
        await self.wait_started()
        second = asyncio.create_task(self.executor.run("two", lambda: "two"))
        joined = asyncio.create_task(self.executor.run("two", lambda: "wrong"))
        await asyncio.sleep(0)
        second.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await second
        self.release.set()
        self.assertEqual(await joined, "two")
        await first

    async def test_worker_error_is_forwarded_and_releases_capacity(self):
        def fail():
            raise LookupError("synthetic failure")
        with self.assertRaisesRegex(LookupError, "synthetic failure"):
            await self.executor.run("one", fail)
        self.assertEqual(await self.executor.run("one", lambda: "new"), "new")


class PreviewExecutorLoopTests(unittest.TestCase):
    def test_one_executor_can_be_reused_after_loop_close_with_running_work(self):
        module = preview_module()
        executor = module.PreviewExecutor()
        release = threading.Event()
        started = threading.Event()

        def work():
            started.set()
            release.wait(3)
            return "retained"

        async def first_loop():
            with self.assertRaises(module.PreviewTimeout):
                await executor.run("same", work, timeout=.02)

        async def next_loop():
            joined = asyncio.create_task(executor.run("same", lambda: "wrong"))
            await asyncio.sleep(0)
            release.set()
            self.assertEqual(await joined, "retained")
            self.assertEqual(await executor.run("new", lambda: "new"), "new")

        try:
            asyncio.run(first_loop())
            self.assertTrue(started.is_set())
            asyncio.run(next_loop())
        finally:
            release.set()
            executor.shutdown()


if __name__ == "__main__":
    unittest.main()
