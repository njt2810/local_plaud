import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


# Keep these unit tests runnable in a bare Python environment. The tested helper
# functions do not use Rich, but localplaud imports it for the interactive UI.
try:
    import rich  # noqa: F401
except ImportError:
    rich_module = types.ModuleType("rich")
    rich_module.box = types.SimpleNamespace()
    sys.modules["rich"] = rich_module
    for module_name, names in {
        "rich.console": ["Console"],
        "rich.panel": ["Panel"],
        "rich.table": ["Table"],
        "rich.progress": ["Progress", "SpinnerColumn", "BarColumn", "TextColumn", "TimeElapsedColumn"],
        "rich.text": ["Text"],
    }.items():
        module = types.ModuleType(module_name)
        for name in names:
            setattr(module, name, type(name, (), {"__init__": lambda self, *args, **kwargs: None}))
        sys.modules[module_name] = module


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("localplaud", ROOT / "localplaud.py")
localplaud = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(localplaud)


def sample_state():
    return {
        "audio_filename": "weekly-sync.wav",
        "meeting_date": "2026-09-15",
        "audio_duration": 75,
        "user_context": "Weekly delivery check-in.",
        "notes_instructions": "Prioritise confirmed owners.",
        "has_speakers": True,
        "transcript": [
            {
                "start": 2,
                "end": 8,
                "speaker_id": "SPEAKER_00",
                "speaker": "Alice",
                "text": "The intake module is ready.",
            },
            {
                "start": 65,
                "end": 72,
                "speaker_id": "SPEAKER_01",
                "speaker": "Bob",
                "text": "I will review it tomorrow.",
            },
        ],
    }


class ReviewWorkflowTests(unittest.TestCase):
    def test_review_round_trip_and_global_speaker_swap(self):
        review = localplaud.generate_review_markdown(sample_state())
        review = review.replace("- SPEAKER_00: Alice", "- SPEAKER_00: Bob")
        review = review.replace("- SPEAKER_01: Bob", "- SPEAKER_01: Alice")

        with patch.object(Path, "read_text", return_value=review):
            parsed = localplaud.parse_review_markdown(Path("review.md"))

        self.assertEqual(parsed["transcript"][0]["speaker"], "Bob")
        self.assertEqual(parsed["transcript"][1]["speaker"], "Alice")
        self.assertEqual(parsed["notes_instructions"], "Prioritise confirmed owners.")
        self.assertEqual(parsed["speaker_count"], 2)

    def test_transcript_has_periodic_timestamps_and_full_speakers(self):
        transcript = localplaud.generate_transcript_markdown(sample_state())
        self.assertIn("# weekly-sync - Transcript", transcript)
        self.assertIn("## 00:00:02", transcript)
        self.assertIn("## 00:01:05", transcript)
        self.assertIn("Alice: The intake module is ready.", transcript)
        self.assertIn("Transcription ended after 00:01:15", transcript)

    def test_prompt_uses_gemini_style_sections(self):
        prompt = localplaud.build_prompt(
            "Alice [00:00]:\n  We agreed to proceed.", True,
            "Planning call", "Focus on ownership.",
        )
        for heading in ["## Summary", "## Decisions", "## Next steps", "## Details"]:
            self.assertIn(heading, prompt)
        self.assertNotIn("## Follow-Up Email Draft", prompt)
        self.assertNotIn("## Key Quotes & Insights", prompt)

    def test_more_samples_are_non_overlapping(self):
        labelled = [
            {"start": i, "end": i + 1, "speaker": "SPEAKER_00" if i in (3, 20) else "SPEAKER_01",
             "text": ("recognisable sentence " * 3) if i in (3, 20) else "reply"}
            for i in range(25)
        ]
        samples = localplaud._find_contextual_excerpts(labelled, "SPEAKER_00", window=6)
        self.assertEqual(len(samples), 2)
        first_times = {seg["start"] for seg in samples[0]}
        second_times = {seg["start"] for seg in samples[1]}
        self.assertTrue(first_times.isdisjoint(second_times))

    def test_pipeline_pauses_before_claude(self):
        state = sample_state()
        state.update({
            "current_step": 5,
            "speaker_count": 2,
            "notes": None,
            "topic": None,
            "token_in": 0,
            "token_out": 0,
            "cost": 0.0,
            "continuation": None,
            "awaiting_review": False,
            "review_path": None,
            "md_path": None,
            "pdf_path": None,
            "transcript_path": None,
        })
        with (
            patch.object(localplaud, "unique_path", return_value=Path("review.md")),
            patch.object(Path, "write_text"),
            patch.object(localplaud, "save_state"),
            patch.object(localplaud, "summarise") as summarise,
            patch.object(localplaud, "print_section"),
            patch.object(localplaud, "print_ok"),
            patch.object(localplaud, "print_info"),
        ):
            result = localplaud.process_file(Path("weekly-sync.wav"), state)

        self.assertEqual(result, "awaiting_review")
        self.assertTrue(state["awaiting_review"])
        self.assertEqual(state["current_step"], 6)
        summarise.assert_not_called()


if __name__ == "__main__":
    unittest.main()
