from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from core.config import settings
from features.cinematic import pipeline as pipeline_module
from features.cinematic.captions import CaptionScene, Word, build_ass, group_words
from features.cinematic.models import MovieJob
from features.cinematic.pipeline import MoviePipeline, output_filename
from features.cinematic.scriptwriter import ScriptError, fallback_script, script_from_dict
from features.cinematic.tts import estimate_duration, pick_voice


def cinematic_config(tmp_path: Path):
    return dataclasses.replace(
        settings.cinematic,
        workdir=tmp_path / "work",
        music_dir=tmp_path / "music",
        max_scenes=6,
        max_duration_seconds=60,
        keep_jobs=2,
        allow_demo_scripts=True,
    )


def raw_script(scene_count: int = 4) -> dict:
    return {
        "title": "  The Last Signal  ",
        "logline": "A lone rover hears a voice.",
        "hook": "Nobody expected an answer.",
        "music_mood": "emotional synth",
        "cta": "Follow for more.",
        "hashtags": ["#shorts", "Sci Fi", "shorts", ""],
        "scenes": [
            {
                "narration": f"Scene {index} narration with enough words to be valid.",
                "visual_query": f"mars shot {index}",
                "duration": 3 + index,
                "on_screen_text": "Signal found",
            }
            for index in range(scene_count)
        ],
    }


def test_script_from_dict_normalises_payload(tmp_path):
    config = cinematic_config(tmp_path)
    script = script_from_dict(raw_script(), theme="mars", settings=config)
    assert script.title == "The Last Signal"
    assert 2 <= len(script.scenes) <= config.max_scenes
    assert script.scenes[0].index == 1
    assert script.clean_hashtags() == ["#shorts", "#SciFi"]
    assert script.scenes[0].narration.startswith("Nobody expected an answer.")
    assert all(scene.visual_query for scene in script.scenes)
    assert not script.demo


def test_script_from_dict_rejects_bad_payload(tmp_path):
    config = cinematic_config(tmp_path)
    with pytest.raises(ScriptError):
        script_from_dict({"title": "x"}, theme="t", settings=config)
    with pytest.raises(ScriptError):
        script_from_dict({"scenes": [{"narration": "hi"}]}, theme="t", settings=config)


def test_script_trims_scenes_to_duration_budget(tmp_path):
    config = dataclasses.replace(cinematic_config(tmp_path), max_duration_seconds=20)
    payload = raw_script(6)
    for scene in payload["scenes"]:
        scene["narration"] = "word " * 120  # ~8 seconds each
    script = script_from_dict(payload, theme="t", settings=config)
    assert len(script.scenes) >= 2
    assert script.estimated_seconds <= 60 * 3  # sanity: estimator never explodes


def test_fallback_script_is_renderable(tmp_path):
    config = cinematic_config(tmp_path)
    script = fallback_script("A lonely astronaut on Mars finding a glowing flower", config)
    assert script.demo is True
    assert len(script.scenes) >= 2
    assert script.title
    assert all(scene.narration for scene in script.scenes)
    assert all(scene.visual_query for scene in script.scenes)


def test_caption_grouping_and_ass(tmp_path):
    config = dataclasses.replace(cinematic_config(tmp_path), caption_font="Test Font", caption_size=60)
    words = [
        Word(start=0.0, end=0.4, text="hello"),
        Word(start=0.4, end=0.9, text="brave"),
        Word(start=0.9, end=1.4, text="new"),
        Word(start=1.4, end=1.9, text="world"),
        Word(start=3.5, end=4.0, text="again"),
    ]
    scene = CaptionScene(offset=0.0, duration=5.0, text="hello brave new world again", words=words)
    cues = group_words(scene, None)
    assert cues
    assert all(cue.end > cue.start for cue in cues)
    assert sum("hello" in cue.text for cue in cues) == 1

    ass = build_ass([scene], title="My <Reel>", settings=config, total_duration=5.0)
    assert "[Script Info]" in ass
    assert "PlayResX: 1080" in ass
    assert "Style: Caption,Test Font,60" in ass
    assert "My <Reel>" in ass
    assert ass.count("Dialogue:") >= 2


def test_caption_fallback_without_word_timings(tmp_path):
    config = cinematic_config(tmp_path)
    scene = CaptionScene(offset=2.0, duration=4.0, text="one two three four", words=[])
    cues = group_words(scene, None)
    assert cues
    assert cues[0].start >= 2.0
    ass = build_ass([scene], title="", settings=config, total_duration=6.0)
    assert "one" in ass


