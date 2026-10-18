import json
import tempfile
import unittest

from pnguinnrip import analyze, ffmpeg
from pnguinnrip.cli import pretty_name
from pnguinnrip.discio import open_disc
from tests.disc_builder import FakeAudio, FakeSub, FakeTitle, FakeVts, contiguous_cells, write_disc


def tracks_for(audio, subs=()):
    root = tempfile.mkdtemp()
    write_disc(root, [FakeTitle(contiguous_cells(0, 100))],
               title_sets={1: FakeVts(audio=list(audio), subs=list(subs))})
    disc, _ = open_disc(root)
    entry = disc.titles[0]
    return (analyze.title_audio_tracks(disc, entry), analyze.title_subtitle_tracks(disc, entry))


class AudioChoiceTests(unittest.TestCase):
    def test_hobbit_case_prefers_english_over_italian_listed_first(self):
        audio, _ = tracks_for([
            FakeAudio("it", coding=6, channels=6),   # Italian DTS 5.1 listed first
            FakeAudio("en", coding=0, channels=6),
            FakeAudio("en", coding=0, channels=2, code_extension=3),
        ])
        choice = analyze.choose_audio(audio, "en")
        self.assertEqual(choice.attr.language, "en")
        self.assertEqual(choice.attr.channels, 6)
        self.assertEqual(choice.stream_id, 0x81)

    def test_unflagged_commentary_loses_to_main_track(self):
        audio, _ = tracks_for([
            FakeAudio("en", channels=6, code_extension=0),
            FakeAudio("en", channels=2, code_extension=0),
        ])
        self.assertEqual(analyze.choose_audio(audio, "en").attr.index, 0)

    def test_same_channels_prefers_earlier_track(self):
        audio, _ = tracks_for([FakeAudio("en", channels=2), FakeAudio("en", channels=2)])
        self.assertEqual(analyze.choose_audio(audio, "en").attr.index, 0)

    def test_flagged_commentary_never_chosen_when_main_exists(self):
        audio, _ = tracks_for([
            FakeAudio("en", channels=6, code_extension=3),
            FakeAudio("fr", channels=6),
        ])
        # No main English track: fall back to the disc default, not the commentary.
        self.assertEqual(analyze.choose_audio(audio, "en").attr.language, "fr")

    def test_dts_and_lpcm_stream_ids(self):
        audio, _ = tracks_for([FakeAudio("en", coding=6), FakeAudio("en", coding=4, channels=2)])
        self.assertEqual([t.stream_id for t in audio], [0x88, 0xA1])

    def test_subtitles_in_language(self):
        _, subs = tracks_for([FakeAudio()], [FakeSub("en"), FakeSub("es"), FakeSub("en", 9),
                                             FakeSub("en", 13)])
        chosen = analyze.choose_subtitles(subs, "en")
        self.assertEqual([s.attr.index for s in chosen], [0, 2])
        self.assertTrue(chosen[1].attr.is_forced)
        self.assertEqual(chosen[1].stream_ids, (0x22,))


PROBE = json.dumps({"streams": [
    {"index": 0, "codec_type": "video", "id": "0x1e0"},
    {"index": 1, "codec_type": "audio", "id": "0x80", "tags": {"language": "ita"}},
    {"index": 2, "codec_type": "audio", "id": "0x81", "tags": {"language": "eng"}},
    {"index": 3, "codec_type": "subtitle", "id": "0x20", "tags": {"language": "eng"}},
    {"index": 4, "codec_type": "subtitle", "id": "0x21", "tags": {"language": "spa"}},
]})


