import os
import random
import tempfile
import unittest

from pnguinnrip import analyze
from pnguinnrip.discio import open_disc
from tests.disc_builder import FakeCell, FakeTitle, contiguous_cells, write_disc


def load(titles, **kwargs):
    root = tempfile.mkdtemp()
    write_disc(root, titles, **kwargs)
    disc, _label = open_disc(root)
    return disc, analyze.analyze_disc(disc)


class StructureTests(unittest.TestCase):
    def test_simple_disc_picks_longest_title(self):
        disc, reports = load([
            FakeTitle(contiguous_cells(0, 3, count=1)),             # FBI warning
            FakeTitle(contiguous_cells(10_000, 112)),                # the movie
            FakeTitle(contiguous_cells(5_000_000, 48)),              # making-of
        ])
        choice = analyze.choose_title(reports, min_minutes=40)
        self.assertEqual(choice.report.number, 2)
        self.assertTrue(choice.report.clean)

    def test_bonus_feature_with_more_chapters_does_not_win(self):
        # The old ranking put chapter count ahead of runtime.
        disc, reports = load([
            FakeTitle(contiguous_cells(0, 45, count=30)),
            FakeTitle(contiguous_cells(1_000_000, 120, count=28)),
        ])
        self.assertEqual(analyze.choose_title(reports, 40).report.number, 2)

    def test_paramount_style_decoys(self):
        """99 titles of the same length; only one plays its cells in order."""
        rng = random.Random(7)
        movie = contiguous_cells(0, 118, count=30)
        titles = []
        real = 63
        for n in range(1, 100):
            cells = list(movie)
            if n != real:
                rng.shuffle(cells)
            titles.append(FakeTitle(cells))
        disc, reports = load(titles)
        choice = analyze.choose_title(reports, 40)
        self.assertEqual(choice.report.number, real)
        self.assertEqual(choice.reason, "only title with a clean structure")
        decoy = next(r for r in reports if r.number == 1)
        self.assertGreater(decoy.backward_jumps, 0)

    def test_up_in_the_air_layout(self):
        """Modeled on a real Paramount disc (Up in the Air, 99 titles).

        The real movie has a few sub-second cells (all its copies do too);
        four in-order copies claim the same runtime but hold ~55% of the
        data; others are scrambled.
        """
        real = contiguous_cells(0, 109, count=24, mbps=6.2)
        cells, sector = [], 0
        for c in real:
            cells.append(FakeCell(sector, sector + c.last - c.first, c.seconds))
            sector = cells[-1].last + 1
            if len(cells) <= 12 and len(cells) % 2:
                cells.append(FakeCell(sector, sector + 39, 0.5))
                sector += 40
        thin = []
        for c in cells:
            span = max(1, int((c.last - c.first + 1) * 0.55))
            thin.append(FakeCell(c.first, c.first + span - 1, c.seconds))
        rng = random.Random(3)
        scrambled = list(cells)
        rng.shuffle(scrambled)
        titles = [FakeTitle(contiguous_cells(9_000_000, 0.1, count=1))]   # 1: warning
        titles += [FakeTitle(scrambled)] * 4                               # 2-5: decoys
        titles += [FakeTitle(thin)] * 4                                    # 6-9: thin copies
        titles += [FakeTitle(cells)]                                       # 10: the movie
        disc, reports = load(titles)
        by_number = {r.number: r for r in reports}

        self.assertTrue(by_number[10].clean, by_number[10].problems)
        self.assertIn("only 55% of the video data of same-length title 10",
                      "; ".join(by_number[6].problems))
        choice = analyze.choose_title(reports, 40)
        self.assertEqual(choice.report.number, 10)
        self.assertEqual(choice.reason, "only title with a clean structure")
        self.assertEqual(analyze.choose_title(reports, 40, navigator_title=10).warnings, [])

    def test_decoy_claiming_long_runtime_with_no_data(self):
        junk = [FakeCell(500 + i, 500 + i, 300.0) for i in range(30)]  # 2.5 h in 30 sectors
        disc, reports = load([
            FakeTitle(junk),
            FakeTitle(contiguous_cells(10_000, 100)),
        ])
        self.assertIn("too little data", "; ".join(reports[0].problems))
        self.assertEqual(analyze.choose_title(reports, 40).report.number, 2)

    def test_repeated_cells_are_flagged(self):
        cells = contiguous_cells(0, 60, count=10)
        disc, reports = load([FakeTitle(cells + cells)])
        self.assertEqual(reports[0].repeated_cells, 10)
        self.assertFalse(reports[0].clean)

    def test_pgc_length_lie_is_flagged(self):
        disc, reports = load([FakeTitle(contiguous_cells(0, 30), pgc_seconds=7200)])
        self.assertIn("claimed length", "; ".join(reports[0].problems))

    def test_angle_block_counts_one_angle(self):
        cells = contiguous_cells(0, 90, count=3)
        a, b, c = cells
        angle_1 = FakeCell(b.first, b.last, b.seconds, block_mode=1, block_type=1)
        angle_2 = FakeCell(b.last + 1, b.last + (b.last - b.first) + 1, b.seconds, block_mode=3, block_type=1)
        c = FakeCell(angle_2.last + 1, angle_2.last + (c.last - c.first) + 1, c.seconds)
        disc, reports = load([FakeTitle([a, angle_1, angle_2, c], pgc_seconds=90 * 60)])
        self.assertAlmostEqual(reports[0].duration, 90 * 60, delta=1)
        self.assertTrue(reports[0].clean, reports[0].problems)

    def test_falls_back_to_bup_when_ifo_is_missing(self):
        disc, reports = load([FakeTitle(contiguous_cells(0, 95))], write_ifo=False)
        self.assertEqual(len(reports), 1)


class NavigatorPrecedenceTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(1)
        movie = contiguous_cells(0, 110, count=20)
        scrambled = list(movie)
        rng.shuffle(scrambled)
        self.disc, self.reports = load([
            FakeTitle(movie),                                  # 1: clean
            FakeTitle(scrambled),                              # 2: looks like a decoy
            FakeTitle(contiguous_cells(3_000_000, 45)),        # 3: bonus feature
        ])

    def test_menu_choice_wins_even_if_structure_looks_odd(self):
        choice = analyze.choose_title(self.reports, 40, navigator_title=2)
        self.assertEqual(choice.report.number, 2)
        self.assertTrue(choice.warnings)

    def test_menu_choice_rejected_when_it_is_a_bonus_feature(self):
        choice = analyze.choose_title(self.reports, 40, navigator_title=3)
        self.assertEqual(choice.report.number, 1)
        self.assertIn("bonus feature", choice.warnings[0])

    def test_menu_choice_rejected_when_title_does_not_exist(self):
        choice = analyze.choose_title(self.reports, 40, navigator_title=42)
        self.assertEqual(choice.report.number, 1)

    def test_nothing_long_enough(self):
        disc, reports = load([FakeTitle(contiguous_cells(0, 10))])
        self.assertIsNone(analyze.choose_title(reports, 40))


class RealIfoTests(unittest.TestCase):
    """Optional: point PNGUINNRIP_REAL_DISCS at a folder of VIDEO_TS dirs."""

    def test_real_discs_parse(self):
        root = os.environ.get("PNGUINNRIP_REAL_DISCS")
        if not root:
            self.skipTest("PNGUINNRIP_REAL_DISCS not set")
        for name in sorted(os.listdir(root)):
            with self.subTest(disc=name):
                disc, _ = open_disc(os.path.join(root, name))
                reports = analyze.analyze_disc(disc)
                self.assertTrue(reports)
                for r in reports:
                    vts = disc.title_sets[r.entry.vts]
                    pgc_total = sum(p.duration for p in vts.title_pgcs(r.entry.vts_ttn))
                    self.assertAlmostEqual(r.duration, pgc_total, delta=1.0)


if __name__ == "__main__":
    unittest.main()