def test_voice_and_duration_helpers():
    voices = ("en-US-AriaNeural", "bn-BD-NabanitaNeural")
    assert pick_voice("hello world", voices) == "en-US-AriaNeural"
    assert pick_voice("আমার মন খারাপ", voices) == "bn-BD-NabanitaNeural"
    assert pick_voice("hello", ("fr-FR-DeniseNeural",)) == "fr-FR-DeniseNeural"
    assert estimate_duration("one two three") > 0


def test_output_filename_slug(tmp_path):
    config = cinematic_config(tmp_path)
    script = fallback_script("Mars & the Glowing Flower!!", config)
    name = output_filename(script)
    assert name.endswith(".mp4")
    assert " " not in name


# ---------------------------------------------------------------------------
# pipeline orchestration with fake ffmpeg / TTS / stock
# ---------------------------------------------------------------------------
async def test_pipeline_runs_end_to_end_with_fakes(tmp_path, monkeypatch):
    config = cinematic_config(tmp_path)
    pipeline = MoviePipeline(config)
    script = fallback_script("A lonely astronaut on Mars", config)

    async def fake_script(theme, *, settings, gemini=None, temperature=0.95):
        return script

    async def fake_synthesize(text, out_path, *, settings, voice=None):
        from features.cinematic.tts import SpeechResult

        Path(out_path).write_bytes(b"fake-audio")
        return SpeechResult(path=Path(out_path), duration=1.5, words=[], voice="test", engine="test")

    async def fake_locate(session, query, destination_dir, index):
        return None, ["fake: no footage"]

    def fake_preflight(self):
        from features.cinematic.pipeline import Preflight

        return Preflight(
            ok=True, ffmpeg=True, ffprobe=True, tts="test", pexels=False,
            gemini=False, disk_free_mb=900, problems=[],
        )

    async def fake_visual(*args, **kwargs):
        Path(kwargs["cwd"], kwargs["out_name"]).write_bytes(b"visual")

    async def fake_make_color_visual(duration, out_name, *, cwd, settings, color="x"):
        Path(cwd, out_name).write_bytes(b"visual")

    async def fake_mux(visual_name, audio_name, *, duration, out_name, cwd, settings):
        Path(cwd, out_name).write_bytes(b"clip")

    async def fake_concat(scene_names, *, out_name, cwd, settings):
        assert scene_names
        Path(cwd, out_name).write_bytes(b"timeline")

    async def fake_burn(input_name, *, captions_name="captions.ass", out_name, cwd, settings):
        assert Path(cwd, captions_name).exists()
        Path(cwd, out_name).write_bytes(b"captioned")

    async def fake_ladder(path, *, target_mb, cwd, settings):
        return path, []

    async def fake_thumb(input_name, *, out_name, cwd, settings):
        Path(cwd, out_name).write_bytes(b"thumb")

    async def fake_probe(path, *, cwd, ffprobe_bin="ffprobe"):
        return 12.5

    monkeypatch.setattr(pipeline_module, "write_script", fake_script)
    monkeypatch.setattr(pipeline_module, "synthesize", fake_synthesize)
    monkeypatch.setattr(MoviePipeline, "preflight", fake_preflight)
    monkeypatch.setattr(pipeline.stock, "locate", fake_locate)
    monkeypatch.setattr(pipeline_module.renderer, "make_color_visual", fake_make_color_visual)
    monkeypatch.setattr(pipeline_module.renderer, "make_visual", fake_visual)
    monkeypatch.setattr(pipeline_module.renderer, "mux_narration", fake_mux)
    monkeypatch.setattr(pipeline_module.renderer, "concat_scenes", fake_concat)
    monkeypatch.setattr(pipeline_module.renderer, "burn_captions", fake_burn)
    monkeypatch.setattr(pipeline_module.renderer, "encode_ladder", fake_ladder)
    monkeypatch.setattr(pipeline_module.renderer, "make_thumbnail", fake_thumb)
    monkeypatch.setattr(pipeline_module.renderer, "probe_duration", fake_probe)
    monkeypatch.setattr(pipeline_module.renderer, "pick_music", lambda music_dir: None)

    job = MovieJob(
        id="job-1",
        theme="A lonely astronaut on Mars",
        guild_id=1,
        channel_id=2,
        requester_id=3,
        requester_name="tester",
    )
    updates: list[tuple[float, str]] = []

    async def progress(percent, stage):
        updates.append((percent, stage))

    result = await pipeline.run(job, progress)
    assert result.video.exists()
    assert result.video.read_bytes() == b"captioned"
    assert result.thumbnail is not None and result.thumbnail.exists()
    assert result.duration == 12.5
    assert job.status == "done"
    assert (Path(result.workdir) / "captions.ass").exists()
    captions = (Path(result.workdir) / "captions.ass").read_text(encoding="utf-8")
    assert "Dialogue:" in captions
    assert updates and updates[-1][0] >= 90
    assert any("demo template" in warning for warning in result.warnings)