class FfmpegTests(unittest.TestCase):
    def setUp(self):
        self.audio, self.subs = tracks_for([FakeAudio("it"), FakeAudio("en")],
                                           [FakeSub("en"), FakeSub("es")])
        self.streams = ffmpeg.parse_probe(PROBE)

    def test_match_audio_by_stream_id(self):
        english = analyze.choose_audio(self.audio, "en")
        self.assertEqual(ffmpeg.match_audio(self.streams, english, self.audio), 2)

    def test_match_audio_by_position_when_ids_missing(self):
        streams = [ffmpeg.ProbedStream(0, "video", None, ""), ffmpeg.ProbedStream(5, "audio", None, ""),
                   ffmpeg.ProbedStream(6, "audio", None, "")]
        english = analyze.choose_audio(self.audio, "en")
        self.assertEqual(ffmpeg.match_audio(streams, english, self.audio), 6)

    def test_match_subtitles(self):
        chosen = analyze.choose_subtitles(self.subs, "en")
        self.assertEqual(ffmpeg.match_subtitles(self.streams, chosen, "en"), [3])
        no_ids = [ffmpeg.ProbedStream(3, "subtitle", None, "eng"), ffmpeg.ProbedStream(4, "subtitle", None, "spa")]
        self.assertEqual(ffmpeg.match_subtitles(no_ids, chosen, "en"), [3])

    def plan(self, **overrides):
        values = dict(source="/dev/sr0", title=7, output="out.mkv", video_index=0, audio_index=2,
                      audio_channels=6, audio_coding="ac3", audio_language="en",
                      subtitle_indexes=[3], scan_type="telecine", name="Movie")
        values.update(overrides)
        return ffmpeg.RipPlan(**values)

    def test_transcode_command(self):
        cmd = ffmpeg.build_command(self.plan())
        joined = " ".join(cmd)
        self.assertIn("-f dvdvideo -title 7 -i /dev/sr0", joined)
        self.assertEqual(cmd.count("-map"), 4)  # video, aac, surround copy, subtitle
        self.assertIn("fieldmatch,yadif=deint=interlaced,decimate", cmd)
        self.assertIn("aresample=async=1:first_pts=0", cmd)
        self.assertIn("title=Surround 5.1", cmd)
        self.assertIn("language=eng", cmd)
        self.assertEqual(cmd[-1], "out.mkv")

    def test_progressive_has_no_filter(self):
        self.assertNotIn("-vf", ffmpeg.build_command(self.plan(scan_type="progressive")))

    def test_mp4_drops_subtitles_and_dts_copy(self):
        cmd = ffmpeg.build_command(self.plan(container="mp4", audio_coding="dts", output="o.mp4"))
        self.assertEqual(cmd.count("-map"), 2)
        self.assertIn("+faststart", cmd)

    def test_remux_copies_and_converts_lpcm(self):
        cmd = ffmpeg.build_command(self.plan(mode="remux", audio_coding="lpcm", audio_channels=2))
        self.assertIn("copy", cmd)
        self.assertIn("flac", cmd)
        self.assertNotIn("libx264", cmd)
        self.assertNotIn("-fps_mode", cmd)

    def test_classify_idet(self):
        def idet(tff, bff, prog):
            return (f"[Parsed_idet_0 @ 0x1] Multi frame detection: TFF: {tff} BFF: {bff} "
                    f"Progressive: {prog} Undetermined: 10\n")
        self.assertEqual(ffmpeg.classify_idet(idet(5, 0, 1400), "ntsc"), "progressive")
        self.assertEqual(ffmpeg.classify_idet(idet(560, 0, 840), "ntsc"), "telecine")
        self.assertEqual(ffmpeg.classify_idet(idet(1300, 0, 50), "ntsc"), "interlaced")
        self.assertEqual(ffmpeg.classify_idet(idet(560, 0, 840), "pal"), "interlaced")
        self.assertIsNone(ffmpeg.classify_idet("no summary", "ntsc"))

    def test_language_tags(self):
        self.assertEqual(ffmpeg.language_tags("fr"), {"fr", "fre", "fra"})


DEMUXERS = """File formats:
 D. = Demuxing supported
 --
 D  dv              DV (Digital Video)
 D  dvdvideo        DVD-Video
"""
ENCODERS_NO_X264 = """Encoders:
 ------
 V....D mpeg2video           MPEG-2 video
 A....D aac                  AAC (Advanced Audio Coding)
"""
ENCODERS_X264 = ENCODERS_NO_X264 + " V....D libx264              libx264 H.264 / AVC\n"


class ToolCheckTests(unittest.TestCase):
    def test_build_without_x264_is_caught(self):
        # What the first real rip hit: "Unrecognized option 'crf'".
        missing = ffmpeg.missing_features(DEMUXERS, ENCODERS_NO_X264, need_x264=True)
        self.assertEqual(missing, ["the libx264 H.264 encoder"])

    def test_remux_does_not_need_x264(self):
        self.assertEqual(ffmpeg.missing_features(DEMUXERS, ENCODERS_NO_X264, need_x264=False), [])

    def test_missing_dvdvideo(self):
        missing = ffmpeg.missing_features(" D  dv   DV\n", ENCODERS_X264, need_x264=True)
        self.assertEqual(len(missing), 1)
        self.assertIn("dvdvideo", missing[0])

    def test_ffmpeg_dir_override_must_exist(self):
        with self.assertRaises(ffmpeg.FfmpegError):
            ffmpeg.find_tools(tempfile.mkdtemp())

    def test_pick_newest_numbered_build(self):
        from pnguinnrip.get_ffmpeg import pick_asset
        assets = [{"name": n} for n in (
            "ffmpeg-master-latest-linux64-gpl.tar.xz",
            "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz",
            "ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz",
            "ffmpeg-n9.0-latest-linux64-lgpl-9.0.tar.xz",
            "ffmpeg-n9.0-latest-win64-gpl-9.0.zip",
            "ffmpeg-n6.1-latest-linux64-gpl-6.1.tar.xz",
        )]
        self.assertEqual(pick_asset(assets, "linux64")["name"], "ffmpeg-n9.0-latest-linux64-gpl-9.0.tar.xz")
        self.assertEqual(pick_asset(assets, "linux64", "8.1")["name"], "ffmpeg-n8.1-latest-linux64-gpl-8.1.tar.xz")
        self.assertEqual(pick_asset(assets, "win64")["name"], "ffmpeg-n9.0-latest-win64-gpl-9.0.zip")
        self.assertIsNone(pick_asset(assets, "linux64", "6.1"))  # too old for dvdvideo


