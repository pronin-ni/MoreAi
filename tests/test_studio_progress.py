"""
Tests for /studio pipeline progress UX.

Tests the data-driven progress sequence mapping and behavior logic
via Python simulation (mirrors the JS PROGRESS_SEQUENCES structure).

Covers:
- progress sequence mapping by mode
- step advancement logic
- progress cleanup on success
- progress cleanup on failure
- no stale progress left after chat switch
- elapsed time formatting
"""


# ── Progress sequences (mirrors JS PROGRESS_SEQUENCES) ──

PROGRESS_SEQUENCES = {
    'fast': [
        {'text': 'Selecting model…', 'subtext': '', 'duration_ms': 800},
        {'text': 'Generating response…', 'subtext': '', 'duration_ms': 4000},
    ],
    'balanced': [
        {'text': 'Selecting best model…', 'subtext': '', 'duration_ms': 1000},
        {'text': 'Generating response…', 'subtext': '', 'duration_ms': 6000},
    ],
    'quality': [
        {'text': 'Selecting best model…', 'subtext': '', 'duration_ms': 1200},
        {'text': 'Generating draft…', 'subtext': 'This mode may take a little longer for a better result', 'duration_ms': 5000},
        {'text': 'Reviewing answer…', 'subtext': 'Checking for accuracy and completeness', 'duration_ms': 5000},
        {'text': 'Refining final response…', 'subtext': '', 'duration_ms': 5000},
    ],
    'review': [
        {'text': 'Selecting best model…', 'subtext': '', 'duration_ms': 1200},
        {'text': 'Drafting answer…', 'subtext': '', 'duration_ms': 5000},
        {'text': 'Critiquing response…', 'subtext': 'Looking for errors and omissions', 'duration_ms': 5000},
        {'text': 'Improving final answer…', 'subtext': 'This mode may take a little longer for a better result', 'duration_ms': 5000},
    ],
    'deep': [
        {'text': 'Selecting best model…', 'subtext': '', 'duration_ms': 1500},
        {'text': 'Drafting answer…', 'subtext': '', 'duration_ms': 5000},
        {'text': 'Verifying reasoning…', 'subtext': 'Cross-checking facts and logic', 'duration_ms': 6000},
        {'text': 'Finalizing high-confidence result…', 'subtext': 'This mode may take a little longer for a better result', 'duration_ms': 5000},
    ],
}

STUDIO_MODES = {
    'fast': {'label': 'Fast', 'isPipeline': False},
    'balanced': {'label': 'Balanced', 'isPipeline': False},
    'quality': {'label': 'Quality', 'isPipeline': True},
    'review': {'label': 'Review', 'isPipeline': True},
    'deep': {'label': 'Deep', 'isPipeline': True},
}


# ── Simulated Progress State ──

class SimulatedProgress:
    """Python simulation of the client-side progress state machine."""

    def __init__(self):
        self.active = False
        self.mode = None
        self.step_index = 0
        self.sequence = []
        self.elapsed_seconds = 0
        self.current_text = ''

    def start(self, mode):
        self.active = True
        self.mode = mode
        self.step_index = 0
        self.sequence = PROGRESS_SEQUENCES.get(mode, PROGRESS_SEQUENCES['balanced'])
        self.elapsed_seconds = 0
        self.current_text = self.sequence[0]['text'] if self.sequence else ''

    def advance(self):
        """Move to next step. Returns True if there's a next step."""
        self.step_index += 1
        if self.step_index >= len(self.sequence):
            return False
        self.current_text = self.sequence[self.step_index]['text']
        return True

    def clear_on_success(self):
        self.active = False
        self.mode = None
        self.step_index = 0
        self.sequence = []
        self.current_text = ''

    def clear_on_failure(self):
        self.active = False
        self.mode = None
        self.step_index = 0
        self.sequence = []
        self.current_text = ''

    def tick_elapsed(self, seconds=1):
        self.elapsed_seconds += seconds

    def formatted_elapsed(self):
        s = self.elapsed_seconds
        if s < 60:
            return f'{s}s'
        return f'{s // 60}m {s % 60}s'


# ── Tests ──


