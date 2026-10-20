import ast
import pytest

from codex_gravity.event_bus import EventBus
from codex_gravity.speculative import (
    AnchorTarget,
    CodexPatch,
    DraftSkeleton,
    SpeculativeDraftingPipeline,
    TokenArbitrageResult,
)


@pytest.mark.asyncio
async def test_speculative_drafting_pipeline_python():
    bus = EventBus()
    pipeline = SpeculativeDraftingPipeline(event_bus=bus)

    # 1. Flash generates draft skeleton
    skeleton = await pipeline.generate_draft(
        task_description="Build user authentication handler",
        file_path="src/auth.py",
        language="python",
    )

    assert skeleton.file_path == "src/auth.py"
    assert len(skeleton.anchors) >= 1
    anchor = skeleton.anchors[0]
    assert anchor.placeholder in skeleton.skeleton_code

    # 2. Codex completes core logic and stitches
    final_code, patches, arbitrage = await pipeline.complete_draft(skeleton)

    assert anchor.placeholder not in final_code
    assert "Codex verified logic implementation" in final_code
    # Must be valid Python syntax
    parsed = ast.parse(final_code)
    assert parsed is not None

    # Check token arbitrage math
    assert arbitrage.raw_context_tokens == skeleton.raw_tokens_estimate
    assert arbitrage.dollars_saved > 0
    assert arbitrage.savings_percentage > 50.0  # Should save >50% compared to pure OpenAI

    # Check that walkie-talkie received dialogues
    history = bus.get_history()
    assert len(history) >= 2


@pytest.mark.asyncio
async def test_code_stitching_indentation_preservation():
    skeleton_code = (
        "class PaymentService:\n"
        "    def process(self, amount: float) -> bool:\n"
        "        # TODO(codex_core_logic): process transaction\n"
        "        return False\n"
    )
    anchor = AnchorTarget(
        anchor_id="anchor-pay",
        target_type="method",
        signature="def process(self, amount: float) -> bool:",
        placeholder="# TODO(codex_core_logic): process transaction",
    )
    skeleton = DraftSkeleton(
        file_path="payment.py",
        language="python",
        skeleton_code=skeleton_code,
        anchors=[anchor],
    )
    patches = [
        CodexPatch(
            anchor_id="anchor-pay",
            replacement_code=(
                "if amount <= 0:\n"
                "    raise ValueError('Invalid amount')\n"
                "return True"
            ),
            codex_tokens_used=50,
        )
    ]

    stitched = SpeculativeDraftingPipeline.stitch_code(skeleton, patches)
    assert "        if amount <= 0:" in stitched
    assert "            raise ValueError('Invalid amount')" in stitched
    assert "        return True" in stitched
    # Ensure valid python
    ast.parse(stitched)


def test_token_arbitrage_calculation():
    # 1,000,000 raw tokens
    # Flash: 1,000,000 tokens ($0.10)
    # Codex: 400 tokens ($0.001)
    # OpenAI pure baseline: 1,000,000 * $2.50 / 1M = $2.50
    result = TokenArbitrageResult.calculate(
        raw_tokens=1_000_000,
        flash_tokens=1_000_000,
        codex_tokens=400,
        generation_time_ms=500.0,
    )
    assert result.baseline_openai_cost == 2.50
    assert result.dollars_saved > 2.30
    assert result.savings_percentage >= 95.0


def test_code_stitching_multiline_block_indentation():
    """
    Tests that a multi-line replacement block with internal indentation (if/else/block)
    is not mangled by stripping leading spaces from the first line only.
    """
    skeleton_code = (
        "class OrderProcessor:\n"
        "    def process(self, order):\n"
        "        # TODO: execute order\n"
        "        return None\n"
    )
    anchor = AnchorTarget(
        anchor_id="anchor-order",
        signature="def process(self, order):",
        placeholder="# TODO: execute order",
    )
    skeleton = DraftSkeleton(
        file_path="orders.py",
        language="python",
        skeleton_code=skeleton_code,
        anchors=[anchor],
    )
    # Replacement passed with leading common indent as commonly emitted by LLMs
    replacement = """
        status = 'pending'
        if order.get('valid'):
            status = 'completed'
            order['id'] = 999
        return status
    """
    patches = [
        CodexPatch(
            anchor_id="anchor-order",
            replacement_code=replacement,
            codex_tokens_used=80,
        )
    ]

    stitched = SpeculativeDraftingPipeline.stitch_code(skeleton, patches)
    # Must be valid Python syntax without IndentationError
    parsed = ast.parse(stitched)
    assert parsed is not None
    assert "        status = 'pending'" in stitched
    assert "        if order.get('valid'):" in stitched
    assert "            status = 'completed'" in stitched
    assert "            order['id'] = 999" in stitched
    assert "        return status" in stitched


def test_code_stitching_with_target_snippet():
    """
    Tests anchoring when placeholder is absent but target_snippet is provided.
    """
    skeleton_code = (
        "def compute(a, b):\n"
        "    return a - b\n"
    )
    anchor = AnchorTarget(
        anchor_id="anchor-compute",
        signature="def compute(a, b):",
        placeholder="",
        target_snippet="return a - b",
    )
    skeleton = DraftSkeleton(
        file_path="math.py",
        language="python",
        skeleton_code=skeleton_code,
        anchors=[anchor],
    )
    patches = [
        CodexPatch(
            anchor_id="anchor-compute",
            replacement_code="return a + b",
        )
    ]
    stitched = SpeculativeDraftingPipeline.stitch_code(skeleton, patches)
    assert "return a + b" in stitched
    assert "return a - b" not in stitched
    ast.parse(stitched)


@pytest.mark.asyncio
async def test_speculative_syntax_error_event_warning():
    bus = EventBus()
    # Completer returning invalid python syntax
    pipeline = SpeculativeDraftingPipeline(
        event_bus=bus,
        codex_completer=lambda s: [CodexPatch(anchor_id=s.anchors[0].anchor_id, replacement_code="def broken(:::")],
    )
    skeleton = await pipeline.generate_draft("Test syntax error", "broken.py", language="python")
    final_code, patches, _ = await pipeline.complete_draft(skeleton)

    dialogues = bus.get_history()
    warnings = [d for d in dialogues if "Warning: Synthesized Python code contains syntax error" in d.message]
    assert len(warnings) == 1