class MissingAudioTests(unittest.TestCase):
    """The IFO can list an audio track the disc never stores (seen on a real sample)."""

    def setUp(self):
        self.audio, _ = tracks_for([FakeAudio("en", channels=6), FakeAudio("en", channels=2)])
        self.streams = ffmpeg.parse_probe(json.dumps({"streams": [
            {"index": 0, "codec_type": "video", "id": "0x1e0"},
            {"index": 1, "codec_type": "audio", "id": "0x80"},
            {"index": 2, "codec_type": "audio", "id": "0x81"},
        ]}))
        self.chosen = analyze.choose_audio(self.audio, "en")

    def test_keeps_choice_when_it_has_data(self):
        from pnguinnrip.cli import pick_audio_with_data
        track, index = pick_audio_with_data(self.streams, self.audio, self.chosen, {0, 1, 2}, "en")
        self.assertIs(track, self.chosen)
        self.assertEqual(index, 1)

    def test_falls_back_to_a_track_with_data(self):
        from pnguinnrip.cli import pick_audio_with_data
        track, index = pick_audio_with_data(self.streams, self.audio, self.chosen, {0, 2}, "en")
        self.assertEqual(track.attr.index, 1)
        self.assertEqual(index, 2)

    def test_video_only_when_no_audio_has_data(self):
        from pnguinnrip.cli import pick_audio_with_data
        self.assertEqual(pick_audio_with_data(self.streams, self.audio, self.chosen, {0}, "en"), (None, None))

    def test_unknown_data_keeps_choice(self):
        from pnguinnrip.cli import pick_audio_with_data
        track, _ = pick_audio_with_data(self.streams, self.audio, self.chosen, None, "en")
        self.assertIs(track, self.chosen)

    def test_command_without_audio(self):
        plan = ffmpeg.RipPlan(source="/dev/sr0", title=1, output="o.mkv", video_index=0, audio_index=None,
                              audio_channels=0, audio_coding="", audio_language="", scan_type="progressive")
        cmd = ffmpeg.build_command(plan)
        self.assertEqual(cmd.count("-map"), 1)
        self.assertNotIn("aac", cmd)
        self.assertFalse(any(arg.startswith("-disposition:a") for arg in cmd))


class WatchdogTests(unittest.TestCase):
    def test_stalled_ffmpeg_is_stopped(self):
        import sys
        import time
        fake = [sys.executable, "-c",
                "import time; print('out_time_us=1000000', flush=True); time.sleep(60)"]
        start = time.monotonic()
        with self.assertRaises(ffmpeg.FfmpegError) as ctx:
            ffmpeg.run_with_progress(fake, 100, log=lambda m: None, stall_seconds=1)
        self.assertIn("no progress", str(ctx.exception))
        self.assertLess(time.monotonic() - start, 15)

    def test_normal_run_finishes(self):
        import sys
        fake = [sys.executable, "-c",
                "print('out_time_us=50000000'); print('out_time_us=100000000'); print('progress=end')"]
        messages = []
        ffmpeg.run_with_progress(fake, 100, log=messages.append, stall_seconds=5)
        self.assertEqual(messages[-1].strip(), "100%")


class LibraryLogTests(unittest.TestCase):
    def test_banner_is_flattened_and_tagged(self):
        from pnguinnrip.discio import LibraryLog
        log = LibraryLog("libdvdread")
        log._func(None, 2, b"Encrypted DVD support unavailable.\n****\n**  No css library  **\n****", None)
        self.assertEqual(log.messages, ["libdvdread warning: Encrypted DVD support unavailable. No css library"])


class NativeStderrTests(unittest.TestCase):
    def test_capture(self):
        import os
        from pnguinnrip.discio import capture_native_stderr
        with capture_native_stderr() as noise:
            os.write(2, b"libdvdread: Get key for /VIDEO_TS/VTS_01_1.VOB\n")
        self.assertIn("Get key", noise[0])


class NamingTests(unittest.TestCase):
    def test_pretty_name(self):
        self.assertEqual(pretty_name("THE_HOBBIT_DESOLATION"), "The Hobbit Desolation")
        self.assertEqual(pretty_name("Star Trek: Nemesis"), "Star Trek Nemesis")
        self.assertEqual(pretty_name("  "), "DVD")


if __name__ == "__main__":
    unittest.main()