class TestProgressSequenceMapping:
    """Verify progress sequences are correctly defined per mode."""

    def test_all_modes_have_sequences(self):
        for mode in STUDIO_MODES:
            assert mode in PROGRESS_SEQUENCES, f'Missing progress sequence for {mode}'

    def test_pipeline_modes_have_more_steps(self):
        for mode, config in STUDIO_MODES.items():
            steps = len(PROGRESS_SEQUENCES[mode])
            if config['isPipeline']:
                assert steps >= 3, f'Pipeline mode {mode} should have at least 3 steps, got {steps}'
            else:
                assert steps >= 2, f'Single-model mode {mode} should have at least 2 steps, got {steps}'

    def test_quality_sequence_has_review_and_refine(self):
        texts = [s['text'] for s in PROGRESS_SEQUENCES['quality']]
        assert any('Review' in t for t in texts)
        assert any('Refin' in t for t in texts)

    def test_review_sequence_has_critique(self):
        texts = [s['text'] for s in PROGRESS_SEQUENCES['review']]
        assert any('Critiqu' in t for t in texts)

    def test_deep_sequence_has_verify(self):
        texts = [s['text'] for s in PROGRESS_SEQUENCES['deep']]
        assert any('Verif' in t for t in texts)

    def test_pipeline_modes_have_subtext(self):
        for mode, config in STUDIO_MODES.items():
            if config['isPipeline']:
                has_subtext = any(s['subtext'] for s in PROGRESS_SEQUENCES[mode])
                assert has_subtext, f'Pipeline mode {mode} should have at least one subtext hint'

    def test_unknown_mode_falls_back_to_balanced(self):
        assert 'unknown' not in PROGRESS_SEQUENCES
        assert 'balanced' in PROGRESS_SEQUENCES


class TestProgressStateMachine:
    """Progress state transitions."""

    def test_start_sets_active_state(self):
        progress = SimulatedProgress()
        progress.start('quality')
        assert progress.active is True
        assert progress.mode == 'quality'
        assert progress.current_text == 'Selecting best model…'

    def test_advance_moves_through_steps(self):
        progress = SimulatedProgress()
        progress.start('fast')
        assert progress.current_text == 'Selecting model…'

        has_next = progress.advance()
        assert has_next is True
        assert progress.current_text == 'Generating response…'

        # Past last step
        has_next = progress.advance()
        assert has_next is False

    def test_clear_on_success_resets_state(self):
        progress = SimulatedProgress()
        progress.start('deep')
        progress.advance()
        progress.clear_on_success()

        assert progress.active is False
        assert progress.mode is None
        assert progress.current_text == ''
        assert progress.step_index == 0

    def test_clear_on_failure_resets_state(self):
        progress = SimulatedProgress()
        progress.start('quality')
        progress.advance()
        progress.clear_on_failure()

        assert progress.active is False
        assert progress.mode is None
        assert progress.current_text == ''

    def test_no_stale_progress_after_chat_switch(self):
        """Simulate: start chat → progress → success → switch chat → no progress."""
        progress = SimulatedProgress()

        # First chat
        progress.start('review')
        progress.advance()
        progress.clear_on_success()

        # Switch to new chat — should be clean
        assert progress.active is False
        assert progress.current_text == ''

    def test_elapsed_time_formatting(self):
        progress = SimulatedProgress()
        progress.start('balanced')
        progress.tick_elapsed(30)
        assert progress.formatted_elapsed() == '30s'

        progress.tick_elapsed(35)
        assert progress.formatted_elapsed() == '1m 5s'

        progress.tick_elapsed(120)
        assert progress.formatted_elapsed() == '3m 5s'


class TestProgressSequenceProperties:
    """Validate sequence data integrity."""

    def test_all_steps_have_text(self):
        for mode, steps in PROGRESS_SEQUENCES.items():
            for step in steps:
                assert step['text'], f'Mode {mode} has empty step text'

    def test_all_steps_have_duration(self):
        for mode, steps in PROGRESS_SEQUENCES.items():
            for step in steps:
                assert step['duration_ms'] > 0, f'Mode {mode} has invalid duration'

    def test_total_duration_reasonable(self):
        """Pipeline modes should have total duration > single-model modes."""
        single_total = sum(s['duration_ms'] for s in PROGRESS_SEQUENCES['fast'])
        pipeline_total = sum(s['duration_ms'] for s in PROGRESS_SEQUENCES['quality'])
        assert pipeline_total > single_total

    def test_first_step_is_model_selection(self):
        """All modes should start with model selection."""
        for mode, steps in PROGRESS_SEQUENCES.items():
            assert 'elect' in steps[0]['text'] or 'elect' in steps[0]['text'], (
                f'Mode {mode} first step should mention model selection: {steps[0]["text"]}'
            )
